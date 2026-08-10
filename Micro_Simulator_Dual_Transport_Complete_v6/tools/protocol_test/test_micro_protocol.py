from __future__ import annotations

import unittest
from pathlib import Path

from micro_protocol import (
    COMMAND_CONFIGURATION_UPDATE,
    OPCODE_BEACON,
    OPCODE_GPS_LTE,
    ProtocolError,
    build_heartbeat_packet,
    build_packet,
    build_settings_update_packet,
    crc16_xmodem,
    decode_application_packet,
    default_settings,
)

IMEI = "861352064050787"
TS = 1784640600000
BEACON = bytes.fromhex("0FAC91003B91")
TRUSTED = [
    bytes.fromhex("AABBCCDDEE01"), bytes.fromhex("CCDDEEFFAABB"),
    bytes.fromhex("AABBCCDDEE03"), bytes.fromhex("BBCCDDEEFF00"),
]
ZONES = bytes.fromhex("02B513BCFB7CF3D0009602B05747FB7D6B16012C")

SAMPLES = [
    "AB100032EF0E00010238363133353230363430353037383700010102B513BCFB7CF3D00096010FAC91003B9101AABBCCDDEE01003C03FF01E000",
    "AB100048289A00020238363133353230363430353037383700020202B513BCFB7CF3D0009602B05747FB7D6B16012C020FAC91003B9100112233445502AABBCCDDEE01CCDDEEFFAABB007803FF0096FF",
    "AB100064869400030238363133353230363430353037383700030302B513BCFB7CF3D0009602B05747FB7D6B16012C02B2094EFB7BB90F012C030FAC91003B9100112233445511223344556604AABBCCDDEE01CCDDEEFFAABBAABBCCDDEE03BBCCDDEEFF0000B403FF0096FF",
    "AB100074060A00040238363133353230363430353037383700040402B513BCFB7CF3D0009602B05747FB7D6B16012C02B2094EFB7BB90F012C02B58310FB7C73B000C8040FAC91003B910011223344551122334455660FAC91003B9404AABBCCDDEE01CCDDEEFFAABBAABBCCDDEE03BBCCDDEEFF00012C007803B6FF",
]


def config(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = default_settings()
    values.update(overrides)
    return values


class HeartbeatTests(unittest.TestCase):
    def heartbeat(self, opcode: int, location: bytes) -> bytes:
        return build_heartbeat_packet(
            sequence_id=1, imei=IMEI, timestamp_ms=TS, battery=1, charging=1,
            last_update_minutes=2047, software_version=1, firmware_version=1,
            opcode=opcode, location_data=location, trusted_devices=TRUSTED,
        )

    def test_beacon_heartbeat_includes_all_four_fixed_slots(self) -> None:
        packet = self.heartbeat(OPCODE_BEACON, BEACON)
        decoded = decode_application_packet(packet)
        self.assertTrue(decoded["valid"], decoded["errors"])
        self.assertEqual(len(packet), 70)
        self.assertEqual(packet[2:4], b"\x00\x3E")
        self.assertEqual(decoded["trusted_device_registry"]["configured"], [item.hex().upper() for item in TRUSTED])

    def test_gps_heartbeat_includes_registry(self) -> None:
        gps = bytes.fromhex("02B513BCFB7CF3D000FF0000")
        packet = self.heartbeat(OPCODE_GPS_LTE, gps)
        self.assertEqual(len(packet), 76)
        self.assertEqual(packet[2:4], b"\x00\x44")
        self.assertTrue(decode_application_packet(packet)["valid"])

    def test_missing_registry_is_rejected(self) -> None:
        packet = self.heartbeat(OPCODE_BEACON, BEACON)
        shortened = build_packet(0x01, packet[9:-25], 1)
        decoded = decode_application_packet(shortened)
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("registry" in text.lower() for text in decoded["errors"]))


class ConfigurationUpdateTests(unittest.TestCase):
    def packet(self, **overrides: object) -> bytes:
        return build_settings_update_packet(target_imei=IMEI, update_id=7, settings=config(**overrides), sequence_id=3)

    def test_exact_canonical_vectors(self) -> None:
        expected = [(1, 1, 1, 1, 60), (2, 2, 2, 2, 120), (3, 3, 3, 4, 180), (4, 4, 4, 4, 300)]
        for text, values in zip(SAMPLES, expected):
            packet = bytes.fromhex(text)
            decoded = decode_application_packet(packet, target_imei=IMEI)
            update = decoded["configuration_update"]
            self.assertTrue(decoded["valid"], decoded["errors"])
            self.assertEqual((update["update_id"], update["gps_safe_zone_count"], update["beacon_count"], update["trusted_device_count"], update["heartbeat_interval_seconds"]), values)
        self.assertEqual(len(bytes.fromhex(SAMPLES[3])), 124)
        self.assertEqual(bytes.fromhex(SAMPLES[3])[2:6], bytes.fromhex("0074060A"))

    def test_builder_encodes_full_replacement(self) -> None:
        packet = self.packet(safe_zones=ZONES, beacon_list=BEACON, trusted_device_list=TRUSTED[0], heartbeat_interval_seconds=60, lte_update_interval_seconds=1023, ble_check_interval_seconds=480)
        decoded = decode_application_packet(packet, target_imei=IMEI)
        self.assertTrue(decoded["valid"], decoded["errors"])
        update = decoded["configuration_update"]
        self.assertEqual(update["gps_safe_zone_count"], 2)
        self.assertEqual(update["heartbeat_interval_seconds"], 60)
        self.assertEqual(packet[8], COMMAND_CONFIGURATION_UPDATE)

    def test_legacy_sleep_alias_preserves_the_exact_ble_field_bytes(self) -> None:
        canonical = self.packet(ble_check_interval_seconds=480)
        legacy = config()
        legacy.pop("ble_check_interval_seconds")
        legacy["sleep_interval_seconds"] = 480
        aliased = build_settings_update_packet(target_imei=IMEI, update_id=7, settings=legacy, sequence_id=3)
        self.assertEqual(aliased, canonical)

    def test_partial_configuration_is_not_encoded(self) -> None:
        with self.assertRaises(ProtocolError):
            build_settings_update_packet(target_imei=IMEI, update_id=1, settings={"heartbeat_interval_seconds": 60})

    def test_bad_crc_and_length_are_rejected(self) -> None:
        packet = bytearray(self.packet())
        packet[-1] ^= 1
        self.assertFalse(decode_application_packet(bytes(packet))["valid"])
        packet = bytearray(self.packet())
        packet[3] ^= 1
        self.assertFalse(decode_application_packet(bytes(packet))["valid"])

    def test_all_three_intervals_must_be_nonzero(self) -> None:
        for name in ("heartbeat_interval_seconds", "lte_update_interval_seconds", "ble_check_interval_seconds"):
            values = config(**{name: 0})
            with self.assertRaises(ProtocolError):
                build_settings_update_packet(target_imei=IMEI, update_id=1, settings=values)

    def test_wrong_target_imei_and_count_overflow_are_rejected(self) -> None:
        self.assertFalse(decode_application_packet(self.packet(), target_imei="123456789012345")["valid"])
        payload = bytearray(self.packet()[9:])
        payload[17] = 5
        decoded = decode_application_packet(build_packet(COMMAND_CONFIGURATION_UPDATE, bytes(payload), 3))
        self.assertFalse(decoded["valid"])
        self.assertTrue(any("exceeds four" in error for error in decoded["errors"]))

    def test_truncation_coordinate_flag_and_trailing_bytes_are_rejected(self) -> None:
        full = self.packet(safe_zones=ZONES[:10])
        self.assertFalse(decode_application_packet(build_packet(2, full[9:-8], 3))["valid"])
        payload = bytearray(full[9:]); payload[18:22] = (91_000_000).to_bytes(4, "big", signed=True)
        self.assertFalse(decode_application_packet(build_packet(2, bytes(payload), 3))["valid"])
        payload = bytearray(self.packet()[9:]); payload[-1] = 1
        self.assertFalse(decode_application_packet(build_packet(2, bytes(payload), 3))["valid"])
        self.assertFalse(decode_application_packet(build_packet(2, self.packet()[9:] + b"\x00", 3))["valid"])

    def test_atomic_rejection_and_zero_counts(self) -> None:
        previous = self.packet(safe_zones=ZONES[:10], beacon_list=BEACON, trusted_device_list=TRUSTED[0])
        previous_decoded = decode_application_packet(previous)["configuration_update"]
        invalid_payload = bytearray(previous[9:]); invalid_payload[-1] = 1
        rejected = decode_application_packet(build_packet(2, bytes(invalid_payload), 3))
        self.assertFalse(rejected["valid"])
        # The already-decoded active model remains unchanged until a valid packet is applied.
        self.assertEqual(previous_decoded["gps_safe_zone_count"], 1)
        cleared = decode_application_packet(self.packet(safe_zones=b"", beacon_list=b"", trusted_device_list=b""))["configuration_update"]
        self.assertTrue(cleared["valid"])
        self.assertEqual((cleared["gps_safe_zone_count"], cleared["beacon_count"], cleared["trusted_device_count"]), (0, 0, 0))


class CrossLanguageConstantsTests(unittest.TestCase):
    def test_c_header_uses_canonical_command_and_e6_coordinates(self) -> None:
        header = (Path(__file__).resolve().parents[2] / "applications" / "micro_simulator" / "src" / "micro_settings.h").read_text()
        self.assertIn("#define MICRO_CONFIGURATION_UPDATE_COMMAND 0x02U", header)
        self.assertIn("int32_t latitude_e6;", header)
        self.assertIn("uint16_t last_update_id;", header)

    def test_crc_check_vector(self) -> None:
        self.assertEqual(crc16_xmodem(b"123456789"), 0x31C3)


if __name__ == "__main__":
    unittest.main()
