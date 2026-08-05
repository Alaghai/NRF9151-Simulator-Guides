#!/usr/bin/env python3
"""Decode and validate Micro Version 7 application packets.

Supported commands:
- 0x01 original or extended heartbeat
- 0x10 LTE-M location
- 0x20 TLV settings update
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from micro_protocol import (
    COMMAND_HEARTBEAT,
    COMMAND_LOCATION,
    COMMAND_SETTINGS_UPDATE,
    HEADER,
    PAYLOAD_OFFSET,
    PROPERTY,
    SETTINGS_BY_ID,
    crc16_xmodem,
    decode_application_packet,
)

TIMESTAMP_EPOCH = "1970-01-01T00:00:00Z"


@dataclass
class Field:
    offset: str
    name: str
    raw_hex: str
    decoded: Any


@dataclass
class DecodeResult:
    valid: bool
    packet_hex: str
    packet_bytes: int
    fields: list[Field]
    errors: list[str]
    warnings: list[str]
    diagnostics: list[str]
    summary: dict[str, Any]

    def json_ready(self) -> dict[str, Any]:
        return asdict(self)


def unix_ms_to_iso8601(timestamp_ms: int) -> str | None:
    if timestamp_ms == 0:
        return None
    seconds, milliseconds = divmod(timestamp_ms, 1000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def iso8601_to_unix_ms(text: str) -> int:
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include Z or an explicit UTC offset.")
    result = int(round(dt.timestamp() * 1000))
    if not 0 <= result <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("Timestamp is outside uint64 range.")
    return result


def normalize_hex_input(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("No packet was supplied.")
    prefixes = (
        "MICRO_RELAY_TX_ASCII_HEX:",
        "MICRO_RELAY_TX_BINARY_HEX:",
        "Dynamic heartbeat hex:",
        "Dynamic location hex:",
        "Generated settings packet:",
    )
    for prefix in prefixes:
        idx = text.rfind(prefix)
        if idx >= 0:
            text = text[idx + len(prefix) :].strip().splitlines()[0]
            break
    if re.fullmatch(r"[0-9A-Fa-f\s:_-]+", text):
        candidate = re.sub(r"[^0-9A-Fa-f]", "", text)
    else:
        candidates = re.findall(r"(?i)(?:[0-9a-f]{2}[\s:_-]*){8,}", text)
        if not candidates:
            raise ValueError("Could not find a HEX packet in the supplied text.")
        candidate = re.sub(r"[^0-9A-Fa-f]", "", max(candidates, key=len))
    if len(candidate) % 2:
        raise ValueError("HEX input has an odd number of characters.")
    if len(candidate) < 18:
        raise ValueError("Packet is shorter than the 9-byte fixed header.")
    return candidate.upper()


def _add(fields: list[Field], start: int, raw: bytes, name: str, decoded: Any) -> None:
    end = start + len(raw) - 1
    offset = str(start) if len(raw) == 1 else f"{start}-{end}"
    fields.append(Field(offset, name, raw.hex().upper(), decoded))


def _mac(raw: bytes) -> str:
    return ":".join(f"{value:02X}" for value in raw)


def decode_packet(packet: bytes, *, target_imei: str | None = None) -> DecodeResult:
    summary = decode_application_packet(packet, target_imei=target_imei)
    errors = list(summary.get("errors", []))
    warnings: list[str] = []
    diagnostics: list[str] = []
    fields: list[Field] = []

    if len(packet) >= 1:
        _add(fields, 0, packet[0:1], "Header", f"0x{packet[0]:02X}")
    if len(packet) >= 2:
        _add(fields, 1, packet[1:2], "Property", f"0x{packet[1]:02X}")
    if len(packet) >= 4:
        _add(
            fields,
            2,
            packet[2:4],
            "Length (uint16 big-endian)",
            {
                "declared_command_plus_payload_bytes": int.from_bytes(packet[2:4], "big"),
                "actual": max(0, len(packet) - 8),
            },
        )
    if len(packet) >= 6:
        _add(
            fields,
            4,
            packet[4:6],
            "CRC-16/XMODEM",
            {
                "received_big_endian": f"0x{int.from_bytes(packet[4:6], 'big'):04X}",
                "calculated_over_payload": (
                    f"0x{crc16_xmodem(packet[PAYLOAD_OFFSET:]):04X}" if len(packet) >= PAYLOAD_OFFSET else None
                ),
                "valid": len(packet) >= PAYLOAD_OFFSET
                and int.from_bytes(packet[4:6], "big") == crc16_xmodem(packet[PAYLOAD_OFFSET:]),
            },
        )
    if len(packet) >= 8:
        _add(fields, 6, packet[6:8], "Sequence ID (uint16 big-endian)", int.from_bytes(packet[6:8], "big"))
    if len(packet) >= 9:
        command = packet[8]
        command_name = {
            COMMAND_HEARTBEAT: "heartbeat",
            COMMAND_LOCATION: "LTE-M location",
            COMMAND_SETTINGS_UPDATE: "settings update",
        }.get(command, "unknown")
        _add(fields, 8, packet[8:9], "Command", f"0x{command:02X} ({command_name})")

    if len(packet) < PAYLOAD_OFFSET:
        return DecodeResult(False, packet.hex().upper(), len(packet), fields, errors, warnings, diagnostics, summary)

    command = packet[8]
    payload = packet[9:]
    if command in (COMMAND_HEARTBEAT, COMMAND_LOCATION):
        if len(payload) >= 15:
            imei = payload[:15].decode("ascii", errors="replace")
            _add(fields, 9, payload[:15], "IMEI", imei)
        if len(payload) >= 23:
            timestamp_ms = int.from_bytes(payload[15:23], "big")
            try:
                timestamp_utc = unix_ms_to_iso8601(timestamp_ms)
            except (ValueError, OSError, OverflowError):
                timestamp_utc = None
                warnings.append(f"Timestamp {timestamp_ms} ms is outside the supported date range.")
            timestamp_name = "Heartbeat state timestamp" if command == COMMAND_HEARTBEAT else "GNSS fix timestamp"
            _add(
                fields,
                24,
                payload[15:23],
                f"{timestamp_name} (uint64 big-endian Unix ms)",
                {
                    "unix_ms": timestamp_ms,
                    "utc": timestamp_utc or "unavailable",
                    "epoch": TIMESTAMP_EPOCH,
                },
            )
        if len(payload) >= 27:
            _add(fields, 32, payload[23:24], "Battery level", summary.get("battery_name"))
            _add(fields, 33, payload[24:25], "Charging state", summary.get("charging_name"))
            _add(fields, 34, payload[25:27], "lastUpdate (uint16 big-endian)", summary.get("last_update_minutes"))

        if command == COMMAND_HEARTBEAT and len(payload) >= 30:
            _add(fields, 36, payload[27:28], "Software version", summary.get("software_version"))
            _add(fields, 37, payload[28:29], "Firmware version", summary.get("firmware_version"))
            _add(
                fields,
                38,
                payload[29:30],
                "Heartbeat opcode",
                f"0x{summary.get('opcode', 0):02X} ({summary.get('opcode_name')})",
            )
            location_length = 12 if summary.get("location_data_kind") == "gps" else 6
            if len(payload) >= 30 + location_length:
                location = payload[30 : 30 + location_length]
                decoded_location = {
                    key: value
                    for key, value in summary.items()
                    if key in {
                        "location_data_kind",
                        "location_data_hex",
                        "latitude",
                        "longitude",
                        "accuracy_m",
                        "speed_mps",
                    }
                }
                _add(fields, 39, location, "Heartbeat locationData", decoded_location)
                extension_offset = 39 + location_length
                registry = summary.get("trusted_device_registry")
                if registry is None:
                    _add(fields, extension_offset, b"", "Heartbeat format", "original Version 7")
                elif len(packet) >= extension_offset + 25:
                    extension = packet[extension_offset : extension_offset + 25]
                    _add(
                        fields,
                        extension_offset,
                        extension[:1],
                        "trustedDeviceCount",
                        registry.get("count"),
                    )
                    for index in range(4):
                        slot = extension[1 + index * 6 : 1 + (index + 1) * 6]
                        configured = index < registry.get("count", 0)
                        _add(
                            fields,
                            extension_offset + 1 + index * 6,
                            slot,
                            f"trustedDeviceSlot{index + 1}",
                            {"configured": configured, "identity": _mac(slot)},
                        )
                    _add(fields, extension_offset, extension, "Heartbeat format", "extended Version 7")
        elif command == COMMAND_LOCATION and len(payload) >= 39:
            _add(
                fields,
                36,
                payload[27:39],
                "Location fields",
                {
                    "latitude": summary.get("latitude"),
                    "longitude": summary.get("longitude"),
                    "accuracy_m": summary.get("accuracy_m"),
                    "speed_mps": summary.get("speed_mps"),
                },
            )

    elif command == COMMAND_SETTINGS_UPDATE:
        settings = summary.get("settings_update", {})
        if len(payload) >= 19:
            _add(fields, 9, payload[:15], "Target IMEI", settings.get("target_imei"))
            _add(fields, 24, payload[15:16], "Settings schema version", settings.get("schema_version"))
            _add(fields, 25, payload[16:18], "Update ID (uint16 big-endian)", settings.get("update_id"))
            _add(fields, 27, payload[18:19], "Setting entry count", settings.get("entry_count"))
            offset = 19
            for index, entry in enumerate(settings.get("entries", []), start=1):
                value_length = entry["value_length"]
                if offset + 4 + value_length > len(payload):
                    break
                raw_header = payload[offset : offset + 4]
                raw_value = payload[offset + 4 : offset + 4 + value_length]
                _add(
                    fields,
                    9 + offset,
                    raw_header,
                    f"Setting entry {index} TLV header",
                    {
                        "setting_id": f"0x{entry['setting_id']:02X}",
                        "setting_name": entry.get("setting_name"),
                        "value_type": f"0x{entry['value_type']:02X}",
                        "value_type_name": entry.get("value_type_name"),
                        "value_length": value_length,
                    },
                )
                _add(
                    fields,
                    9 + offset + 4,
                    raw_value,
                    f"Setting entry {index} value",
                    {
                        "decoded": entry.get("decoded_value"),
                        "valid": entry.get("valid"),
                        "error": entry.get("error"),
                    },
                )
                offset += 4 + value_length

    if command not in (COMMAND_HEARTBEAT, COMMAND_LOCATION, COMMAND_SETTINGS_UPDATE):
        diagnostics.append("The command is not defined by the updated simulator protocol.")
    if packet and packet[0] != HEADER:
        diagnostics.append("The packet does not begin with the binary header byte 0xAB.")
    if len(packet) > 1 and packet[1] != PROPERTY:
        diagnostics.append("The packet property byte does not match 0x10.")

    return DecodeResult(
        valid=not errors,
        packet_hex=packet.hex().upper(),
        packet_bytes=len(packet),
        fields=fields,
        errors=errors,
        warnings=warnings,
        diagnostics=diagnostics,
        summary=summary,
    )


def print_human(result: DecodeResult) -> None:
    print(f"Packet: {result.packet_hex}")
    print(f"Total size: {result.packet_bytes} bytes")
    print(f"Overall result: {'VALID' if result.valid else 'INVALID'}")
    print()
    print(f"{'Offset':<10} {'Field':<46} {'Raw HEX':<34} Decoded")
    print("-" * 130)
    for field in result.fields:
        decoded = json.dumps(field.decoded, ensure_ascii=False) if isinstance(field.decoded, (dict, list)) else str(field.decoded)
        print(f"{field.offset:<10} {field.name:<46} {field.raw_hex:<34} {decoded}")
    for title, items in (("ERRORS", result.errors), ("WARNINGS", result.warnings), ("DIAGNOSTICS", result.diagnostics)):
        if items:
            print(f"\n{title}:")
            for item in items:
                print(f"- {item}")


def _read_input(args: Iterable[str]) -> str:
    joined = " ".join(args).strip()
    if joined:
        return joined
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return input("Paste packet HEX and press Enter:\n> ")


def main() -> int:
    parser = argparse.ArgumentParser(description="Decode and validate a Micro application packet.")
    parser.add_argument("packet", nargs="*", help="Packet HEX or simulator log line.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--target-imei", help="Require a settings update to target this IMEI.")
    parser.add_argument("--crc-self-test", action="store_true")
    parser.add_argument("--timestamp-to-unix-ms", metavar="ISO8601")
    parser.add_argument("--unix-ms-to-timestamp", metavar="UNIX_MS")
    args = parser.parse_args()

    if args.crc_self_test:
        value = crc16_xmodem(b"123456789")
        print(f"CRC-16/XMODEM('123456789') = 0x{value:04X}")
        return 0 if value == 0x31C3 else 1
    if args.timestamp_to_unix_ms is not None:
        try:
            timestamp_ms = iso8601_to_unix_ms(args.timestamp_to_unix_ms)
        except ValueError as exc:
            print(f"Timestamp input error: {exc}", file=sys.stderr)
            return 2
        print(f"UTC timestamp: {unix_ms_to_iso8601(timestamp_ms)}")
        print(f"Unix milliseconds: {timestamp_ms}")
        print(f"8-byte big-endian HEX: {timestamp_ms.to_bytes(8, 'big').hex().upper()}")
        print(f"Simulator command: set timestamp {timestamp_ms}")
        return 0
    if args.unix_ms_to_timestamp is not None:
        try:
            timestamp_ms = int(args.unix_ms_to_timestamp, 10)
            timestamp_utc = unix_ms_to_iso8601(timestamp_ms)
        except (ValueError, OSError, OverflowError) as exc:
            print(f"Timestamp input error: {exc}", file=sys.stderr)
            return 2
        print(f"Unix milliseconds: {timestamp_ms}")
        print(f"UTC timestamp: {timestamp_utc or 'unavailable'}")
        print(f"8-byte big-endian HEX: {timestamp_ms.to_bytes(8, 'big').hex().upper()}")
        return 0

    try:
        packet_hex = normalize_hex_input(_read_input(args.packet))
        result = decode_packet(bytes.fromhex(packet_hex), target_imei=args.target_imei)
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.json_ready(), indent=2, ensure_ascii=False))
    else:
        print_human(result)
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
