#!/usr/bin/env python3
"""Shared Micro Version 7 protocol constants, encoders, and validators.

The Version 7 envelope is unchanged. This module adds:
- backward-compatible decoding of original and extended heartbeat packets;
- the 25-byte configured trusted-device heartbeat extension;
- SETTINGS_UPDATE command 0x20 with a typed TLV payload;
- one shared Python settings registry used by the decoder, server, update tool,
  and automated tests.

The under-development firmware parser supplied with this project uses a
positional settings payload. The TLV command implemented here follows the
explicit simulator task specification; setting semantics from that parser are
represented in the registry where practical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

HEADER = 0xAB
PROPERTY = 0x10
COMMAND_HEARTBEAT = 0x01
COMMAND_LOCATION = 0x10
COMMAND_SETTINGS_UPDATE = 0x20
COMMAND_OFFSET = 8
PAYLOAD_OFFSET = 9
FIXED_PREFIX_BYTES = 8
SCHEMA_VERSION = 0x01
COORD_SCALE = 1_000_000
MAX_TRUSTED_DEVICES = 4
TRUSTED_DEVICE_BYTES = 6
TRUSTED_EXTENSION_BYTES = 1 + (MAX_TRUSTED_DEVICES * TRUSTED_DEVICE_BYTES)
MAX_SAFE_ZONES = 4
SAFE_ZONE_BYTES = 10
MAX_BEACONS = 4
BEACON_BYTES = 6

TYPE_UINT8 = 0x01
TYPE_UINT16 = 0x02
TYPE_UINT32 = 0x03
TYPE_INT32 = 0x04
TYPE_BOOL = 0x05
TYPE_RAW = 0x06
TYPE_UTF8 = 0x07

VALUE_TYPE_NAMES = {
    TYPE_UINT8: "uint8",
    TYPE_UINT16: "uint16",
    TYPE_UINT32: "uint32",
    TYPE_INT32: "int32",
    TYPE_BOOL: "boolean",
    TYPE_RAW: "raw bytes",
    TYPE_UTF8: "UTF-8 string",
}

OPCODE_BEACON = 0x01
OPCODE_GPS_SAFEZONE = 0x0A
OPCODE_GPS_LTE = 0x10
OPCODE_TRUSTED = 0xA0
GPS_OPCODES = {OPCODE_GPS_SAFEZONE, OPCODE_GPS_LTE}
ADDRESS_OPCODES = {OPCODE_BEACON, OPCODE_TRUSTED}
OPCODE_NAMES = {
    OPCODE_BEACON: "beacon detected",
    OPCODE_GPS_SAFEZONE: "GPS safe zone",
    OPCODE_GPS_LTE: "GPS + LTE",
    OPCODE_TRUSTED: "trusted device detected",
}

BATTERY_NAMES = {0x00: "low", 0x01: "medium", 0x10: "high"}
CHARGING_NAMES = {0x01: "not charging", 0x10: "charging"}


class ProtocolError(ValueError):
    """A deterministic packet construction or validation failure."""


Validator = Callable[[Any], None]


@dataclass(frozen=True)
class SettingDefinition:
    setting_id: int
    name: str
    value_type: int
    minimum: int | None
    maximum: int | None
    default: Any
    persistent: bool
    fixed_length: int | None = None
    validator: Validator | None = None
    description: str = ""

    def validate(self, value: Any) -> None:
        if self.value_type in (TYPE_UINT8, TYPE_UINT16, TYPE_UINT32, TYPE_INT32):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ProtocolError(f"{self.name} must be an integer.")
            if self.minimum is not None and value < self.minimum:
                raise ProtocolError(f"{self.name} is below minimum {self.minimum}.")
            if self.maximum is not None and value > self.maximum:
                raise ProtocolError(f"{self.name} exceeds maximum {self.maximum}.")
        elif self.value_type == TYPE_BOOL:
            if not isinstance(value, bool):
                raise ProtocolError(f"{self.name} must be Boolean.")
        elif self.value_type == TYPE_RAW:
            if not isinstance(value, (bytes, bytearray)):
                raise ProtocolError(f"{self.name} must be raw bytes.")
        elif self.value_type == TYPE_UTF8:
            if not isinstance(value, str):
                raise ProtocolError(f"{self.name} must be text.")
        else:
            raise ProtocolError(f"Unsupported registry value type 0x{self.value_type:02X}.")

        if self.validator is not None:
            self.validator(value)


def _validate_safe_zones(value: bytes | bytearray) -> None:
    raw = bytes(value)
    if len(raw) % SAFE_ZONE_BYTES != 0:
        raise ProtocolError("safe_zones must contain 10-byte records.")
    if len(raw) // SAFE_ZONE_BYTES > MAX_SAFE_ZONES:
        raise ProtocolError("safe_zones supports at most four records.")
    for idx in range(0, len(raw), SAFE_ZONE_BYTES):
        lat = int.from_bytes(raw[idx : idx + 4], "big", signed=True)
        lon = int.from_bytes(raw[idx + 4 : idx + 8], "big", signed=True)
        radius = int.from_bytes(raw[idx + 8 : idx + 10], "big")
        # The supplied under-development firmware parser names coordinates e7.
        if not -900_000_000 <= lat <= 900_000_000:
            raise ProtocolError("safe-zone latitude_e7 is outside -90..90 degrees.")
        if not -1_800_000_000 <= lon <= 1_800_000_000:
            raise ProtocolError("safe-zone longitude_e7 is outside -180..180 degrees.")
        if radius == 0:
            raise ProtocolError("safe-zone radius_m must be greater than zero.")


def _validate_six_byte_list(value: bytes | bytearray, label: str) -> None:
    raw = bytes(value)
    if len(raw) % 6 != 0:
        raise ProtocolError(f"{label} must contain complete six-byte entries.")
    if len(raw) // 6 > 4:
        raise ProtocolError(f"{label} supports at most four entries.")


def _validate_beacons(value: bytes | bytearray) -> None:
    _validate_six_byte_list(value, "beacon_list")


def _validate_trusted_devices(value: bytes | bytearray) -> None:
    _validate_six_byte_list(value, "trusted_device_list")
    raw = bytes(value)
    for index in range(0, len(raw), TRUSTED_DEVICE_BYTES):
        if raw[index : index + TRUSTED_DEVICE_BYTES] == b"\x00" * TRUSTED_DEVICE_BYTES:
            raise ProtocolError("trusted_device_list cannot contain an all-zero configured identifier.")


# Development limits/defaults for numeric settings are explicit simulator
# assumptions because the supplied firmware parser provides field widths but no
# ranges/defaults. They are reported in the final implementation notes.
SETTINGS: tuple[SettingDefinition, ...] = (
    SettingDefinition(
        0x01,
        "heartbeat_interval_seconds",
        TYPE_UINT16,
        1,
        65_535,
        60,
        True,
        fixed_length=2,
        description="Automatic simulator heartbeat interval.",
    ),
    SettingDefinition(
        0x02,
        "lte_update_interval_seconds",
        TYPE_UINT16,
        1,
        65_535,
        480,
        True,
        fixed_length=2,
        description="Persistent LTE update interval from the firmware settings parser.",
    ),
    SettingDefinition(
        0x03,
        "sleep_interval_seconds",
        TYPE_UINT16,
        1,
        65_535,
        480,
        True,
        fixed_length=2,
        description="Persistent sleep interval from the firmware settings parser.",
    ),
    SettingDefinition(
        0x10,
        "safe_zones",
        TYPE_RAW,
        None,
        None,
        b"",
        True,
        validator=_validate_safe_zones,
        description="Zero to four 10-byte latitude_e7/longitude_e7/radius_m records.",
    ),
    SettingDefinition(
        0x11,
        "beacon_list",
        TYPE_RAW,
        None,
        None,
        b"",
        True,
        validator=_validate_beacons,
        description="Zero to four configured static six-byte beacon identifiers.",
    ),
    SettingDefinition(
        0x12,
        "trusted_device_list",
        TYPE_RAW,
        None,
        None,
        bytes.fromhex("AABBCCDDEE01"),
        True,
        validator=_validate_trusted_devices,
        description="Zero to four configured six-byte trusted-device identifiers.",
    ),
)
SETTINGS_BY_ID = {item.setting_id: item for item in SETTINGS}
SETTINGS_BY_NAME = {item.name: item for item in SETTINGS}


def crc16_xmodem(data: bytes, initial_crc: int = 0x0000) -> int:
    crc = initial_crc & 0xFFFF
    for value in data:
        crc = (((crc >> 8) & 0xFF) | ((crc << 8) & 0xFFFF)) & 0xFFFF
        crc ^= value
        crc ^= (crc & 0xFF) >> 4
        crc ^= ((crc << 8) << 4) & 0xFFFF
        crc ^= (((crc & 0xFF) << 4) << 1) & 0xFFFF
        crc &= 0xFFFF
    return crc


def validate_imei(imei: str) -> None:
    if len(imei) != 15 or not imei.isdigit():
        raise ProtocolError("IMEI must contain exactly 15 ASCII decimal digits.")


def build_packet(command: int, payload: bytes, sequence_id: int) -> bytes:
    if not 0 <= command <= 0xFF:
        raise ProtocolError("Command is outside uint8 range.")
    if not 0 <= sequence_id <= 0xFFFF:
        raise ProtocolError("Sequence ID is outside uint16 range.")
    body_length = 1 + len(payload)
    if body_length > 0xFFFF:
        raise ProtocolError("Command plus payload is too large for the Version 7 Length field.")
    crc = crc16_xmodem(payload)
    return (
        bytes((HEADER, PROPERTY))
        + body_length.to_bytes(2, "big")
        + crc.to_bytes(2, "big")
        + sequence_id.to_bytes(2, "big")
        + bytes((command,))
        + payload
    )


def validate_envelope(packet: bytes) -> tuple[int, bytes, list[str]]:
    errors: list[str] = []
    if len(packet) < PAYLOAD_OFFSET:
        return -1, b"", ["Packet is shorter than the 9-byte fixed header."]
    if packet[0] != HEADER:
        errors.append(f"Header is 0x{packet[0]:02X}; expected 0x{HEADER:02X}.")
    if packet[1] != PROPERTY:
        errors.append(f"Property is 0x{packet[1]:02X}; expected 0x{PROPERTY:02X}.")
    declared = int.from_bytes(packet[2:4], "big")
    actual = len(packet) - FIXED_PREFIX_BYTES
    if declared != actual:
        errors.append(f"Length mismatch: declared {declared}, actual {actual} Command+Payload bytes.")
    received_crc = int.from_bytes(packet[4:6], "big")
    calculated_crc = crc16_xmodem(packet[PAYLOAD_OFFSET:])
    if received_crc != calculated_crc:
        errors.append(
            f"CRC mismatch: received 0x{received_crc:04X}, calculated 0x{calculated_crc:04X} over payload."
        )
    return packet[COMMAND_OFFSET], packet[PAYLOAD_OFFSET:], errors


def normalize_six_byte_entries(entries: Sequence[bytes]) -> tuple[bytes, ...]:
    if len(entries) > MAX_TRUSTED_DEVICES:
        raise ProtocolError("At most four trusted devices are supported.")
    normalized: list[bytes] = []
    for index, entry in enumerate(entries):
        raw = bytes(entry)
        if len(raw) != TRUSTED_DEVICE_BYTES:
            raise ProtocolError(f"Trusted-device entry {index + 1} is not six bytes.")
        if raw == b"\x00" * TRUSTED_DEVICE_BYTES:
            raise ProtocolError("A configured trusted-device entry cannot be all zeros.")
        normalized.append(raw)
    return tuple(normalized)


def encode_trusted_extension(entries: Sequence[bytes]) -> bytes:
    normalized = normalize_six_byte_entries(entries)
    slots = list(normalized)
    slots.extend([b"\x00" * TRUSTED_DEVICE_BYTES] * (MAX_TRUSTED_DEVICES - len(slots)))
    return bytes((len(normalized),)) + b"".join(slots)


def decode_trusted_extension(raw: bytes) -> dict[str, Any]:
    if len(raw) != TRUSTED_EXTENSION_BYTES:
        raise ProtocolError(f"Trusted-device extension must be {TRUSTED_EXTENSION_BYTES} bytes.")
    count = raw[0]
    if count > MAX_TRUSTED_DEVICES:
        raise ProtocolError(f"trustedDeviceCount {count} exceeds four.")
    slots = [raw[1 + i * 6 : 1 + (i + 1) * 6] for i in range(MAX_TRUSTED_DEVICES)]
    for index, slot in enumerate(slots):
        if index >= count and slot != b"\x00" * 6:
            raise ProtocolError(f"Unused trusted-device slot {index + 1} is not zero-filled.")
        if index < count and slot == b"\x00" * 6:
            raise ProtocolError(f"Configured trusted-device slot {index + 1} is all zeros.")
    return {
        "count": count,
        "slots": [slot.hex().upper() for slot in slots],
        "configured": [slot.hex().upper() for slot in slots[:count]],
        "unused_zero_filled": all(slot == b"\x00" * 6 for slot in slots[count:]),
    }


def _encode_location_fields(latitude_e6: int, longitude_e6: int, accuracy_x10: int, speed_x10: int) -> bytes:
    if not -90_000_000 <= latitude_e6 <= 90_000_000:
        raise ProtocolError("Latitude is outside -90..90 degrees.")
    if not -180_000_000 <= longitude_e6 <= 180_000_000:
        raise ProtocolError("Longitude is outside -180..180 degrees.")
    if not 0 <= accuracy_x10 <= 0xFFFF or not 0 <= speed_x10 <= 0xFFFF:
        raise ProtocolError("Accuracy and speed must fit uint16.")
    return (
        latitude_e6.to_bytes(4, "big", signed=True)
        + longitude_e6.to_bytes(4, "big", signed=True)
        + accuracy_x10.to_bytes(2, "big")
        + speed_x10.to_bytes(2, "big")
    )


def build_heartbeat_packet(
    *,
    sequence_id: int,
    imei: str,
    timestamp_ms: int,
    battery: int,
    charging: int,
    last_update_minutes: int,
    software_version: int,
    firmware_version: int,
    opcode: int,
    location_data: bytes,
    trusted_devices: Sequence[bytes] = (),
    extended: bool = True,
) -> bytes:
    validate_imei(imei)
    expected_location = 12 if opcode in GPS_OPCODES else 6 if opcode in ADDRESS_OPCODES else None
    if expected_location is None:
        raise ProtocolError(f"Unknown heartbeat opcode 0x{opcode:02X}.")
    if len(location_data) != expected_location:
        raise ProtocolError(f"Opcode 0x{opcode:02X} requires {expected_location} locationData bytes.")
    payload = (
        imei.encode("ascii")
        + timestamp_ms.to_bytes(8, "big")
        + bytes((battery, charging))
        + last_update_minutes.to_bytes(2, "big")
        + bytes((software_version, firmware_version, opcode))
        + location_data
    )
    if extended:
        payload += encode_trusted_extension(trusted_devices)
    return build_packet(COMMAND_HEARTBEAT, payload, sequence_id)


def build_location_packet(
    *,
    sequence_id: int,
    imei: str,
    timestamp_ms: int,
    battery: int,
    charging: int,
    last_update_minutes: int,
    latitude_e6: int,
    longitude_e6: int,
    accuracy_x10: int,
    speed_x10: int,
) -> bytes:
    validate_imei(imei)
    payload = (
        imei.encode("ascii")
        + timestamp_ms.to_bytes(8, "big")
        + bytes((battery, charging))
        + last_update_minutes.to_bytes(2, "big")
        + _encode_location_fields(latitude_e6, longitude_e6, accuracy_x10, speed_x10)
    )
    return build_packet(COMMAND_LOCATION, payload, sequence_id)


def encode_setting_value(definition: SettingDefinition, value: Any) -> bytes:
    definition.validate(value)
    value_type = definition.value_type
    if value_type == TYPE_UINT8:
        encoded = int(value).to_bytes(1, "big")
    elif value_type == TYPE_UINT16:
        encoded = int(value).to_bytes(2, "big")
    elif value_type == TYPE_UINT32:
        encoded = int(value).to_bytes(4, "big")
    elif value_type == TYPE_INT32:
        encoded = int(value).to_bytes(4, "big", signed=True)
    elif value_type == TYPE_BOOL:
        encoded = b"\x01" if value else b"\x00"
    elif value_type == TYPE_RAW:
        encoded = bytes(value)
    elif value_type == TYPE_UTF8:
        encoded = value.encode("utf-8")
    else:
        raise ProtocolError(f"Unsupported value type 0x{value_type:02X}.")
    if definition.fixed_length is not None and len(encoded) != definition.fixed_length:
        raise ProtocolError(f"{definition.name} must encode to {definition.fixed_length} bytes.")
    return encoded


def decode_setting_value(definition: SettingDefinition, raw: bytes) -> Any:
    if definition.fixed_length is not None and len(raw) != definition.fixed_length:
        raise ProtocolError(
            f"{definition.name} has value length {len(raw)}; expected {definition.fixed_length}."
        )
    value_type = definition.value_type
    if value_type == TYPE_UINT8:
        if len(raw) != 1:
            raise ProtocolError("uint8 value length must be 1.")
        value: Any = int.from_bytes(raw, "big")
    elif value_type == TYPE_UINT16:
        if len(raw) != 2:
            raise ProtocolError("uint16 value length must be 2.")
        value = int.from_bytes(raw, "big")
    elif value_type == TYPE_UINT32:
        if len(raw) != 4:
            raise ProtocolError("uint32 value length must be 4.")
        value = int.from_bytes(raw, "big")
    elif value_type == TYPE_INT32:
        if len(raw) != 4:
            raise ProtocolError("int32 value length must be 4.")
        value = int.from_bytes(raw, "big", signed=True)
    elif value_type == TYPE_BOOL:
        if len(raw) != 1 or raw[0] not in (0, 1):
            raise ProtocolError("Boolean value must be one byte 00 or 01.")
        value = raw[0] == 1
    elif value_type == TYPE_RAW:
        value = raw
    elif value_type == TYPE_UTF8:
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Invalid UTF-8 setting value.") from exc
    else:
        raise ProtocolError(f"Unsupported value type 0x{value_type:02X}.")
    definition.validate(value)
    return value


def parse_cli_setting_value(definition: SettingDefinition, text: str) -> Any:
    if definition.value_type in (TYPE_UINT8, TYPE_UINT16, TYPE_UINT32, TYPE_INT32):
        try:
            value: Any = int(text, 10)
        except ValueError as exc:
            raise ProtocolError(f"{definition.name} requires a decimal integer.") from exc
    elif definition.value_type == TYPE_BOOL:
        lowered = text.strip().lower()
        if lowered in {"1", "true", "on", "yes"}:
            value = True
        elif lowered in {"0", "false", "off", "no"}:
            value = False
        else:
            raise ProtocolError(f"{definition.name} requires true/false.")
    elif definition.value_type == TYPE_RAW:
        cleaned = "".join(text.split()).replace(":", "").replace("-", "")
        if len(cleaned) % 2:
            raise ProtocolError("Raw HEX value has an odd number of characters.")
        try:
            value = bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ProtocolError("Raw value must be hexadecimal.") from exc
    else:
        value = text
    definition.validate(value)
    return value


def build_settings_update_packet(
    *,
    target_imei: str,
    update_id: int,
    settings: Mapping[str | int, Any],
    sequence_id: int = 1,
    schema_version: int = SCHEMA_VERSION,
) -> bytes:
    validate_imei(target_imei)
    if not 0 <= update_id <= 0xFFFF:
        raise ProtocolError("Update ID is outside uint16 range.")
    if schema_version != SCHEMA_VERSION:
        raise ProtocolError(f"Only schema version {SCHEMA_VERSION} can be encoded.")
    if not settings or len(settings) > 0xFF:
        raise ProtocolError("A settings update must contain between 1 and 255 entries.")

    entries: list[tuple[SettingDefinition, Any]] = []
    seen: set[int] = set()
    for key, value in settings.items():
        definition = SETTINGS_BY_NAME.get(key) if isinstance(key, str) else SETTINGS_BY_ID.get(key)
        if definition is None:
            raise ProtocolError(f"Unknown setting {key!r}.")
        if definition.setting_id in seen:
            raise ProtocolError(f"Duplicate setting {definition.name}.")
        seen.add(definition.setting_id)
        entries.append((definition, value))

    payload = bytearray(target_imei.encode("ascii"))
    payload.append(schema_version)
    payload.extend(update_id.to_bytes(2, "big"))
    payload.append(len(entries))
    for definition, value in entries:
        raw = encode_setting_value(definition, value)
        payload.extend((definition.setting_id, definition.value_type))
        payload.extend(len(raw).to_bytes(2, "big"))
        payload.extend(raw)
    return build_packet(COMMAND_SETTINGS_UPDATE, bytes(payload), sequence_id)


def decode_settings_payload(payload: bytes, *, target_imei: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    if len(payload) < 19:
        return {"valid": False, "errors": ["Settings payload is shorter than 19-byte fixed portion."]}
    imei_raw = payload[:15]
    try:
        imei = imei_raw.decode("ascii")
    except UnicodeDecodeError:
        imei = ""
    if len(imei) != 15 or not imei.isdigit():
        errors.append("Target IMEI is not exactly 15 ASCII decimal digits.")
    if target_imei is not None and imei != target_imei:
        errors.append(f"Target IMEI {imei!r} does not match device IMEI {target_imei!r}.")
    schema = payload[15]
    if schema != SCHEMA_VERSION:
        errors.append(f"Unsupported settings schema version 0x{schema:02X}.")
    update_id = int.from_bytes(payload[16:18], "big")
    entry_count = payload[18]
    offset = 19
    entries: list[dict[str, Any]] = []
    seen: set[int] = set()

    if entry_count == 0:
        errors.append("Settings update must contain at least one TLV entry.")

    for index in range(entry_count):
        if offset + 4 > len(payload):
            errors.append(f"TLV entry {index + 1} is truncated before its four-byte header.")
            break
        setting_id = payload[offset]
        value_type = payload[offset + 1]
        value_length = int.from_bytes(payload[offset + 2 : offset + 4], "big")
        offset += 4
        if offset + value_length > len(payload):
            errors.append(
                f"TLV entry {index + 1} declares {value_length} value bytes but is truncated."
            )
            break
        raw = payload[offset : offset + value_length]
        offset += value_length
        definition = SETTINGS_BY_ID.get(setting_id)
        entry: dict[str, Any] = {
            "setting_id": setting_id,
            "setting_name": definition.name if definition else None,
            "value_type": value_type,
            "value_type_name": VALUE_TYPE_NAMES.get(value_type, "unknown"),
            "value_length": value_length,
            "raw_hex": raw.hex().upper(),
            "decoded_value": None,
            "valid": True,
            "error": None,
        }
        if setting_id in seen:
            entry["valid"] = False
            entry["error"] = "Duplicate setting ID."
            errors.append(f"Duplicate setting ID 0x{setting_id:02X}.")
        seen.add(setting_id)
        if definition is None:
            entry["valid"] = False
            entry["error"] = "Unknown setting ID."
            errors.append(f"Unknown setting ID 0x{setting_id:02X}.")
        elif value_type != definition.value_type:
            entry["valid"] = False
            entry["error"] = (
                f"Value type 0x{value_type:02X} does not match registry type "
                f"0x{definition.value_type:02X}."
            )
            errors.append(f"{definition.name} uses an incorrect value type.")
        else:
            try:
                decoded = decode_setting_value(definition, raw)
                entry["decoded_value"] = (
                    decoded.hex().upper() if isinstance(decoded, (bytes, bytearray)) else decoded
                )
                if definition.name == "safe_zones":
                    entry["record_count"] = len(raw) // SAFE_ZONE_BYTES
                elif definition.name in {"beacon_list", "trusted_device_list"}:
                    entry["record_count"] = len(raw) // 6
            except ProtocolError as exc:
                entry["valid"] = False
                entry["error"] = str(exc)
                errors.append(f"{definition.name}: {exc}")
        entries.append(entry)

    if len(entries) != entry_count and not any("truncated" in error for error in errors):
        errors.append(f"Entry count is {entry_count}, but only {len(entries)} entries were decoded.")
    if offset < len(payload):
        errors.append(f"Settings payload has {len(payload) - offset} unexpected trailing byte(s).")

    return {
        "valid": not errors,
        "errors": errors,
        "target_imei": imei,
        "schema_version": schema,
        "update_id": update_id,
        "entry_count": entry_count,
        "entries": entries,
        "bytes_consumed": offset,
    }


def decode_application_packet(packet: bytes, *, target_imei: str | None = None) -> dict[str, Any]:
    command, payload, errors = validate_envelope(packet)
    result: dict[str, Any] = {
        "valid": False,
        "errors": list(errors),
        "packet_hex": packet.hex().upper(),
        "packet_length": len(packet),
        "header": packet[0] if packet else None,
        "property": packet[1] if len(packet) > 1 else None,
        "declared_length": int.from_bytes(packet[2:4], "big") if len(packet) >= 4 else None,
        "received_crc": int.from_bytes(packet[4:6], "big") if len(packet) >= 6 else None,
        "calculated_crc": crc16_xmodem(payload) if len(packet) >= PAYLOAD_OFFSET else None,
        "sequence_id": int.from_bytes(packet[6:8], "big") if len(packet) >= 8 else None,
        "command": command,
        "command_name": {
            COMMAND_HEARTBEAT: "heartbeat",
            COMMAND_LOCATION: "LTE-M location",
            COMMAND_SETTINGS_UPDATE: "settings update",
        }.get(command, "unknown"),
    }
    if errors:
        return result

    if command == COMMAND_SETTINGS_UPDATE:
        settings = decode_settings_payload(payload, target_imei=target_imei)
        result["settings_update"] = settings
        result["imei"] = settings.get("target_imei")
        result["errors"].extend(settings["errors"])
    elif command in (COMMAND_HEARTBEAT, COMMAND_LOCATION):
        common = _decode_common_device_payload(payload, heartbeat=command == COMMAND_HEARTBEAT)
        result.update(common)
        result["errors"].extend(common.get("errors", []))
    else:
        result["errors"].append(f"Unknown command 0x{command:02X}.")

    result["valid"] = not result["errors"]
    return result


def _decode_common_device_payload(payload: bytes, *, heartbeat: bool) -> dict[str, Any]:
    errors: list[str] = []
    minimum = 27 + (3 if heartbeat else 12)
    if len(payload) < minimum:
        return {"errors": [f"Payload is too short for {'heartbeat' if heartbeat else 'location'} fields."]}
    imei_raw = payload[:15]
    try:
        imei = imei_raw.decode("ascii")
    except UnicodeDecodeError:
        imei = ""
    if len(imei) != 15 or not imei.isdigit():
        errors.append("IMEI is not exactly 15 ASCII decimal digits.")
    timestamp_ms = int.from_bytes(payload[15:23], "big")
    battery = payload[23]
    charging = payload[24]
    last_update = int.from_bytes(payload[25:27], "big")
    if battery not in BATTERY_NAMES:
        errors.append(f"Unsupported battery value 0x{battery:02X}.")
    if charging not in CHARGING_NAMES:
        errors.append(f"Unsupported charging value 0x{charging:02X}.")
    out: dict[str, Any] = {
        "errors": errors,
        "imei": imei,
        "timestamp_ms": timestamp_ms,
        "battery": battery,
        "battery_name": BATTERY_NAMES.get(battery, "unknown"),
        "charging": charging,
        "charging_name": CHARGING_NAMES.get(charging, "unknown"),
        "last_update_minutes": last_update,
    }
    offset = 27
    if heartbeat:
        if len(payload) < offset + 3:
            errors.append("Heartbeat payload is truncated before version/opcode fields.")
            return out
        out["software_version"] = payload[offset]
        out["firmware_version"] = payload[offset + 1]
        opcode = payload[offset + 2]
        out["opcode"] = opcode
        out["opcode_name"] = OPCODE_NAMES.get(opcode, "unknown")
        offset += 3
        if opcode in ADDRESS_OPCODES:
            needed = 6
            if len(payload) < offset + needed:
                errors.append("Heartbeat address locationData is truncated.")
                return out
            location = payload[offset : offset + needed]
            out["location_data_hex"] = location.hex().upper()
            out["location_data_kind"] = "beacon" if opcode == OPCODE_BEACON else "trusted_device_detected"
            offset += needed
        elif opcode in GPS_OPCODES:
            if len(payload) < offset + 12:
                errors.append("Heartbeat GPS locationData is truncated.")
                return out
            location_fields, location_errors = _decode_location_bytes(payload[offset : offset + 12])
            out.update(location_fields)
            errors.extend(location_errors)
            out["location_data_kind"] = "gps"
            offset += 12
        else:
            errors.append(f"Unknown heartbeat opcode 0x{opcode:02X}.")
            return out

        remaining = len(payload) - offset
        if remaining == 0:
            out["heartbeat_format"] = "original_v7"
            out["trusted_device_registry"] = None
        elif remaining == TRUSTED_EXTENSION_BYTES:
            try:
                out["trusted_device_registry"] = decode_trusted_extension(payload[offset:])
                out["heartbeat_format"] = "extended_v7"
            except ProtocolError as exc:
                errors.append(str(exc))
        else:
            errors.append(
                f"Heartbeat has {remaining} bytes after locationData; expected 0 or {TRUSTED_EXTENSION_BYTES}."
            )
    else:
        if len(payload) != offset + 12:
            errors.append(f"Location payload length is {len(payload)}; expected {offset + 12}.")
        elif len(payload) >= offset + 12:
            location_fields, location_errors = _decode_location_bytes(payload[offset : offset + 12])
            out.update(location_fields)
            errors.extend(location_errors)
    return out


def _decode_location_bytes(raw: bytes) -> tuple[dict[str, Any], list[str]]:
    lat = int.from_bytes(raw[0:4], "big", signed=True)
    lon = int.from_bytes(raw[4:8], "big", signed=True)
    accuracy = int.from_bytes(raw[8:10], "big")
    speed = int.from_bytes(raw[10:12], "big")
    errors: list[str] = []
    if not -90_000_000 <= lat <= 90_000_000:
        errors.append(f"Latitude {lat / COORD_SCALE} is outside -90..90 degrees.")
    if not -180_000_000 <= lon <= 180_000_000:
        errors.append(f"Longitude {lon / COORD_SCALE} is outside -180..180 degrees.")
    out = {
        "latitude_e6": lat,
        "longitude_e6": lon,
        "latitude": lat / COORD_SCALE,
        "longitude": lon / COORD_SCALE,
        "accuracy_x10": accuracy,
        "accuracy_m": accuracy / 10.0,
        "speed_x10": speed,
        "speed_mps": speed / 10.0,
    }
    return out, errors


def default_settings() -> dict[str, Any]:
    return {item.name: item.default for item in SETTINGS if item.persistent}


def values_from_decoded_settings(decoded: Mapping[str, Any]) -> dict[str, Any]:
    if not decoded.get("valid"):
        raise ProtocolError("Cannot extract values from an invalid settings update.")
    values: dict[str, Any] = {}
    for entry in decoded["entries"]:
        definition = SETTINGS_BY_ID[entry["setting_id"]]
        raw = bytes.fromhex(entry["raw_hex"])
        values[definition.name] = decode_setting_value(definition, raw)
    return values
