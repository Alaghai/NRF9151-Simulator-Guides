#!/usr/bin/env python3
"""Development utility for generating and queuing Micro settings updates."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any

from micro_packet_decoder import decode_packet, print_human
from micro_pending_store import PendingUpdateStore
from micro_protocol import (
    SETTINGS,
    SETTINGS_BY_NAME,
    ProtocolError,
    build_settings_update_packet,
    parse_cli_setting_value,
    validate_imei,
)

DEFAULT_STORE = pathlib.Path("/root/micro_pending_updates.json")


def parse_assignments(items: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    settings: dict[str, Any] = {}
    display: list[dict[str, Any]] = []
    for item in items:
        if "=" not in item:
            raise ProtocolError(f"Setting assignment {item!r} must use name=value.")
        name, text = item.split("=", 1)
        definition = SETTINGS_BY_NAME.get(name)
        if definition is None:
            raise ProtocolError(f"Unknown setting {name!r}.")
        if name in settings:
            raise ProtocolError(f"Duplicate setting {name!r}.")
        value = parse_cli_setting_value(definition, text)
        settings[name] = value
        display_value = value.hex().upper() if isinstance(value, (bytes, bytearray)) else value
        display.append({"name": name, "value": display_value})
    return settings, display


def next_update_id() -> int:
    return int(time.time()) & 0xFFFF


def build_from_args(args: argparse.Namespace) -> tuple[bytes, list[dict[str, Any]]]:
    settings, display = parse_assignments(args.set_values)
    update_id = args.update_id if args.update_id is not None else next_update_id()
    packet = build_settings_update_packet(
        target_imei=args.imei,
        update_id=update_id,
        settings=settings,
        sequence_id=args.sequence_id,
    )
    return packet, display


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and queue Micro SETTINGS_UPDATE packets.")
    parser.add_argument("--store", type=pathlib.Path, default=DEFAULT_STORE)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("generate", "queue"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--imei", required=True)
        cmd.add_argument("--set", dest="set_values", action="append", required=True, metavar="NAME=VALUE")
        cmd.add_argument("--update-id", type=int)
        cmd.add_argument("--sequence-id", type=int, default=1)

    raw = sub.add_parser("queue-raw")
    raw.add_argument("--imei", required=True)
    raw.add_argument("--hex", required=True, dest="packet_hex")

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--imei")
    inspect.add_argument("--hex", dest="packet_hex")

    sub.add_parser("list")
    for name in ("cancel", "requeue", "remove"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--imei", required=True)

    sub.add_parser("registry")
    return parser


def main() -> int:
    args = make_parser().parse_args()
    store = PendingUpdateStore(args.store)
    try:
        if args.command == "registry":
            rows = [
                {
                    "id": f"0x{item.setting_id:02X}",
                    "name": item.name,
                    "type": f"0x{item.value_type:02X}",
                    "minimum": item.minimum,
                    "maximum": item.maximum,
                    "default": item.default.hex().upper() if isinstance(item.default, bytes) else item.default,
                    "persistent": item.persistent,
                    "description": item.description,
                }
                for item in SETTINGS
            ]
            print(json.dumps(rows, indent=2))
            return 0

        if args.command in {"generate", "queue"}:
            validate_imei(args.imei)
            packet, settings = build_from_args(args)
            print(f"Packet HEX: {packet.hex().upper()}")
            print(f"Packet bytes: {len(packet)}")
            decoded = decode_packet(packet, target_imei=args.imei)
            print(f"Validation: {'VALID' if decoded.valid else 'INVALID'}")
            if args.command == "queue":
                record = store.queue_packet(imei=args.imei, packet=packet, settings=settings)
                print(f"Queued update ID: {record['update_id']}")
                print(f"Store: {args.store}")
            return 0 if decoded.valid else 1

        if args.command == "queue-raw":
            validate_imei(args.imei)
            packet = bytes.fromhex("".join(args.packet_hex.split()))
            record = store.queue_packet(imei=args.imei, packet=packet, source="micro_update_tool queue-raw")
            print(json.dumps(record, indent=2))
            return 0

        if args.command == "list":
            print(json.dumps(store.list_records(), indent=2))
            return 0

        if args.command == "inspect":
            if args.packet_hex:
                packet = bytes.fromhex("".join(args.packet_hex.split()))
            elif args.imei:
                record = store.get(args.imei)
                if not record:
                    raise ProtocolError(f"No update exists for IMEI {args.imei}.")
                packet = bytes.fromhex(record["packet_hex"])
            else:
                raise ProtocolError("inspect requires --hex or --imei.")
            result = decode_packet(packet, target_imei=args.imei)
            print_human(result)
            return 0 if result.valid else 1

        if args.command == "cancel":
            record = store.cancel(args.imei)
        elif args.command == "requeue":
            record = store.requeue(args.imei)
        else:
            removed = store.remove(args.imei)
            print("Removed" if removed else "No record found")
            return 0 if removed else 1
        if not record:
            print("No record found", file=sys.stderr)
            return 1
        print(json.dumps(record, indent=2))
        return 0
    except (ProtocolError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
