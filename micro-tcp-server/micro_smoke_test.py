#!/usr/bin/env python3
"""Send a synthetic V8 heartbeat and check the server's response."""

from __future__ import annotations

import argparse
import math
import socket
import sys
import time

from micro_protocol import OPCODE_BEACON, ProtocolError, build_heartbeat_packet, decode_application_packet
from micro_response_parser import ResponseEvent, ServerResponseParser


def read_response(connection: socket.socket, timeout: float) -> ResponseEvent:
    parser = ServerResponseParser()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError(f"Timed out waiting for a complete response ({parser.timeout().kind}).")
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(1024)
        except TimeoutError as exc:
            raise ProtocolError(f"Timed out waiting for a complete response ({parser.timeout().kind}).") from exc
        if not chunk:
            partial = parser.connection_closed()
            detail = f" ({partial.kind})" if partial else ""
            raise ProtocolError(f"Connection closed before a complete response{detail}.")
        events = parser.feed(chunk)
        if events:
            if len(events) != 1 or parser.buffer:
                raise ProtocolError("Server returned extra bytes after the heartbeat response.")
            return events[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Micro V8 TCP with a synthetic heartbeat. A matching queued update can be transmitted.",
    )
    parser.add_argument("--host", required=True, help="Server IPv4 address or hostname.")
    parser.add_argument("--port", type=int, default=5000, help="TCP port (default 5000).")
    parser.add_argument("--imei", required=True, help="15-digit IMEI assigned to the test.")
    parser.add_argument("--expect", choices=("ok", "update"), default="ok", help="Expected server response.")
    parser.add_argument("--update-id", type=int, help="Expected update ID when --expect update is used.")
    parser.add_argument("--ascii-hex", action="store_true", help="Send the heartbeat as diagnostic ASCII HEX.")
    parser.add_argument("--timeout", type=float, default=10, help="Connect and response timeouts in seconds.")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite value greater than 0.")
    if args.update_id is not None and (args.expect != "update" or not 0 <= args.update_id <= 65535):
        parser.error("--update-id requires --expect update and must be between 0 and 65535.")

    try:
        # A locally administered beacon and an empty registry keep the fixture synthetic.
        packet = build_heartbeat_packet(
            sequence_id=1,
            imei=args.imei,
            timestamp_ms=time.time_ns() // 1_000_000,
            battery=0x10,
            charging=0x01,
            last_update_minutes=0,
            software_version=1,
            firmware_version=1,
            opcode=OPCODE_BEACON,
            location_data=bytes.fromhex("020000000001"),
        )
        wire = packet.hex().upper().encode("ascii") + b"\n" if args.ascii_hex else packet
        with socket.create_connection((args.host, args.port), timeout=args.timeout) as connection:
            connection.sendall(wire)
            event = read_response(connection, args.timeout)

        if event.kind == "OK" and event.raw == b"OK\n":
            if args.expect != "ok":
                raise ProtocolError("Received OK instead of the expected settings update.")
            print(f"OK: server accepted the heartbeat for IMEI {args.imei}.")
            return 0
        if event.kind != "SETTINGS_PACKET" or event.packet is None:
            raise ProtocolError(f"Unexpected server response ({event.kind}): {event.raw[:120]!r}")

        decoded = decode_application_packet(event.packet, target_imei=args.imei)
        if not decoded["valid"]:
            raise ProtocolError("Invalid settings update. " + " ".join(decoded["errors"]))
        update_id = decoded["configuration_update"]["update_id"]
        if args.expect != "update":
            raise ProtocolError(f"Server transmitted settings update {update_id}, while this check expected OK.")
        if args.update_id is not None and args.update_id != update_id:
            raise ProtocolError(f"Received update ID {update_id}, expected {args.update_id}.")
        print(f"OK: validated direct binary settings update {update_id} for IMEI {args.imei} ({len(event.packet)} bytes).")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
