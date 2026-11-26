from __future__ import annotations

import json
import unittest

from micro_packet_decoder import decode_packet, iso8601_to_unix_ms, unix_ms_to_iso8601
from micro_protocol import OPCODE_BEACON, build_heartbeat_packet, build_location_packet, build_settings_update_packet, crc16_xmodem, default_settings

IMEI = "861352064050787"
TS = 1784640600000


class DecoderTests(unittest.TestCase):
    def test_crc_check_vector(self) -> None:
        self.assertEqual(crc16_xmodem(b"123456789"), 0x31C3)

    def test_canonical_extended_heartbeat(self) -> None:
        packet = build_heartbeat_packet(
            sequence_id=1, imei=IMEI, timestamp_ms=TS, battery=1, charging=1,
            last_update_minutes=2047, software_version=1, firmware_version=1,
            opcode=OPCODE_BEACON, location_data=bytes.fromhex("0FAC91003B91"),
            trusted_devices=[bytes.fromhex("123456789123")],
        )
        result = decode_packet(packet)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.summary["heartbeat_format"], "canonical_v7")
        self.assertEqual(result.summary["trusted_device_registry"]["count"], 1)
        self.assertEqual(len(packet), 70)
        json.dumps(result.json_ready())

    def test_location(self) -> None:
        packet = build_location_packet(sequence_id=2, imei=IMEI, timestamp_ms=TS, battery=1,
                                       charging=1, last_update_minutes=2047, latitude_e6=45421500,
                                       longitude_e6=-75697200, accuracy_x10=255, speed_x10=0)
        result = decode_packet(packet)
        self.assertTrue(result.valid, result.errors)
        self.assertAlmostEqual(result.summary["latitude"], 45.4215)

    def test_configuration_update_has_positional_fields(self) -> None:
        config = default_settings()
        config.update({"safe_zones": bytes.fromhex("02B513BCFB7CF3D00096"), "beacon_list": bytes.fromhex("0FAC91003B91"),
                       "trusted_device_list": bytes.fromhex("AABBCCDDEE01"), "heartbeat_interval_seconds": 60,
                       "lte_update_interval_seconds": 1023, "ble_check_interval_seconds": 480})
        packet = build_settings_update_packet(target_imei=IMEI, update_id=7, settings=config, sequence_id=3)
        result = decode_packet(packet, target_imei=IMEI)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(result.summary["configuration_update"]["update_id"], 7)
        self.assertTrue(any(field.name == "heartbeatIntervalSeconds" for field in result.fields))
        self.assertEqual(packet[8], 0x02)

    def test_rejects_old_command_0x20(self) -> None:
        packet = bytearray(build_settings_update_packet(target_imei=IMEI, update_id=1, settings=default_settings()))
        packet[8] = 0x20
        packet[4:6] = crc16_xmodem(packet[9:]).to_bytes(2, "big")
        result = decode_packet(bytes(packet))
        self.assertFalse(result.valid)
        self.assertTrue(any("Unknown command" in error for error in result.errors))

    def test_timestamp_roundtrip(self) -> None:
        self.assertEqual(iso8601_to_unix_ms("2026-07-21T13:30:00Z"), TS)
        self.assertEqual(unix_ms_to_iso8601(TS), "2026-07-21T13:30:00.000Z")
        self.assertIsNone(unix_ms_to_iso8601(0))


if __name__ == "__main__":
    unittest.main()
