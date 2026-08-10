from __future__ import annotations

import pathlib
import tempfile
import unittest

from micro_pending_store import PendingUpdateStore
from micro_protocol import OPCODE_BEACON, build_heartbeat_packet, build_settings_update_packet, default_settings
from micro_response_parser import ServerResponseParser
from micro_tcp_server import MicroStreamParser, decide_response

IMEI = "861352064050787"
OTHER_IMEI = "123456789012345"
TS = 1784640600000


def heartbeat(imei: str = IMEI, sequence: int = 1) -> bytes:
    return build_heartbeat_packet(
        sequence_id=sequence, imei=imei, timestamp_ms=TS, battery=1, charging=1,
        last_update_minutes=2047, software_version=1, firmware_version=1,
        opcode=OPCODE_BEACON, location_data=bytes.fromhex("0FAC91003B91"),
        trusted_devices=[bytes.fromhex("123456789123"), bytes.fromhex("AABBCCDDEE02")],
    )


def update(imei: str = IMEI, update_id: int = 55) -> bytes:
    config = default_settings()
    config.update({"heartbeat_interval_seconds": 120, "safe_zones": bytes.fromhex("02B513BCFB7CF3D00096"),
                   "beacon_list": bytes.fromhex("0FAC91003B91"), "trusted_device_list": bytes.fromhex("AABBCCDDEE01")})
    return build_settings_update_packet(target_imei=imei, update_id=update_id, settings=config, sequence_id=9)


class StreamParserTests(unittest.TestCase):
    def test_binary_ascii_and_multiple_packet_framing(self) -> None:
        parser = MicroStreamParser(); packet = heartbeat()
        self.assertEqual(parser.feed(packet[:7]), [])
        self.assertEqual(parser.feed(packet[7:])[0].packet, packet)
        parser = MicroStreamParser()
        self.assertEqual(parser.feed(packet.hex().encode()[:13]), [])
        self.assertEqual(parser.feed(packet.hex().encode()[13:])[0].wire_mode, "ascii_hex")
        parser = MicroStreamParser()
        self.assertEqual(len(parser.feed(heartbeat(sequence=1) + heartbeat(sequence=2))), 2)


class AutoResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PendingUpdateStore(pathlib.Path(self.tmp.name) / "pending.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_valid_heartbeat_without_pending_update_returns_ok(self) -> None:
        self.assertEqual(decide_response("AUTO", heartbeat(), store=self.store).response, b"OK\n")

    def test_invalid_packet_returns_error(self) -> None:
        packet = bytearray(heartbeat()); packet[-1] ^= 1
        self.assertTrue(decide_response("AUTO", bytes(packet), store=self.store).response.startswith(b"ERROR"))

    def test_pending_update_is_imei_specific_and_persists(self) -> None:
        packet = update(); self.store.queue_packet(imei=IMEI, packet=packet)
        decision = decide_response("AUTO", heartbeat(), store=self.store)
        self.assertEqual(decision.response, b"SUP\n" + packet)
        self.assertEqual(decision.pending_update_id, 55)
        self.assertEqual(decide_response("AUTO", heartbeat(OTHER_IMEI), store=self.store).response, b"OK\n")
        reopened = PendingUpdateStore(self.store.path)
        self.assertEqual(reopened.get(IMEI)["update_id"], 55)
        self.assertEqual(reopened.get(IMEI)["configuration"]["heartbeat_interval_seconds"], 120)

    def test_bad_queued_packet_does_not_overwrite_previous_record(self) -> None:
        good = self.store.queue_packet(imei=IMEI, packet=update())
        bad = bytearray(update()); bad[-1] ^= 1
        with self.assertRaises(Exception): self.store.queue_packet(imei=IMEI, packet=bytes(bad))
        self.assertEqual(self.store.get(IMEI)["packet_hex"], good["packet_hex"])


class ResponseParserTests(unittest.TestCase):
    def test_split_token_same_read_fragmented_packet_and_buffered_bytes(self) -> None:
        packet = update(); parser = ServerResponseParser()
        self.assertEqual(parser.feed(b"S"), [])
        self.assertEqual([event.kind for event in parser.feed(b"UP\n" + packet[:10])], ["SUP"])
        events = parser.feed(packet[10:] + b"OK\n")
        self.assertEqual([event.kind for event in events], ["SETTINGS_PACKET", "OK"])

    def test_connection_closure_and_timeout_during_update(self) -> None:
        parser = ServerResponseParser(); parser.feed(b"SUP\n" + update()[:5])
        self.assertEqual(parser.connection_closed().kind, "INCOMPLETE_SETTINGS")
        parser = ServerResponseParser(); parser.feed(b"SUP\n" + update()[:7])
        self.assertEqual(parser.timeout().kind, "SETTINGS_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
