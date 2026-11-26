#!/usr/bin/env python3
"""Receive Micro V8 packets and deliver queued settings on matching heartbeats."""

from __future__ import annotations

import logging
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from micro_packet_decoder import decode_packet, unix_ms_to_iso8601
from micro_pending_store import PendingUpdateStore, default_db_path
from micro_protocol import (
    COMMAND_CONFIGURATION_UPDATE,
    COMMAND_HEARTBEAT,
    COMMAND_LOCATION,
    COMMAND_OFFSET,
    FIXED_PREFIX_BYTES,
    HEADER,
    PAYLOAD_OFFSET,
    PROPERTY,
    validate_imei,
)

MAX_PACKET_BYTES = 4096
READ_BYTES = 4096
IDLE_TIMEOUT_SECONDS = 24 * 60 * 60
FRAME_TIMEOUT_SECONDS = 30
SEND_TIMEOUT_SECONDS = 10
MAX_CLIENTS = 32
HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")
WHITESPACE_BYTES = frozenset(b" \t\r\n")
ASCII_HEX_BYTES = HEX_BYTES | WHITESPACE_BYTES
POSSIBLE_PREFIX = re.compile(rb"[\xABAa]")
PACKET_IDENTITY_COMMANDS = {COMMAND_HEARTBEAT, COMMAND_LOCATION, COMMAND_CONFIGURATION_UPDATE}
LOG = logging.getLogger("micro-tcp-server")


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    packet: bytes
    wire_mode: str | None = None
    reason: str | None = None


class MicroStreamParser:
    """Frame binary and diagnostic ASCII-HEX packets on a persistent TCP stream."""

    def __init__(self, max_packet_bytes: int = MAX_PACKET_BYTES):
        self.max_packet_bytes = max_packet_bytes
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[StreamEvent]:
        self.buffer.extend(data)
        events = []
        while self.buffer:
            if self.buffer[0] == HEADER:
                event = self._extract_binary()
            elif self.buffer[0] in (ord("A"), ord("a")):
                event = self._extract_ascii_hex()
            else:
                candidate = POSSIBLE_PREFIX.search(self.buffer)
                count = candidate.start() if candidate else len(self.buffer)
                noise = self._take(count)
                if noise.strip(b" \t\r\n"):
                    events.append(StreamEvent("framing_error", noise, reason="Bytes before packet prefix"))
                continue
            if event is None:
                break
            events.append(event)
        return events

    def finish(self) -> StreamEvent | None:
        if not self.buffer:
            return None
        mode = "binary" if self.buffer[0] == HEADER else "ascii_hex"
        return StreamEvent("incomplete_packet", self._take(len(self.buffer)), mode, "Incomplete packet at end of stream")

    def _take(self, count: int) -> bytes:
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def _valid_length(self, declared: int) -> bool:
        return 1 <= declared <= self.max_packet_bytes - FIXED_PREFIX_BYTES

    def _extract_binary(self) -> StreamEvent | None:
        if len(self.buffer) < 2:
            return None
        if self.buffer[1] != PROPERTY:
            return StreamEvent("framing_error", self._take(1), "binary", "Unexpected property byte")
        if len(self.buffer) < 4:
            return None
        declared = int.from_bytes(self.buffer[2:4], "big")
        if not self._valid_length(declared):
            return StreamEvent("framing_error", self._take(1), "binary", f"Unsupported length {declared}")
        total = FIXED_PREFIX_BYTES + declared
        if len(self.buffer) < total:
            return None
        return StreamEvent("packet", self._take(total), "binary")

    def _extract_ascii_hex(self) -> StreamEvent | None:
        digits = bytearray()
        target_digits = None
        for index, value in enumerate(self.buffer):
            if index >= self.max_packet_bytes * 4 or value not in ASCII_HEX_BYTES:
                return StreamEvent("framing_error", self._take(1), "ascii_hex", "Malformed or oversized ASCII-HEX frame")
            if value in WHITESPACE_BYTES:
                continue
            digits.append(value)
            if len(digits) <= 4 and not b"AB10".startswith(digits.upper()):
                return StreamEvent("framing_error", self._take(1), "ascii_hex", "Unexpected ASCII-HEX prefix")
            if len(digits) == 8:
                declared = int(digits[4:8], 16)
                if not self._valid_length(declared):
                    return StreamEvent("framing_error", self._take(1), "ascii_hex", f"Unsupported length {declared}")
                target_digits = (FIXED_PREFIX_BYTES + declared) * 2
            if len(digits) == target_digits:
                self._take(index + 1)
                return StreamEvent("packet", bytes.fromhex(digits.decode("ascii")), "ascii_hex")
        return None


def packet_imei(packet: bytes) -> str | None:
    """Extract a recognizable identity claim for logging, even on a bad CRC."""
    imei_end = PAYLOAD_OFFSET + 15
    if len(packet) >= imei_end and packet[COMMAND_OFFSET] in PACKET_IDENTITY_COMMANDS:
        raw_imei = packet[PAYLOAD_OFFSET:imei_end]
        if raw_imei.isdigit():
            return raw_imei.decode("ascii")
    return None


def handle_client(
    conn: socket.socket,
    addr: tuple[str, int],
    store: PendingUpdateStore,
    allowed_imeis: frozenset[str] = frozenset(),
) -> None:
    connection_id = uuid.uuid4().hex
    opened = time.monotonic()
    parser = MicroStreamParser()

    def record(direction: str, kind: str, payload: bytes | None = None, **fields) -> None:
        store.record_message(
            connection_id=connection_id,
            remote_ip=addr[0],
            remote_port=addr[1],
            direction=direction,
            kind=kind,
            payload=payload,
            **fields,
        )

    with conn:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        close_reason = "peer_closed"
        frame_deadline = None
        try:
            record("connection", "opened")
            while True:
                timeout = IDLE_TIMEOUT_SECONDS if frame_deadline is None else frame_deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError("Incomplete packet deadline elapsed")
                conn.settimeout(timeout)
                chunk = conn.recv(READ_BYTES)
                if not chunk:
                    break
                record("inbound", "raw_chunk", chunk)
                events = parser.feed(chunk)
                if parser.buffer and (frame_deadline is None or events):
                    frame_deadline = time.monotonic() + FRAME_TIMEOUT_SECONDS
                elif not parser.buffer:
                    frame_deadline = None
                for event in events:
                    if event.kind != "packet":
                        record("inbound", event.kind, event.packet, wire_mode=event.wire_mode,
                               valid=False, details={"reason": event.reason})
                        continue
                    packet = event.packet
                    decoded = decode_packet(packet)
                    command = decoded.summary["command"]
                    imei = packet_imei(packet)
                    errors = decoded.errors.copy()
                    if decoded.valid and command not in {COMMAND_HEARTBEAT, COMMAND_LOCATION}:
                        errors.append("Command is not permitted from a device")
                    if imei and allowed_imeis and imei not in allowed_imeis:
                        errors.append("IMEI is outside the configured allowlist")
                    summary = decoded.json_ready()["summary"]
                    summary.pop("errors", None)
                    summary.pop("packet_hex", None)
                    if "timestamp_ms" in summary:
                        try:
                            summary["timestamp_utc"] = unix_ms_to_iso8601(summary["timestamp_ms"])
                        except (OverflowError, OSError, ValueError):
                            summary["timestamp_utc"] = None
                    record("inbound", "packet", packet, imei=imei, wire_mode=event.wire_mode,
                           valid=not errors, details={"parsed": summary, "errors": errors, "warnings": decoded.warnings})

                    update = None
                    response = b"ERROR:INVALID_PACKET\n" if errors else b"OK\n"
                    if not errors and command == COMMAND_HEARTBEAT and imei:
                        update = store.claim_packet(imei, connection_id)
                        if update:
                            response = bytes.fromhex(update["packet_hex"])
                    response_fields = {"imei": imei, "update_id": update["update_id"] if update else None}
                    record("outbound", "response_attempt", response, **response_fields)
                    try:
                        conn.settimeout(SEND_TIMEOUT_SECONDS)
                        conn.sendall(response)
                    except OSError as exc:
                        if update:
                            store.finish_delivery(update["id"], connection_id, status="failed", error=str(exc))
                        record("outbound", "response_failed", details={"error": str(exc)}, **response_fields)
                        raise
                    if update:
                        store.finish_delivery(update["id"], connection_id, status="sent_unconfirmed")
                    record("outbound", "response_sent", **response_fields)
        except TimeoutError:
            close_reason = "timeout"
        except OSError as exc:
            close_reason = "socket_error"
            LOG.info("Connection %s closed after a socket error: %s", connection_id, exc)
        except sqlite3.Error:
            close_reason = "storage_error"
            LOG.exception("Storage failed on connection %s", connection_id)
        finally:
            try:
                partial = parser.finish()
                if partial:
                    record("inbound", partial.kind, partial.packet, wire_mode=partial.wire_mode,
                           valid=False, details={"reason": partial.reason})
                record("connection", "closed", details={"reason": close_reason,
                       "duration_seconds": round(time.monotonic() - opened, 3)})
            except sqlite3.Error:
                LOG.exception("Could not record closure of connection %s", connection_id)


def notify_systemd_ready() -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
        notifier.sendto(b"READY=1", address)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host = os.environ.get("MICRO_SERVER_HOST", "127.0.0.1")
    port = int(os.environ.get("MICRO_SERVER_PORT", "5000"))
    db_path = default_db_path()
    allowed_imeis = frozenset(value.strip() for value in os.environ.get("MICRO_ALLOWED_IMEIS", "").split(",") if value.strip())
    for imei in allowed_imeis:
        validate_imei(imei)
    slots = threading.BoundedSemaphore(MAX_CLIENTS)

    def run_connection(client: socket.socket, addr: tuple[str, int]) -> None:
        try:
            handle_client(client, addr, store, allowed_imeis)
        finally:
            slots.release()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(MAX_CLIENTS)
        store = PendingUpdateStore(db_path)
        store.recover_interrupted_deliveries()
        LOG.info("Micro V8 TCP server listening on %s:%d with database %s", host, listener.getsockname()[1], db_path.resolve())
        notify_systemd_ready()
        try:
            while True:
                conn, addr = listener.accept()
                if not slots.acquire(blocking=False):
                    conn.close()
                    LOG.warning("Connection refused because all %d client slots are in use", MAX_CLIENTS)
                    continue
                threading.Thread(target=run_connection, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            LOG.info("Micro V8 TCP server stopped")


if __name__ == "__main__":
    main()
