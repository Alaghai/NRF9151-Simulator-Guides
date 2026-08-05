from __future__ import annotations

import unittest
from pathlib import Path

from micro_protocol import (
    COMMAND_SETTINGS_UPDATE,
    OPCODE_BEACON,
    OPCODE_GPS_LTE,
    SETTINGS_BY_NAME,
    TYPE_UINT16,
    ProtocolError,
    build_heartbeat_packet,
    build_packet,
    build_settings_update_packet,
    crc16_xmodem,
    decode_application_packet,
    decode_settings_payload,
    default_settings,
    encode_setting_value,
)

IMEI = "861352064050787"
TS = 1784640600000
BEACON = bytes.fromhex("0FAC91003B91")
TRUSTED = [
    bytes.fromhex("123456789123"),
    bytes.fromhex("AABBCCDDEE02"),
    bytes.fromhex("AB12C5D6E9A5"),
    bytes.fromhex("5C354DE15A6B"),
]
GPS = (
    int(45.4215 * 1_000_000).to_bytes(4, "big", signed=True)
    + int(-75.6972 * 1_000_000).to_bytes(4, "big", signed=True)
    + (255).to_bytes(2, "big")
    + (0).to_bytes(2, "big")
)

ORIGINAL_BEACON = bytes.fromhex(
    "AB10002569FD0001013836313335323036343035303738370000019F84DE77C0010107FF0101010FAC91003B91"
)
ORIGINAL_GPS = bytes.fromhex(
    "AB10002BFA890001013836313335323036343035303738370000019F84DE77C0010107FF01011002B513BCFB7CF3D000FF0000"
)


def heartbeat(opcode: int, location: bytes, trusted: list[bytes], seq: int = 1) -> bytes:
    return build_heartbeat_packet(
        sequence_id=seq,
        imei=IMEI,
        timestamp_ms=TS,
        battery=0x01,
        charging=0x01,
        last_update_minutes=2047,
        software_version=1,
        firmware_version=1,
        opcode=opcode,
        location_data=location,
        trusted_devices=trusted,
        extended=True,
    )


class HeartbeatTests(unittest.TestCase):
    def test_original_vectors_still_decode(self) -> None:
        for packet in (ORIGINAL_BEACON, ORIGINAL_GPS):
            decoded = decode_application_packet(packet)
            self.assertTrue(decoded["valid"], decoded["errors"])
            self.assertEqual(decoded["heartbeat_format"], "original_v7")

    def test_zero_trusted_devices(self) -> None:
        packet = heartbeat(OPCODE_BEACON, BEACON, [])
        decoded = decode_application_packet(packet)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(len(packet), 70)
        self.assertEqual(packet[2:4], b"\x00\x3E")
        self.assertEqual(decoded["trusted_device_registry"]["count"], 0)
        self.assertTrue(decoded["trusted_device_registry"]["unused_zero_filled"])

    def test_one_trusted_device(self) -> None:
        packet = heartbeat(OPCODE_BEACON, BEACON, TRUSTED[:1])
        decoded = decode_application_packet(packet)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(decoded["trusted_device_registry"]["configured"], [TRUSTED[0].hex().upper()])

    def test_four_trusted_devices_deterministic(self) -> None:
        packet = heartbeat(OPCODE_BEACON, BEACON, TRUSTED)
        decoded = decode_application_packet(packet)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(decoded["trusted_device_registry"]["configured"], [v.hex().upper() for v in TRUSTED])

    def test_gps_extended_length(self) -> None:
        packet = heartbeat(OPCODE_GPS_LTE, GPS, TRUSTED[:2])
        decoded = decode_application_packet(packet)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(len(packet), 76)
        self.assertEqual(packet[2:4], b"\x00\x44")

    def test_invalid_count_is_rejected(self) -> None:
        packet = bytearray(heartbeat(OPCODE_BEACON, BEACON, []))
        extension_start = 45
        packet[extension_start] = 5
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("exceeds four" in e for e in decoded["errors"]))

    def test_nonzero_unused_slot_is_rejected(self) -> None:
        packet = bytearray(heartbeat(OPCODE_BEACON, BEACON, TRUSTED[:1]))
        extension_start = 45
        packet[extension_start + 1 + 6] = 0xAA
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("not zero-filled" in e for e in decoded["errors"]))


class SettingsUpdateTests(unittest.TestCase):
    def build(self, settings: dict[str, object] | None = None) -> bytes:
        return build_settings_update_packet(
            target_imei=IMEI,
            update_id=0x1234,
            settings=settings or {"heartbeat_interval_seconds": 60},
            sequence_id=3,
        )

    def test_valid_one_setting(self) -> None:
        packet = self.build()
        decoded = decode_application_packet(packet, target_imei=IMEI)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(decoded["command"], COMMAND_SETTINGS_UPDATE)
        update = decoded["settings_update"]
        self.assertEqual(update["update_id"], 0x1234)
        self.assertEqual(update["entries"][0]["decoded_value"], 60)
        self.assertEqual(packet[25:27], b"\x12\x34")

    def test_zero_entry_update_is_rejected(self) -> None:
        payload = IMEI.encode("ascii") + b"\x01\x12\x34\x00"
        packet = build_packet(COMMAND_SETTINGS_UPDATE, payload, 1)
        decoded = decode_application_packet(packet, target_imei=IMEI)
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("at least one TLV entry" in error for error in decoded["errors"]))

    def test_multiple_settings(self) -> None:
        packet = self.build({
            "heartbeat_interval_seconds": 60,
            "lte_update_interval_seconds": 480,
            "sleep_interval_seconds": 320,
            "trusted_device_list": b"".join(TRUSTED[:2]),
        })
        decoded = decode_application_packet(packet, target_imei=IMEI)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(decoded["settings_update"]["entry_count"], 4)

    def test_big_endian_value_length_and_value(self) -> None:
        packet = self.build()
        payload = packet[9:]
        self.assertEqual(payload[19:23], bytes((0x01, TYPE_UINT16, 0x00, 0x02)))
        self.assertEqual(payload[23:25], b"\x00\x3C")

    def test_wrong_target_imei(self) -> None:
        decoded = decode_application_packet(self.build(), target_imei="123456789012345")
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("does not match" in e for e in decoded["errors"]))

    def test_invalid_crc(self) -> None:
        packet = bytearray(self.build())
        packet[-1] ^= 1
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("CRC mismatch" in e for e in decoded["errors"]))

    def test_unknown_setting_id(self) -> None:
        packet = bytearray(self.build())
        packet[28] = 0xFE  # first TLV setting ID: packet offset 9+19
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("Unknown setting ID" in e for e in decoded["errors"]))

    def test_wrong_value_type(self) -> None:
        packet = bytearray(self.build())
        packet[29] = 0x03
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])

    def test_duplicate_setting_id(self) -> None:
        definition = SETTINGS_BY_NAME["heartbeat_interval_seconds"]
        raw = encode_setting_value(definition, 60)
        payload = (
            IMEI.encode()
            + b"\x01\x12\x34\x02"
            + bytes((definition.setting_id, definition.value_type))
            + len(raw).to_bytes(2, "big") + raw
            + bytes((definition.setting_id, definition.value_type))
            + len(raw).to_bytes(2, "big") + raw
        )
        packet = build_packet(COMMAND_SETTINGS_UPDATE, payload, 1)
        decoded = decode_application_packet(packet)
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("Duplicate setting ID" in e for e in decoded["errors"]))

    def test_out_of_range(self) -> None:
        with self.assertRaises(ProtocolError):
            self.build({"heartbeat_interval_seconds": 0})

    def test_all_zero_trusted_device_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            self.build({"trusted_device_list": b"\x00" * 6})

    def test_truncated_tlv(self) -> None:
        packet = self.build()
        truncated_payload = packet[9:-1]
        rebuilt = build_packet(COMMAND_SETTINGS_UPDATE, truncated_payload, 3)
        decoded = decode_application_packet(rebuilt)
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("truncated" in e for e in decoded["errors"]))

    def test_invalid_header_and_property(self) -> None:
        packet = bytearray(self.build())
        packet[0] = 0xAA
        self.assertFalse(decode_application_packet(bytes(packet))["valid"])
        packet = bytearray(self.build())
        packet[1] = 0x11
        self.assertFalse(decode_application_packet(bytes(packet))["valid"])

    def test_invalid_declared_length(self) -> None:
        packet = bytearray(self.build())
        packet[3] ^= 0x01
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("Length" in e for e in decoded["errors"]))

    def test_wrong_command(self) -> None:
        packet = bytearray(self.build())
        packet[8] = 0x21
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("Unknown command" in e for e in decoded["errors"]))

    def test_unsupported_schema_version(self) -> None:
        packet = bytearray(self.build())
        packet[24] = 0x02
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("schema" in e.lower() for e in decoded["errors"]))

    def test_incorrect_value_length(self) -> None:
        packet = bytearray(self.build())
        packet[30:32] = b"\x00\x01"
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        decoded = decode_application_packet(bytes(packet))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("length" in e.lower() or "trailing" in e.lower() for e in decoded["errors"]))

    def test_unexpected_trailing_bytes(self) -> None:
        payload = self.build()[9:] + b"\x00"
        decoded = decode_application_packet(build_packet(COMMAND_SETTINGS_UPDATE, payload, 3))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("trailing" in e.lower() for e in decoded["errors"]))


class RegistrySyncTests(unittest.TestCase):
    def test_c_and_python_protocol_ids_agree(self) -> None:
        header = (Path(__file__).resolve().parents[2] / "applications" / "micro_simulator" / "src" / "micro_settings.h").read_text()
        expected = {
            "MICRO_SETTINGS_COMMAND": "0x20U",
            "MICRO_SETTING_HEARTBEAT_INTERVAL": "0x01U",
            "MICRO_SETTING_LTE_UPDATE_INTERVAL": "0x02U",
            "MICRO_SETTING_SLEEP_INTERVAL": "0x03U",
            "MICRO_SETTING_SAFE_ZONES": "0x10U",
            "MICRO_SETTING_BEACON_LIST": "0x11U",
            "MICRO_SETTING_TRUSTED_DEVICE_LIST": "0x12U",
        }
        for name, value in expected.items():
            self.assertIn(f"#define {name} {value}", header)

    def test_python_and_c_trusted_device_defaults_agree(self) -> None:
        c_source_path = (
            Path(__file__).resolve().parents[2]
            / "applications"
            / "micro_simulator"
            / "src"
            / "micro_settings.c"
        )
        c_source = c_source_path.read_text()
        self.assertIn(
            "const uint8_t default_trusted[MICRO_DEVICE_ID_BYTES] = "
            "{ 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01 };",
            c_source,
        )
        self.assertEqual(default_settings()["trusted_device_list"], bytes.fromhex("AABBCCDDEE01"))


if __name__ == "__main__":
    unittest.main()
