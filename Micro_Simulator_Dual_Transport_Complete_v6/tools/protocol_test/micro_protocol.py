#!/usr/bin/env python3
"""Canonical Micro Data Packet Version 8 protocol helpers.

The module deliberately implements one cross-team configuration format:
command 0x02 with a positional, full-replacement payload. The former command
0x20 TLV format is not accepted or emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

HEADER = 0xAB
PROPERTY = 0x10
COMMAND_HEARTBEAT = 0x01
COMMAND_CONFIGURATION_UPDATE = 0x02
# Compatibility import name for the simulator/server code. Its value is canonical 0x02.
COMMAND_SETTINGS_UPDATE = COMMAND_CONFIGURATION_UPDATE
COMMAND_LOCATION = 0x10
COMMAND_OFFSET = 8
PAYLOAD_OFFSET = 9
FIXED_PREFIX_BYTES = 8

COORD_SCALE = 1_000_000
MAX_TRUSTED_DEVICES = 4
TRUSTED_DEVICE_BYTES = 6
TRUSTED_EXTENSION_BYTES = 1 + (MAX_TRUSTED_DEVICES * TRUSTED_DEVICE_BYTES)
MAX_SAFE_ZONES = 4
SAFE_ZONE_BYTES = 10
MAX_BEACONS = 4
BEACON_BYTES = 6
CONFIG_FIXED_PAYLOAD_BYTES = 27

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


@dataclass(frozen=True)
class SettingDefinition:
    name: str
    kind: str
    description: str


SETTINGS: tuple[SettingDefinition, ...] = (
    SettingDefinition("heartbeat_interval_seconds", "uint16_nonzero", "Base automatic heartbeat interval."),
    SettingDefinition("lte_update_interval_seconds", "uint16_nonzero", "LTE location-update interval while actively outside."),
    SettingDefinition("ble_check_interval_seconds", "uint16_nonzero", "Local BLE/GNSS context reevaluation interval."),
    SettingDefinition("safe_zones", "safe_zone_list", "Zero to four 10-byte latitude_e6/longitude_e6/radius_m records."),
    SettingDefinition("beacon_list", "six_byte_list", "Zero to four static six-byte beacon identifiers."),
    SettingDefinition("trusted_device_list", "six_byte_list", "Zero to four trusted-device identifiers."),
    SettingDefinition("sending_update", "sending_update", "0x00 or 0xFF firmware-update indication."),
)
SETTINGS_BY_NAME = {item.name: item for item in SETTINGS}
# There are no settings IDs in canonical command-0x02. Keep this empty export
# so old tools fail closed instead of inventing a TLV interpretation.
SETTINGS_BY_ID: dict[int, SettingDefinition] = {}


def crc16_xmodem(data: bytes, initial_crc: int = 0x0000) -> int:
    crc = initial_crc & 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def validate_imei(imei: str) -> None:
    if len(imei) != 15 or not imei.isdigit():
        raise ProtocolError("IMEI must contain exactly 15 ASCII decimal digits.")


def build_packet(command: int, payload: bytes, sequence_id: int) -> bytes:
    if not 0 <= command <= 0xFF:
        raise ProtocolError("Command is outside uint8 range.")
    if not 0 <= sequence_id <= 0xFFFF:
        raise ProtocolError("Sequence ID is outside uint16 range.")
    length = 1 + len(payload)
    if length > 0xFFFF:
        raise ProtocolError("Command plus payload is too large for the Length field.")
    return (
        bytes((HEADER, PROPERTY))
        + length.to_bytes(2, "big")
        + crc16_xmodem(payload).to_bytes(2, "big")
        + sequence_id.to_bytes(2, "big")
        + bytes((command,))
        + payload
    )


def validate_envelope(packet: bytes) -> tuple[int, bytes, list[str]]:
    if len(packet) < PAYLOAD_OFFSET:
        return -1, b"", ["Packet is shorter than the 9-byte fixed envelope."]
    errors: list[str] = []
    if packet[0] != HEADER:
        errors.append(f"Header is 0x{packet[0]:02X}; expected 0xAB.")
    if packet[1] != PROPERTY:
        errors.append(f"Property is 0x{packet[1]:02X}; expected 0x10.")
    declared = int.from_bytes(packet[2:4], "big")
    actual = len(packet) - FIXED_PREFIX_BYTES
    if declared != actual:
        errors.append(f"Length mismatch: declared {declared}, actual {actual} Command+Payload bytes.")
    expected_crc = crc16_xmodem(packet[PAYLOAD_OFFSET:])
    received_crc = int.from_bytes(packet[4:6], "big")
    if received_crc != expected_crc:
        errors.append(f"CRC mismatch: received 0x{received_crc:04X}, calculated 0x{expected_crc:04X} over payload.")
    return packet[COMMAND_OFFSET], packet[PAYLOAD_OFFSET:], errors


def _validate_six_byte_entries(entries: Sequence[bytes], label: str) -> tuple[bytes, ...]:
    if len(entries) > 4:
        raise ProtocolError(f"{label} supports at most four entries.")
    normalized: list[bytes] = []
    for index, entry in enumerate(entries, 1):
        raw = bytes(entry)
        if len(raw) != 6:
            raise ProtocolError(f"{label} entry {index} is not six bytes.")
        if label == "trusted_device_list" and raw == b"\x00" * 6:
            raise ProtocolError("A configured trusted-device identifier cannot be all zeros.")
        normalized.append(raw)
    return tuple(normalized)


def normalize_six_byte_entries(entries: Sequence[bytes]) -> tuple[bytes, ...]:
    return _validate_six_byte_entries(entries, "trusted_device_list")


def encode_trusted_extension(entries: Sequence[bytes]) -> bytes:
    normalized = normalize_six_byte_entries(entries)
    return bytes((len(normalized),)) + b"".join(
        normalized + (b"\x00" * 6,) * (MAX_TRUSTED_DEVICES - len(normalized))
    )


def decode_trusted_extension(raw: bytes) -> dict[str, Any]:
    if len(raw) != TRUSTED_EXTENSION_BYTES:
        raise ProtocolError(f"Trusted-device registry must be {TRUSTED_EXTENSION_BYTES} bytes.")
    count = raw[0]
    if count > MAX_TRUSTED_DEVICES:
        raise ProtocolError(f"trustedDeviceCount {count} exceeds four.")
    slots = [raw[1 + index * 6 : 7 + index * 6] for index in range(4)]
    for index, slot in enumerate(slots):
        if index < count and slot == b"\x00" * 6:
            raise ProtocolError(f"Configured trusted-device slot {index + 1} is all zeros.")
        if index >= count and slot != b"\x00" * 6:
            raise ProtocolError(f"Unused trusted-device slot {index + 1} is not zero-filled.")
    return {
        "count": count,
        "slots": [slot.hex().upper() for slot in slots],
        "configured": [slot.hex().upper() for slot in slots[:count]],
        "unused_zero_filled": True,
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


def build_heartbeat_packet(*, sequence_id: int, imei: str, timestamp_ms: int, battery: int,
                           charging: int, last_update_minutes: int, software_version: int,
                           firmware_version: int, opcode: int, location_data: bytes,
                           trusted_devices: Sequence[bytes] = (), extended: bool = True) -> bytes:
    validate_imei(imei)
    if not extended:
        raise ProtocolError("Canonical Version 7 heartbeats always include the trusted-device registry.")
    expected_length = 12 if opcode in GPS_OPCODES else 6 if opcode in ADDRESS_OPCODES else None
    if expected_length is None or len(location_data) != expected_length:
        raise ProtocolError(f"Opcode 0x{opcode:02X} requires {expected_length} locationData bytes.")
    if not 0 <= timestamp_ms <= 0xFFFFFFFFFFFFFFFF:
        raise ProtocolError("Timestamp is outside uint64 range.")
    payload = (
        imei.encode("ascii") + timestamp_ms.to_bytes(8, "big") + bytes((battery, charging))
        + last_update_minutes.to_bytes(2, "big")
        + bytes((software_version, firmware_version, opcode)) + location_data
        + encode_trusted_extension(trusted_devices)
    )
    return build_packet(COMMAND_HEARTBEAT, payload, sequence_id)


def build_location_packet(*, sequence_id: int, imei: str, timestamp_ms: int, battery: int,
                          charging: int, last_update_minutes: int, latitude_e6: int,
                          longitude_e6: int, accuracy_x10: int, speed_x10: int) -> bytes:
    validate_imei(imei)
    payload = (
        imei.encode("ascii") + timestamp_ms.to_bytes(8, "big") + bytes((battery, charging))
        + last_update_minutes.to_bytes(2, "big")
        + _encode_location_fields(latitude_e6, longitude_e6, accuracy_x10, speed_x10)
    )
    return build_packet(COMMAND_LOCATION, payload, sequence_id)


def _raw_list(value: Any, label: str) -> tuple[bytes, ...]:
    raw = bytes(value)
    if len(raw) % 6 or len(raw) // 6 > 4:
        raise ProtocolError(f"{label} must contain zero to four complete six-byte entries.")
    return _validate_six_byte_entries([raw[offset : offset + 6] for offset in range(0, len(raw), 6)], label)


def _safe_zone_records(raw_value: Any) -> tuple[tuple[int, int, int], ...]:
    raw = bytes(raw_value)
    if len(raw) % SAFE_ZONE_BYTES or len(raw) // SAFE_ZONE_BYTES > 4:
        raise ProtocolError("safe_zones must contain zero to four complete 10-byte records.")
    records: list[tuple[int, int, int]] = []
    for offset in range(0, len(raw), SAFE_ZONE_BYTES):
        lat = int.from_bytes(raw[offset : offset + 4], "big", signed=True)
        lon = int.from_bytes(raw[offset + 4 : offset + 8], "big", signed=True)
        radius = int.from_bytes(raw[offset + 8 : offset + 10], "big")
        if not -90_000_000 <= lat <= 90_000_000 or not -180_000_000 <= lon <= 180_000_000 or radius == 0:
            raise ProtocolError("safe-zone coordinate or radius is outside the canonical range.")
        records.append((lat, lon, radius))
    return tuple(records)


def default_settings() -> dict[str, Any]:
    return {
        "heartbeat_interval_seconds": 60,
        "lte_update_interval_seconds": 480,
        "ble_check_interval_seconds": 480,
        "safe_zones": b"",
        "beacon_list": b"",
        "trusted_device_list": bytes.fromhex("AABBCCDDEE01"),
        "sending_update": 0x00,
    }


def _normalize_config(values: Mapping[str, Any]) -> dict[str, Any]:
    # The exact two bytes formerly named sleepIntervalSeconds are now the
    # BLE-check interval.  Accept the old input spelling only as a local-tool
    # compatibility alias; all decoded and generated canonical data uses BLE.
    values = dict(values)
    if "sleep_interval_seconds" in values:
        if "ble_check_interval_seconds" in values:
            raise ProtocolError("Use either ble_check_interval_seconds or legacy sleep_interval_seconds, not both.")
        values["ble_check_interval_seconds"] = values.pop("sleep_interval_seconds")
    required = set(SETTINGS_BY_NAME)
    missing = required - set(values)
    unexpected = set(values) - required
    if missing or unexpected:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            detail.append("unexpected " + ", ".join(sorted(unexpected)))
        raise ProtocolError("Full-replacement configuration requires every field: " + "; ".join(detail))
    normalized = dict(values)
    for name in ("heartbeat_interval_seconds", "lte_update_interval_seconds", "ble_check_interval_seconds"):
        value = normalized[name]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xFFFF:
            raise ProtocolError(f"{name} must fit uint16.")
    for name in ("heartbeat_interval_seconds", "lte_update_interval_seconds", "ble_check_interval_seconds"):
        if normalized[name] == 0:
            raise ProtocolError(f"{name} must be 1..65535.")
    flag = normalized["sending_update"]
    if flag not in (0x00, 0xFF):
        raise ProtocolError("sending_update must be exactly 0x00 or 0xFF.")
    zones = _safe_zone_records(normalized["safe_zones"])
    beacons = _raw_list(normalized["beacon_list"], "beacon_list")
    trusted = _raw_list(normalized["trusted_device_list"], "trusted_device_list")
    normalized["safe_zones"] = b"".join(
        lat.to_bytes(4, "big", signed=True) + lon.to_bytes(4, "big", signed=True) + radius.to_bytes(2, "big")
        for lat, lon, radius in zones
    )
    normalized["beacon_list"] = b"".join(beacons)
    normalized["trusted_device_list"] = b"".join(trusted)
    return normalized


def build_configuration_update_packet(*, target_imei: str, update_id: int,
                                      configuration: Mapping[str, Any], sequence_id: int = 1) -> bytes:
    validate_imei(target_imei)
    if not 0 <= update_id <= 0xFFFF:
        raise ProtocolError("Update ID is outside uint16 range.")
    config = _normalize_config(configuration)
    payload = bytearray(target_imei.encode("ascii"))
    payload.extend(update_id.to_bytes(2, "big"))
    zones = config["safe_zones"]
    beacons = config["beacon_list"]
    trusted = config["trusted_device_list"]
    payload.append(len(zones) // SAFE_ZONE_BYTES); payload.extend(zones)
    payload.append(len(beacons) // BEACON_BYTES); payload.extend(beacons)
    payload.append(len(trusted) // TRUSTED_DEVICE_BYTES); payload.extend(trusted)
    payload.extend(config["heartbeat_interval_seconds"].to_bytes(2, "big"))
    payload.extend(config["lte_update_interval_seconds"].to_bytes(2, "big"))
    payload.extend(config["ble_check_interval_seconds"].to_bytes(2, "big"))
    payload.append(config["sending_update"])
    return build_packet(COMMAND_CONFIGURATION_UPDATE, bytes(payload), sequence_id)


def build_settings_update_packet(*, target_imei: str, update_id: int,
                                 settings: Mapping[str, Any], sequence_id: int = 1, **_: Any) -> bytes:
    """Compatibility name for the canonical command-0x02 full replacement builder."""
    return build_configuration_update_packet(
        target_imei=target_imei, update_id=update_id, configuration=settings, sequence_id=sequence_id
    )


def decode_settings_payload(payload: bytes, *, target_imei: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    if len(payload) < CONFIG_FIXED_PAYLOAD_BYTES:
        return {"valid": False, "errors": ["Configuration payload is shorter than 27 bytes."]}
    try:
        imei = payload[:15].decode("ascii")
    except UnicodeDecodeError:
        imei = ""
    if len(imei) != 15 or not imei.isdigit():
        errors.append("Target IMEI is not exactly 15 ASCII decimal digits.")
    if target_imei is not None and imei != target_imei:
        errors.append(f"Target IMEI {imei!r} does not match device IMEI {target_imei!r}.")
    update_id = int.from_bytes(payload[15:17], "big")
    cursor = 17

    def take_records(count_name: str, record_size: int, maximum: int) -> tuple[int, bytes]:
        nonlocal cursor
        if cursor >= len(payload):
            errors.append(f"Missing {count_name}.")
            return 0, b""
        count = payload[cursor]; cursor += 1
        if count > maximum:
            errors.append(f"{count_name} {count} exceeds four.")
        needed = count * record_size
        if cursor + needed > len(payload):
            errors.append(f"Truncated {count_name} record.")
            raw = payload[cursor:]
            cursor = len(payload)
            return count, raw
        raw = payload[cursor : cursor + needed]
        cursor += needed
        return count, raw

    zone_count, zones_raw = take_records("gpsSafeZoneCount", SAFE_ZONE_BYTES, MAX_SAFE_ZONES)
    beacon_count, beacons_raw = take_records("beaconCount", BEACON_BYTES, MAX_BEACONS)
    trusted_count, trusted_raw = take_records("trustedDeviceCount", TRUSTED_DEVICE_BYTES, MAX_TRUSTED_DEVICES)
    if cursor + 7 > len(payload):
        errors.append("Configuration update is truncated before interval and SendingUpdate fields.")
        tail = b""
    else:
        tail = payload[cursor : cursor + 7]
        cursor += 7
    if cursor != len(payload):
        errors.append(f"Configuration payload has {len(payload) - cursor} unexpected trailing byte(s).")
    heartbeat = int.from_bytes(tail[0:2], "big") if len(tail) == 7 else None
    lte = int.from_bytes(tail[2:4], "big") if len(tail) == 7 else None
    ble_check = int.from_bytes(tail[4:6], "big") if len(tail) == 7 else None
    flag = tail[6] if len(tail) == 7 else None
    if heartbeat == 0:
        errors.append("heartbeatIntervalSeconds must be 1..65535.")
    if lte == 0:
        errors.append("LTEupdateIntervalSeconds must be 1..65535.")
    if ble_check == 0:
        errors.append("bleCheckIntervalSeconds must be 1..65535.")
    if flag is not None and flag not in (0x00, 0xFF):
        errors.append("SendingUpdate must be 0x00 or 0xFF.")
    try:
        zone_records = _safe_zone_records(zones_raw)
    except ProtocolError as exc:
        errors.append(str(exc)); zone_records = ()
    try:
        beacon_entries = _raw_list(beacons_raw, "beacon_list")
    except ProtocolError as exc:
        errors.append(str(exc)); beacon_entries = ()
    try:
        trusted_entries = _raw_list(trusted_raw, "trusted_device_list")
    except ProtocolError as exc:
        errors.append(str(exc)); trusted_entries = ()
    configuration = {
        "heartbeat_interval_seconds": heartbeat,
        "lte_update_interval_seconds": lte,
        "ble_check_interval_seconds": ble_check,
        "safe_zones": zones_raw,
        "beacon_list": beacons_raw,
        "trusted_device_list": trusted_raw,
        "sending_update": flag,
    }
    return {
        "valid": not errors,
        "errors": errors,
        "target_imei": imei,
        "update_id": update_id,
        "gps_safe_zone_count": zone_count,
        "beacon_count": beacon_count,
        "trusted_device_count": trusted_count,
        "safe_zones": [
            {"latitude_e6": lat, "longitude_e6": lon, "latitude": lat / COORD_SCALE,
             "longitude": lon / COORD_SCALE, "radius_m": radius}
            for lat, lon, radius in zone_records
        ],
        "beacons": [entry.hex().upper() for entry in beacon_entries],
        "trusted_devices": [entry.hex().upper() for entry in trusted_entries],
        "heartbeat_interval_seconds": heartbeat,
        "lte_update_interval_seconds": lte,
        "ble_check_interval_seconds": ble_check,
        "sending_update": flag,
        "configuration": configuration,
        "bytes_consumed": cursor,
    }


def decode_application_packet(packet: bytes, *, target_imei: str | None = None) -> dict[str, Any]:
    command, payload, errors = validate_envelope(packet)
    result: dict[str, Any] = {
        "valid": False, "errors": list(errors), "packet_hex": packet.hex().upper(), "packet_length": len(packet),
        "header": packet[0] if packet else None, "property": packet[1] if len(packet) > 1 else None,
        "declared_length": int.from_bytes(packet[2:4], "big") if len(packet) >= 4 else None,
        "received_crc": int.from_bytes(packet[4:6], "big") if len(packet) >= 6 else None,
        "calculated_crc": crc16_xmodem(payload) if len(packet) >= PAYLOAD_OFFSET else None,
        "sequence_id": int.from_bytes(packet[6:8], "big") if len(packet) >= 8 else None,
        "command": command,
        "command_name": {COMMAND_HEARTBEAT: "heartbeat", COMMAND_CONFIGURATION_UPDATE: "configuration update",
                         COMMAND_LOCATION: "LTE-M location"}.get(command, "unknown"),
    }
    if errors:
        return result
    if command == COMMAND_CONFIGURATION_UPDATE:
        update = decode_settings_payload(payload, target_imei=target_imei)
        result["settings_update"] = update
        result["configuration_update"] = update
        result["imei"] = update.get("target_imei")
        result["errors"].extend(update["errors"])
    elif command in (COMMAND_HEARTBEAT, COMMAND_LOCATION):
        common = _decode_common_device_payload(payload, heartbeat=command == COMMAND_HEARTBEAT)
        result.update(common); result["errors"].extend(common.get("errors", []))
    else:
        result["errors"].append(f"Unknown command 0x{command:02X}.")
    result["valid"] = not result["errors"]
    return result


def _decode_location_bytes(raw: bytes) -> tuple[dict[str, Any], list[str]]:
    lat = int.from_bytes(raw[0:4], "big", signed=True)
    lon = int.from_bytes(raw[4:8], "big", signed=True)
    accuracy = int.from_bytes(raw[8:10], "big")
    speed = int.from_bytes(raw[10:12], "big")
    errors = []
    if not -90_000_000 <= lat <= 90_000_000: errors.append(f"Latitude {lat / COORD_SCALE} is outside -90..90 degrees.")
    if not -180_000_000 <= lon <= 180_000_000: errors.append(f"Longitude {lon / COORD_SCALE} is outside -180..180 degrees.")
    return {"latitude_e6": lat, "longitude_e6": lon, "latitude": lat / COORD_SCALE,
            "longitude": lon / COORD_SCALE, "accuracy_x10": accuracy, "accuracy_m": accuracy / 10.0,
            "speed_x10": speed, "speed_mps": speed / 10.0}, errors


def _decode_common_device_payload(payload: bytes, *, heartbeat: bool) -> dict[str, Any]:
    errors: list[str] = []
    minimum = 27 + (3 if heartbeat else 12)
    if len(payload) < minimum:
        return {"errors": ["Payload is too short for the required device fields."]}
    try: imei = payload[:15].decode("ascii")
    except UnicodeDecodeError: imei = ""
    if len(imei) != 15 or not imei.isdigit(): errors.append("IMEI is not exactly 15 ASCII decimal digits.")
    timestamp_ms = int.from_bytes(payload[15:23], "big")
    battery, charging = payload[23], payload[24]
    if battery not in BATTERY_NAMES: errors.append(f"Unsupported battery value 0x{battery:02X}.")
    if charging not in CHARGING_NAMES: errors.append(f"Unsupported charging value 0x{charging:02X}.")
    out: dict[str, Any] = {"errors": errors, "imei": imei, "timestamp_ms": timestamp_ms,
                           "battery": battery, "battery_name": BATTERY_NAMES.get(battery, "unknown"),
                           "charging": charging, "charging_name": CHARGING_NAMES.get(charging, "unknown"),
                           "last_update_minutes": int.from_bytes(payload[25:27], "big")}
    if not heartbeat:
        if len(payload) != 39: errors.append(f"Location payload length is {len(payload)}; expected 39.")
        elif len(payload) >= 39:
            location, location_errors = _decode_location_bytes(payload[27:39]); out.update(location); errors.extend(location_errors)
        return out
    out.update({"software_version": payload[27], "firmware_version": payload[28], "opcode": payload[29]})
    opcode = payload[29]; out["opcode_name"] = OPCODE_NAMES.get(opcode, "unknown")
    offset = 30
    size = 12 if opcode in GPS_OPCODES else 6 if opcode in ADDRESS_OPCODES else 0
    if size == 0:
        errors.append(f"Unknown heartbeat opcode 0x{opcode:02X}."); return out
    if len(payload) < offset + size + TRUSTED_EXTENSION_BYTES:
        errors.append("Heartbeat is truncated before the required trusted-device registry."); return out
    location_raw = payload[offset:offset + size]
    if opcode in GPS_OPCODES:
        location, location_errors = _decode_location_bytes(location_raw); out.update(location); errors.extend(location_errors); out["location_data_kind"] = "gps"
    else:
        out["location_data_hex"] = location_raw.hex().upper(); out["location_data_kind"] = "beacon" if opcode == OPCODE_BEACON else "trusted_device_detected"
    offset += size
    if len(payload) != offset + TRUSTED_EXTENSION_BYTES:
        errors.append(f"Heartbeat payload length is {len(payload)}; expected {offset + TRUSTED_EXTENSION_BYTES}."); return out
    try:
        out["trusted_device_registry"] = decode_trusted_extension(payload[offset:])
        out["heartbeat_format"] = "canonical_v7"
    except ProtocolError as exc:
        errors.append(str(exc))
    return out


def parse_cli_setting_value(definition: SettingDefinition, text: str) -> Any:
    if definition.kind in {"uint16", "uint16_nonzero"}:
        try: value = int(text, 10)
        except ValueError as exc: raise ProtocolError(f"{definition.name} requires a decimal integer.") from exc
        if not 0 <= value <= 0xFFFF or (definition.kind == "uint16_nonzero" and value == 0):
            raise ProtocolError(f"{definition.name} is outside its valid range.")
        return value
    if definition.kind == "sending_update":
        try: value = int(text, 0)
        except ValueError:
            if text.strip().upper() == "FF": value = 0xFF
            elif text.strip() == "00": value = 0
            else: raise ProtocolError("sending_update must be 00 or FF.")
        if value not in (0, 0xFF): raise ProtocolError("sending_update must be 00 or FF.")
        return value
    cleaned = "".join(text.split()).replace(":", "").replace("-", "")
    try: raw = bytes.fromhex(cleaned)
    except ValueError as exc: raise ProtocolError(f"{definition.name} must be hexadecimal.") from exc
    if definition.kind == "safe_zone_list": _safe_zone_records(raw)
    else: _raw_list(raw, definition.name)
    return raw


def values_from_decoded_settings(decoded: Mapping[str, Any]) -> dict[str, Any]:
    if not decoded.get("valid"):
        raise ProtocolError("Cannot extract values from an invalid configuration update.")
    return dict(decoded["configuration"])
