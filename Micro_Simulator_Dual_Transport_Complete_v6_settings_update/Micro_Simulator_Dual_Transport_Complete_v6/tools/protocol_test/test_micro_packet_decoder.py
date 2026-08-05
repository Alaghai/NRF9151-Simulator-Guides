from __future__ import annotations

import json
import unittest

from micro_packet_decoder import decode_packet, iso8601_to_unix_ms, unix_ms_to_iso8601
from micro_protocol import (
    OPCODE_BEACON,
    build_heartbeat_packet,
    build_location_packet,
    build_settings_update_packet,
    crc16_xmodem,
)

IMEI = "861352064050787"
TS = 1784640600000
ORIGINAL_HB = bytes.fromhex(
    "AB10002569FD0001013836313335323036343035303738370000019F84DE77C0010107FF0101010FAC91003B91"
)
ORIGINAL_LOC = bytes.fromhex(
    "AB10002867C80001103836313335323036343035303738370000019F84DE77C0010107FF02B513BCFB7CF3D000FF0000"
)


class DecoderTests(unittest.TestCase):
    def test_crc_check_vector(self) -> None:
        self.assertEqual(crc16_xmodem(b"123456789"), 0x31C3)

    def test_original_heartbeat(self) -> None:
        result = decode_packet(ORIGINAL_HB)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.summary["heartbeat_format"], "original_v7")

    def test_extended_heartbeat(self) -> None:
        packet = build_heartbeat_packet(
            sequence_id=1,
            imei=IMEI,
            timestamp_ms=TS,
            battery=1,
            charging=1,
            last_update_minutes=2047,
            software_version=1,
            firmware_version=1,
            opcode=OPCODE_BEACON,
            location_data=bytes.fromhex("0FAC91003B91"),
            trusted_devices=[bytes.fromhex("123456789123")],
        )
        result = decode_packet(packet)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.summary["heartbeat_format"], "extended_v7")
        self.assertEqual(result.summary["trusted_device_registry"]["count"], 1)
        json.dumps(result.json_ready())

    def test_location(self) -> None:
        result = decode_packet(ORIGINAL_LOC)
        self.assertTrue(result.valid, result.errors)
        self.assertAlmostEqual(result.summary["latitude"], 45.4215)
        self.assertAlmostEqual(result.summary["longitude"], -75.6972)

    def test_invalid_latitude(self) -> None:
        packet = bytearray(ORIGINAL_LOC)
        packet[36:40] = (91_000_000).to_bytes(4, "big", signed=True)
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        result = decode_packet(bytes(packet))
        self.assertFalse(result.valid)
        self.assertTrue(any("Latitude" in error for error in result.errors))

    def test_invalid_longitude(self) -> None:
        packet = bytearray(ORIGINAL_LOC)
        packet[40:44] = (-181_000_000).to_bytes(4, "big", signed=True)
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        result = decode_packet(bytes(packet))
        self.assertFalse(result.valid)
        self.assertTrue(any("Longitude" in error for error in result.errors))

    def test_invalid_battery_value(self) -> None:
        packet = bytearray(ORIGINAL_HB)
        packet[32] = 0xFF
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        result = decode_packet(bytes(packet))
        self.assertFalse(result.valid)
        self.assertTrue(any("battery" in error.lower() for error in result.errors))

    def test_invalid_charging_value(self) -> None:
        packet = bytearray(ORIGINAL_HB)
        packet[33] = 0x00
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        result = decode_packet(bytes(packet))
        self.assertFalse(result.valid)
        self.assertTrue(any("charging" in error.lower() for error in result.errors))

    def test_generated_location(self) -> None:
        packet = build_location_packet(
            sequence_id=2,
            imei=IMEI,
            timestamp_ms=TS,
            battery=1,
            charging=1,
            last_update_minutes=2047,
            latitude_e6=45421500,
            longitude_e6=-75697200,
            accuracy_x10=255,
            speed_x10=0,
        )
        self.assertTrue(decode_packet(packet).valid)

    def test_settings_update(self) -> None:
        packet = build_settings_update_packet(
            target_imei=IMEI,
            update_id=7,
            settings={"heartbeat_interval_seconds": 60, "sleep_interval_seconds": 320},
            sequence_id=3,
        )
        result = decode_packet(packet, target_imei=IMEI)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.summary["settings_update"]["entry_count"], 2)
        self.assertTrue(any(field.name == "Target IMEI" for field in result.fields))

    def test_corrupt_length(self) -> None:
        packet = bytearray(ORIGINAL_HB)
        packet[3] ^= 1
        result = decode_packet(bytes(packet))
        self.assertFalse(result.valid)
        self.assertTrue(any("Length mismatch" in e for e in result.errors))

    def test_timestamp_roundtrip(self) -> None:
        self.assertEqual(iso8601_to_unix_ms("2026-07-21T13:30:00Z"), TS)
        self.assertEqual(unix_ms_to_iso8601(TS), "2026-07-21T13:30:00.000Z")
        self.assertIsNone(unix_ms_to_iso8601(0))


if __name__ == "__main__":
    unittest.main()
