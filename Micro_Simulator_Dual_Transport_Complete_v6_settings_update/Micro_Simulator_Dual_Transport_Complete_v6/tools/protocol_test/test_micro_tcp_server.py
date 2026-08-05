from __future__ import annotations

import pathlib
import tempfile
import unittest
import json
from unittest.mock import patch

import micro_tcp_server as tcp_server
from micro_pending_store import PendingUpdateStore
from micro_protocol import (
    COMMAND_SETTINGS_UPDATE,
    OPCODE_BEACON,
    ProtocolError,
    build_heartbeat_packet,
    build_packet,
    build_settings_update_packet,
    build_location_packet,
    crc16_xmodem,
)
from micro_response_parser import ServerResponseParser
from micro_tcp_server import FramedPacket, MicroStreamParser, decide_response

IMEI = "861352064050787"
OTHER_IMEI = "123456789012345"
TS = 1784640600000


def heartbeat(imei: str = IMEI, seq: int = 1, extended: bool = True) -> bytes:
    return build_heartbeat_packet(
        sequence_id=seq,
        imei=imei,
        timestamp_ms=TS,
        battery=1,
        charging=1,
        last_update_minutes=2047,
        software_version=1,
        firmware_version=1,
        opcode=OPCODE_BEACON,
        location_data=bytes.fromhex("0FAC91003B91"),
        trusted_devices=[bytes.fromhex("123456789123")],
        extended=extended,
    )


def location(imei: str = IMEI, seq: int = 1) -> bytes:
    return build_location_packet(
        sequence_id=seq,
        imei=imei,
        timestamp_ms=TS,
        battery=1,
        charging=1,
        last_update_minutes=2047,
        latitude_e6=45421500,
        longitude_e6=-75697200,
        accuracy_x10=255,
        speed_x10=0,
    )


def zero_entry_update() -> bytes:
    payload = IMEI.encode("ascii") + b"\x01\x00\x01\x00"
    return build_packet(COMMAND_SETTINGS_UPDATE, payload, 1)


def update(imei: str = IMEI) -> bytes:
    return build_settings_update_packet(
        target_imei=imei,
        update_id=55,
        settings={"heartbeat_interval_seconds": 60},
        sequence_id=9,
    )


class StreamParserTests(unittest.TestCase):
    def test_binary_fragmentation(self) -> None:
        parser = MicroStreamParser()
        packet = heartbeat()
        self.assertEqual(parser.feed(packet[:7]), [])
        framed = parser.feed(packet[7:])
        self.assertEqual(len(framed), 1)
        self.assertEqual(framed[0].packet, packet)

    def test_ascii_fragmentation(self) -> None:
        parser = MicroStreamParser()
        encoded = heartbeat().hex().upper().encode()
        self.assertEqual(parser.feed(encoded[:13]), [])
        framed = parser.feed(encoded[13:])
        self.assertEqual(framed[0].wire_mode, "ascii_hex")

    def test_multiple_packets_one_read(self) -> None:
        parser = MicroStreamParser()
        packets = parser.feed(heartbeat(seq=1) + heartbeat(seq=2))
        self.assertEqual(len(packets), 2)

    def test_mixed_modes(self) -> None:
        parser = MicroStreamParser()
        binary = heartbeat(seq=1)
        ascii_hex = heartbeat(seq=2).hex().encode()
        packets = parser.feed(binary + ascii_hex)
        self.assertEqual([p.wire_mode for p in packets], ["binary", "ascii_hex"])

    def test_ascii_ab_noise_before_binary_packet(self) -> None:
        parser = MicroStreamParser()
        packet = heartbeat()
        framed = parser.feed(b"XAB" + packet)
        self.assertEqual(len(framed), 1)
        self.assertEqual(framed[0].wire_mode, "binary")
        self.assertEqual(framed[0].packet, packet)

    def test_fragmented_ascii_ab_noise_before_binary_packet(self) -> None:
        parser = MicroStreamParser()
        packet = heartbeat()
        self.assertEqual(parser.feed(b"XAB"), [])
        framed = parser.feed(packet)
        self.assertEqual(len(framed), 1)
        self.assertEqual(framed[0].wire_mode, "binary")
        self.assertEqual(framed[0].packet, packet)


class AutoResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PendingUpdateStore(pathlib.Path(self.tmp.name) / "pending.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_no_update_returns_ok(self) -> None:
        decision = decide_response("AUTO", heartbeat(), store=self.store)
        self.assertEqual(decision.response, b"OK\n")

    def test_invalid_returns_error(self) -> None:
        packet = bytearray(heartbeat())
        packet[-1] ^= 1
        decision = decide_response("AUTO", bytes(packet), store=self.store)
        self.assertTrue(decision.response.startswith(b"ERROR"))

    def test_invalid_location_returns_error(self) -> None:
        packet = bytearray(location())
        packet[36:40] = (91_000_000).to_bytes(4, "big", signed=True)
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decision = decide_response("AUTO", bytes(packet), store=self.store)
        self.assertTrue(decision.response.startswith(b"ERROR"))

    def test_invalid_battery_returns_error(self) -> None:
        packet = bytearray(heartbeat())
        packet[32] = 0xFF
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decision = decide_response("AUTO", bytes(packet), store=self.store)
        self.assertTrue(decision.response.startswith(b"ERROR"))

    def test_invalid_charging_returns_error(self) -> None:
        packet = bytearray(heartbeat())
        packet[33] = 0x00
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decision = decide_response("AUTO", bytes(packet), store=self.store)
        self.assertTrue(decision.response.startswith(b"ERROR"))

    def test_pending_update_returns_sup_and_binary_packet(self) -> None:
        packet = update()
        self.store.queue_packet(imei=IMEI, packet=packet)
        decision = decide_response("AUTO", heartbeat(), store=self.store)
        self.assertEqual(decision.response, b"SUP\n" + packet)
        self.assertTrue(decision.sends_settings_packet)
        self.assertEqual(decision.pending_update_id, 55)

    def test_update_is_imei_specific(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update())
        decision = decide_response("AUTO", heartbeat(OTHER_IMEI), store=self.store)
        self.assertEqual(decision.response, b"OK\n")

    def test_sent_unconfirmed_not_repeated(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update())
        self.store.mark_sent(IMEI, "conn")
        decision = decide_response("AUTO", heartbeat(), store=self.store)
        self.assertEqual(decision.response, b"OK\n")

    def test_existing_diagnostic_modes(self) -> None:
        packet = heartbeat()
        self.assertEqual(decide_response("OK", packet, store=self.store).response, b"OK\n")
        self.assertTrue(decide_response("ECHO_HEX", packet, store=self.store).response.startswith(b"RX_HEX:"))

    def test_pending_store_survives_reopen(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update())
        reopened = PendingUpdateStore(self.store.path)
        record = reopened.get(IMEI)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["update_id"], 55)

    def test_cancel_and_requeue(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update())
        self.assertEqual(self.store.cancel(IMEI)["status"], "cancelled")
        self.assertEqual(self.store.requeue(IMEI)["status"], "pending")

    def test_invalid_queued_packet_does_not_replace_valid_record(self) -> None:
        valid = self.store.queue_packet(imei=IMEI, packet=update())
        damaged = bytearray(update())
        damaged[-1] ^= 1
        with self.assertRaises(Exception):
            self.store.queue_packet(imei=IMEI, packet=bytes(damaged))
        self.assertEqual(self.store.get(IMEI)["packet_hex"], valid["packet_hex"])

    def test_zero_entry_update_is_rejected_without_replacing_record(self) -> None:
        valid = self.store.queue_packet(imei=IMEI, packet=update())
        with self.assertRaises(ProtocolError):
            self.store.queue_packet(imei=IMEI, packet=zero_entry_update())
        record = self.store.get(IMEI)
        self.assertEqual(record["packet_hex"], valid["packet_hex"])


class DecoderLoadingAndLogSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PendingUpdateStore(pathlib.Path(self.tmp.name) / "pending.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _log_and_read(self, packet: bytes, mode: str = "OK") -> tuple[str, dict[str, object]]:
        log_path = pathlib.Path(self.tmp.name) / "packets.log"
        trace_path = pathlib.Path(self.tmp.name) / "trace.jsonl"
        with patch.object(tcp_server, "LOG_FILE", log_path), patch.object(tcp_server, "TRACE_FILE", trace_path):
            decision = decide_response(mode, packet, store=self.store)
            tcp_server.log_packet(
                "test-connection",
                ("127.0.0.1", 12345),
                FramedPacket("binary", packet),
                decision,
            )
        trace = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[-1])
        return log_path.read_text(encoding="utf-8"), trace

    def test_original_heartbeat_log_schema(self) -> None:
        text, trace = self._log_and_read(heartbeat(extended=False))
        self.assertIn("header_byte: 0xAB", text)
        self.assertIn("sequence_id: 1", text)
        self.assertIn("timestamp_utc: 2026-07-21T13:30:00.000Z", text)
        self.assertEqual(trace["header_byte"], "0xAB")
        self.assertEqual(trace["sequence_id"], 1)
        self.assertEqual(trace["timestamp_utc"], "2026-07-21T13:30:00.000Z")

    def test_extended_heartbeat_log_schema(self) -> None:
        text, trace = self._log_and_read(heartbeat())
        self.assertIn("heartbeat_format: extended_v7", text)
        self.assertEqual(trace["validation"], "VALID")
        self.assertEqual(trace["sequence_id"], 1)

    def test_invalid_packet_log_schema(self) -> None:
        packet = bytearray(heartbeat())
        packet[-1] ^= 1
        text, trace = self._log_and_read(bytes(packet))
        self.assertIn("validation: INVALID", text)
        self.assertIn("header_byte: 0xAB", text)
        self.assertIn("sequence_id: 1", text)
        self.assertIn("timestamp_utc: unavailable", text)
        self.assertEqual(trace["validation"], "INVALID")
        self.assertEqual(trace["header_byte"], "0xAB")
        self.assertEqual(trace["sequence_id"], 1)
        self.assertEqual(trace["timestamp_utc"], "unavailable")

    def test_decoder_unavailable_does_not_crash_and_is_logged(self) -> None:
        packet = heartbeat()
        with patch.object(tcp_server, "decode_packet", None):
            text, trace = self._log_and_read(packet, mode="AUTO")
        self.assertIn("validation: decoder unavailable", text)
        self.assertIn("validation_error: decoder unavailable", text)
        self.assertIn("header_byte: unavailable", text)
        self.assertIn("sequence_id: unavailable", text)
        self.assertIn("timestamp_utc: unavailable", text)
        self.assertEqual(trace["validation"], "decoder unavailable")
        self.assertEqual(trace["header_byte"], "unavailable")
        self.assertEqual(trace["sequence_id"], "unavailable")
        self.assertEqual(trace["timestamp_utc"], "unavailable")


class ResponseParserTests(unittest.TestCase):
    def test_ok_split(self) -> None:
        parser = ServerResponseParser()
        self.assertEqual(parser.feed(b"O"), [])
        events = parser.feed(b"K\n")
        self.assertEqual(events[0].kind, "OK")

    def test_error_split(self) -> None:
        parser = ServerResponseParser()
        parser.feed(b"ERR")
        events = parser.feed(b"OR:INVALID_CRC\n")
        self.assertEqual(events[0].kind, "ERROR")

    def test_sup_and_packet_same_read(self) -> None:
        parser = ServerResponseParser()
        events = parser.feed(b"SUP\n" + update())
        self.assertEqual([e.kind for e in events], ["SUP", "SETTINGS_PACKET"])

    def test_sup_fragmented_packet(self) -> None:
        parser = ServerResponseParser()
        packet = update()
        events = parser.feed(b"S")
        self.assertEqual(events, [])
        events = parser.feed(b"UP\n" + packet[:10])
        self.assertEqual([e.kind for e in events], ["SUP"])
        events = parser.feed(packet[10:])
        self.assertEqual(events[0].kind, "SETTINGS_PACKET")

    def test_connection_closed_early(self) -> None:
        parser = ServerResponseParser()
        parser.feed(b"SUP\n" + update()[:5])
        event = parser.connection_closed()
        self.assertIsNotNone(event)
        self.assertEqual(event.kind, "INCOMPLETE_SETTINGS")

    def test_fwup_deferred_token(self) -> None:
        parser = ServerResponseParser()
        self.assertEqual(parser.feed(b"FWUP\n")[0].kind, "FWUP")

    def test_fwup_fragmented_token_is_deferred(self) -> None:
        parser = ServerResponseParser()
        self.assertEqual(parser.feed(b"F"), [])
        self.assertEqual(parser.feed(b"W"), [])
        self.assertEqual(parser.feed(b"U"), [])
        events = parser.feed(b"P\n")
        self.assertEqual([event.kind for event in events], ["FWUP"])
        self.assertFalse(parser.awaiting_settings)

    def test_unknown_response(self) -> None:
        parser = ServerResponseParser()
        self.assertEqual(parser.feed(b"WAT\n")[0].kind, "UNKNOWN")

    def test_settings_receive_timeout(self) -> None:
        parser = ServerResponseParser()
        parser.feed(b"SUP\n" + update()[:7])
        event = parser.timeout()
        self.assertEqual(event.kind, "SETTINGS_TIMEOUT")
        self.assertFalse(parser.awaiting_settings)
        self.assertEqual(parser.buffer, bytearray())

    def test_response_token_timeout(self) -> None:
        parser = ServerResponseParser()
        parser.feed(b"ER")
        event = parser.timeout()
        self.assertEqual(event.kind, "RESPONSE_TIMEOUT")
        self.assertEqual(event.raw, b"ER")


if __name__ == "__main__":
    unittest.main()
