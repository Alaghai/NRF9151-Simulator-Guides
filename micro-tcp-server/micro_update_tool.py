#!/usr/bin/env python3
"""Generate Micro configuration packets and manage the per-IMEI queue and audit."""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time
from typing import Any

from micro_packet_decoder import decode_packet, print_human
from micro_pending_store import PendingUpdateStore, default_db_path
from micro_protocol import (
    SETTINGS,
    SETTINGS_BY_NAME,
    ProtocolError,
    build_settings_update_packet,
    parse_cli_setting_value,
)


def build_from_args(args: argparse.Namespace) -> tuple[bytes, dict[str, Any]]:
    settings: dict[str, Any] = {}
    for assignment in args.set_values:
        name, separator, text = assignment.partition("=")
        if not separator:
            raise ProtocolError(f"Setting assignment {assignment!r} must use name=value.")
        if name == "sleep_interval_seconds":
            name = "ble_check_interval_seconds"
        definition = SETTINGS_BY_NAME.get(name)
        if definition is None:
            raise ProtocolError(f"Unknown setting {name!r}.")
        if name in settings:
            raise ProtocolError(f"Duplicate setting {name!r}.")
        settings[name] = parse_cli_setting_value(definition, text)
    missing = set(SETTINGS_BY_NAME) - settings.keys()
    if missing:
        raise ProtocolError(
            "Canonical configuration updates are full replacement packets. Missing --set values: "
            + ", ".join(sorted(missing))
        )
    update_id = args.update_id if args.update_id is not None else int(time.time()) & 0xFFFF
    packet = build_settings_update_packet(
        target_imei=args.imei,
        update_id=update_id,
        settings=settings,
        sequence_id=args.sequence_id,
    )
    display = {
        name: value.hex().upper() if isinstance(value, bytes) else value
        for name, value in settings.items()
    }
    return packet, display


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage canonical command-0x02 Micro configurations and the TCP packet audit."
    )
    parser.add_argument(
        "--store", type=pathlib.Path, default=default_db_path(),
        help="SQLite path. Defaults to MICRO_DB or ./micro-data/micro.sqlite3.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("generate", "Encode a complete update without queuing it."),
        ("queue", "Encode and queue a complete update."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--imei", required=True)
        command.add_argument(
            "--set", dest="set_values", action="append", required=True, metavar="NAME=VALUE",
            help="Repeat for all seven full-replacement fields. Use empty HEX to clear a list.",
        )
        command.add_argument("--update-id", type=int)
        command.add_argument("--sequence-id", type=int, default=1)

    raw = commands.add_parser("queue-raw", help="Validate and queue an existing complete update.")
    raw.add_argument("--imei", required=True)
    raw.add_argument("--hex", required=True, dest="packet_hex")

    inspect = commands.add_parser("inspect", help="Decode a packet or the current update for an IMEI.")
    inspect.add_argument("--imei")
    inspect.add_argument("--hex", dest="packet_hex")

    commands.add_parser("list", help="List the current update for each IMEI.")
    for name, help_text in (
        ("cancel", "Cancel the current update."),
        ("requeue", "Make the current update eligible for transmission again."),
        ("remove", "Remove the current update from the queue and retain its history."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--imei", required=True)

    history = commands.add_parser("history", help="List recent updates for the given IMEI.")
    history.add_argument("--imei", required=True)
    history.add_argument("--limit", type=int, default=20)

    messages = commands.add_parser("messages", help="Read recent packet and connection records.")
    messages.add_argument("--imei", help="Filter to records whose decoded or target IMEI matches.")
    messages.add_argument("--connection", help="Filter to a connection ID from a previous query.")
    messages.add_argument("--kind", help="Filter by event kind, such as packet or raw_chunk.")
    messages.add_argument("--limit", type=int, default=20)

    commands.add_parser("devices", help="Summarize valid inbound traffic by IMEI.")
    commands.add_parser("registry", help="List the seven configuration settings and input formats.")
    backup = commands.add_parser("backup", help="Write a consistent SQLite snapshot to a new file.")
    backup.add_argument("--out", required=True, type=pathlib.Path)
    legacy = commands.add_parser("import-legacy", help="Import a saved V8 pending-update JSON store.")
    legacy.add_argument("--from", dest="source", required=True, type=pathlib.Path)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        if args.command == "registry":
            rows = [
                {"name": setting.name, "kind": setting.kind, "description": setting.description}
                for setting in SETTINGS
            ]
            print(json.dumps(rows, indent=2))
            return 0

        if args.command in {"generate", "queue"}:
            packet, settings = build_from_args(args)
            decoded = decode_packet(packet, target_imei=args.imei)
            print(f"Packet HEX: {packet.hex().upper()}")
            print(f"Packet bytes: {len(packet)}")
            print(f"Validation: {'VALID' if decoded.valid else 'INVALID'}")
            if not decoded.valid:
                return 1
            if args.command == "queue":
                store = PendingUpdateStore(args.store)
                record = store.queue_packet(imei=args.imei, packet=packet, settings=settings)
                print(f"Queued update ID: {record['update_id']}")
                print(f"Store: {args.store}")
            return 0

        if args.command == "inspect":
            if args.packet_hex:
                packet = bytes.fromhex("".join(args.packet_hex.split()))
            elif args.imei:
                record = PendingUpdateStore(args.store).get(args.imei)
                if record is None:
                    raise ProtocolError(f"No current update exists for IMEI {args.imei}.")
                packet = bytes.fromhex(record["packet_hex"])
            else:
                raise ProtocolError("inspect requires --hex or --imei.")
            decoded = decode_packet(packet, target_imei=args.imei)
            print_human(decoded)
            return 0 if decoded.valid else 1

        store = PendingUpdateStore(args.store)
        if args.command == "queue-raw":
            result = store.queue_packet(
                imei=args.imei,
                packet=bytes.fromhex("".join(args.packet_hex.split())),
                source="micro_update_tool queue-raw",
            )
        elif args.command == "list":
            result = store.list_records()
        elif args.command == "history":
            result = store.history(args.imei, limit=args.limit)
        elif args.command == "messages":
            result = store.messages(
                imei=args.imei, connection_id=args.connection, kind=args.kind, limit=args.limit,
            )
        elif args.command == "devices":
            result = store.devices()
        elif args.command == "backup":
            store.backup(args.out)
            print(f"Backup written to {args.out}")
            return 0
        elif args.command == "import-legacy":
            result = store.import_legacy(args.source)
        elif args.command == "cancel":
            result = store.cancel(args.imei)
        elif args.command == "requeue":
            result = store.requeue(args.imei)
        else:
            removed = store.remove(args.imei)
            print("Removed from active queue" if removed else "No current record found")
            return 0 if removed else 1
        if result is None:
            print("No current record found", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
