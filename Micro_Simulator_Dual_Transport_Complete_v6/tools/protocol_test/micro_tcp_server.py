#!/usr/bin/env python3
"""Persistent Micro TCP test server with Version 7 stream framing and AUTO updates."""

from __future__ import annotations

import binascii
import datetime
import json
import os
import pathlib
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

try:
    from micro_packet_decoder import decode_packet
except ImportError:  # pragma: no cover - exercised through simulated import failure tests
    decode_packet = None
from micro_pending_store import PendingUpdateStore
from micro_protocol import COMMAND_HEARTBEAT, HEADER, PROPERTY

HOST = os.environ.get("MICRO_SERVER_HOST", "0.0.0.0")
PORT = int(os.environ.get("MICRO_SERVER_PORT", "5000"))
LOG_FILE = pathlib.Path(os.environ.get("MICRO_PACKET_LOG", "/root/micro_tcp_packets.log"))
TRACE_FILE = pathlib.Path(os.environ.get("MICRO_TRACE_LOG", "/root/micro_protocol_trace.jsonl"))
RESPONSE_MODE_FILE = pathlib.Path(os.environ.get("MICRO_RESPONSE_MODE", "/root/micro_response_mode.txt"))
PENDING_UPDATES_FILE = pathlib.Path(os.environ.get("MICRO_PENDING_UPDATES", "/root/micro_pending_updates.json"))
MAX_PACKET_BYTES = 4096
FIXED_PREFIX_BYTES = 8
CONNECTION_TIMEOUT_SECONDS = 120

LOG_LOCK = threading.Lock()
HEX_BYTES = set(b"0123456789abcdefABCDEF")
WHITESPACE_BYTES = set(b" \t\r\n")
PENDING_STORE = PendingUpdateStore(PENDING_UPDATES_FILE)


def utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def safe_ascii(data: bytes) -> str:
    return "".join(chr(v) if 32 <= v <= 126 else "\\n" if v in (10, 13) else "." for v in data)


def read_response_mode() -> str:
    if not RESPONSE_MODE_FILE.exists():
        return "AUTO"
    value = RESPONSE_MODE_FILE.read_text(encoding="utf-8").strip()
    return value or "AUTO"


@dataclass(frozen=True)
class FramedPacket:
    wire_mode: str
    packet: bytes


@dataclass(frozen=True)
class DecoderUnavailableResult:
    valid: bool
    packet_hex: str
    packet_bytes: int
    errors: list[str]
    summary: dict[str, Any]


@dataclass
class ResponseDecision:
    response: bytes
    mode: str
    decoded: Any
    pending_update_id: int | None = None
    pending_imei: str | None = None
    sends_settings_packet: bool = False


class MicroStreamParser:
    """Incrementally extract binary or ASCII-HEX packets from a TCP stream."""

    def __init__(self, max_packet_bytes: int = MAX_PACKET_BYTES):
        self.max_packet_bytes = max_packet_bytes
        self._raw = bytearray()
        self._mode: str | None = None
        self._ascii_hex = bytearray()
        self.diagnostics: list[str] = []

    def feed(self, data: bytes) -> list[FramedPacket]:
        if data:
            self._raw.extend(data)
        packets: list[FramedPacket] = []
        while True:
            if self._mode is None and not self._detect_mode():
                break
            before = (len(self._raw), len(self._ascii_hex), self._mode)
            packet = self._extract_binary() if self._mode == "binary" else self._extract_ascii_hex()
            if packet is None:
                after = (len(self._raw), len(self._ascii_hex), self._mode)
                if after != before and self._mode is None:
                    continue
                break
            packets.append(packet)
            self._mode = None
        return packets

    def _find_binary_candidate(self) -> tuple[int, bool] | None:
        raw = bytes(self._raw)
        search_from = 0
        while True:
            index = raw.find(b"\xAB\x10", search_from)
            if index < 0:
                return None
            if len(raw) < index + 4:
                return index, False
            declared = int.from_bytes(raw[index + 2 : index + 4], "big")
            total = FIXED_PREFIX_BYTES + declared
            if declared >= 1 and total <= self.max_packet_bytes:
                return index, True
            search_from = index + 1

    def _find_ascii_candidate(self) -> tuple[int, bool] | None:
        upper = bytes(self._raw).upper()
        search_from = 0
        while True:
            index = upper.find(b"AB", search_from)
            if index < 0:
                return None
            if len(upper) < index + 4:
                return index, False
            if upper[index : index + 4] != b"AB10":
                search_from = index + 1
                continue
            if len(upper) < index + 8:
                return index, False
            try:
                declared = int(upper[index + 4 : index + 8].decode("ascii"), 16)
            except ValueError:
                search_from = index + 1
                continue
            total = FIXED_PREFIX_BYTES + declared
            if declared >= 1 and total <= self.max_packet_bytes:
                return index, True
            search_from = index + 1

    def _detect_mode(self) -> bool:
        if self._ascii_hex:
            self._mode = "ascii_hex"
            return True
        while self._raw and self._raw[0] in WHITESPACE_BYTES:
            del self._raw[0]
        if not self._raw:
            return False
        if self._raw[0] == HEADER:
            self._mode = "binary"
            return True
        binary_candidate = self._find_binary_candidate()
        ascii_candidate = self._find_ascii_candidate()
        if binary_candidate is None and ascii_candidate is None:
            if self._raw[-1] in (ord("A"), ord("a")):
                keep = 1
            else:
                keep = 0
            discard = len(self._raw) - keep
            if discard:
                self.diagnostics.append(f"Discarded {discard} non-packet byte(s) while searching for AB.")
                del self._raw[:discard]
            return False

        candidates = []
        if binary_candidate is not None:
            candidates.append((binary_candidate[0], binary_candidate[1], "binary"))
        if ascii_candidate is not None:
            candidates.append((ascii_candidate[0], ascii_candidate[1], "ascii_hex"))
        complete_candidates = [candidate for candidate in candidates if candidate[1]]
        selected = min(complete_candidates or candidates, key=lambda candidate: candidate[0])
        start, complete, mode = selected
        if start > 0:
            self.diagnostics.append(f"Discarded {start} leading byte(s) before a possible AB header.")
            del self._raw[:start]
        if not complete:
            return False
        self._mode = mode
        return True

    def _extract_binary(self) -> FramedPacket | None:
        if len(self._raw) < 4:
            return None
        if self._raw[0] != HEADER:
            self._mode = None
            return None
        if self._raw[1] != PROPERTY:
            self.diagnostics.append(
                f"Rejected binary header AB followed by property 0x{self._raw[1]:02X}; expected 0x{PROPERTY:02X}."
            )
            del self._raw[0]
            self._mode = None
            return None
        declared = int.from_bytes(self._raw[2:4], "big")
        total = FIXED_PREFIX_BYTES + declared
        if declared < 1 or total > self.max_packet_bytes:
            self.diagnostics.append(f"Rejected binary length {declared}; total would be {total} bytes.")
            del self._raw[0]
            self._mode = None
            return None
        if len(self._raw) < total:
            return None
        packet = bytes(self._raw[:total])
        del self._raw[:total]
        return FramedPacket("binary", packet)

    def _extract_ascii_hex(self) -> FramedPacket | None:
        consumed = 0
        for value in self._raw:
            if value in HEX_BYTES:
                self._ascii_hex.append(value)
                consumed += 1
            elif value in WHITESPACE_BYTES:
                consumed += 1
            else:
                break
        if consumed:
            del self._raw[:consumed]
        upper = bytes(self._ascii_hex).upper()
        start = upper.find(b"AB10")
        if start < 0:
            if len(self._ascii_hex) > 3:
                discard = len(self._ascii_hex) - 3
                self.diagnostics.append(f"Discarded {discard} ASCII-HEX character(s) while searching for AB10.")
                del self._ascii_hex[:discard]
            return None
        if start > 0:
            self.diagnostics.append(f"Discarded {start} ASCII-HEX character(s) before an AB10 header.")
            del self._ascii_hex[:start]
            upper = bytes(self._ascii_hex).upper()
        if len(upper) < 8:
            return None
        try:
            declared = int(upper[4:8].decode("ascii"), 16)
        except ValueError:
            self.diagnostics.append("Invalid ASCII-HEX length field after AB10.")
            del self._ascii_hex[0]
            self._mode = None
            return None
        total = FIXED_PREFIX_BYTES + declared
        total_chars = total * 2
        if declared < 1 or total > self.max_packet_bytes:
            self.diagnostics.append(f"Rejected ASCII-HEX length {declared}; total would be {total} bytes.")
            del self._ascii_hex[0]
            self._mode = None
            return None
        if len(self._ascii_hex) < total_chars:
            return None
        packet_hex = bytes(self._ascii_hex[:total_chars])
        del self._ascii_hex[:total_chars]
        try:
            packet = bytes.fromhex(packet_hex.decode("ascii"))
        except ValueError:
            self.diagnostics.append("ASCII-HEX packet contained a non-HEX character.")
            self._mode = None
            return None
        return FramedPacket("ascii_hex", packet)


def build_manual_response(mode: str, packet: bytes) -> bytes:
    mode = mode.strip()
    if mode == "OK":
        return b"OK\n"
    if mode == "CONFIG_NONE":
        return b"CONFIG_NONE\n"
    if mode == "ERROR":
        return b"ERROR\n"
    if mode == "FWUP":
        return b"FWUP\n"
    if mode in {"ECHO_HEX", "ECHO_PACKET_HEX"}:
        return b"RX_HEX:" + binascii.hexlify(packet).upper() + b"\n"
    if mode.startswith("CONFIG_UPDATE_ASCII_HEX:"):
        return mode.encode("ascii") + b"\n"
    if mode.startswith("BINARY_HEX:"):
        return bytes.fromhex(mode.split(":", 1)[1].strip())
    return b"OK\n"


def _decode_for_server(packet: bytes) -> Any:
    if decode_packet is None:
        return DecoderUnavailableResult(
            valid=False,
            packet_hex=packet.hex().upper(),
            packet_bytes=len(packet),
            errors=["decoder unavailable"],
            summary={},
        )
    return decode_packet(packet)


def decide_response(
    mode: str,
    packet: bytes,
    *,
    store: PendingUpdateStore = PENDING_STORE,
) -> ResponseDecision:
    decoded = _decode_for_server(packet)
    normalized = mode.strip().upper()
    if normalized != "AUTO":
        return ResponseDecision(build_manual_response(mode, packet), mode, decoded)
    if decode_packet is None:
        return ResponseDecision(b"ERROR:DECODER_UNAVAILABLE\n", "AUTO", decoded)
    if not decoded.valid:
        return ResponseDecision(b"ERROR:INVALID_PACKET\n", "AUTO", decoded)
    summary = decoded.summary
    imei = summary.get("imei")
    if summary.get("command") == COMMAND_HEARTBEAT and imei:
        record = store.get(imei)
        if record and record.get("status") == "pending":
            update_packet = bytes.fromhex(record["packet_hex"])
            return ResponseDecision(
                b"SUP\n" + update_packet,
                "AUTO",
                decoded,
                pending_update_id=int(record["update_id"]),
                pending_imei=imei,
                sends_settings_packet=True,
            )
    return ResponseDecision(b"OK\n", "AUTO", decoded)


def _write_text(lines: list[str]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    with LOG_LOCK:
        print(text, end="")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(text)


def write_trace(event: dict[str, Any]) -> None:
    TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": utc_timestamp(), **event}
    with LOG_LOCK:
        with TRACE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def log_diagnostics(connection_id: str, addr: tuple[str, int], diagnostics: list[str]) -> None:
    for message in diagnostics:
        _write_text([f"[{utc_timestamp()}] connection_id={connection_id} {addr[0]}:{addr[1]} parser: {message}"])
        write_trace({"event": "parser_diagnostic", "connection_id": connection_id, "remote_ip": addr[0], "remote_port": addr[1], "message": message})


def _summary_value(summary: dict[str, Any], name: str) -> Any:
    value = summary.get(name)
    return value if value is not None else "unavailable"


def _timestamp_utc(summary: dict[str, Any]) -> str:
    timestamp_ms = summary.get("timestamp_ms")
    if not isinstance(timestamp_ms, int) or timestamp_ms == 0:
        return "unavailable"
    try:
        seconds, milliseconds = divmod(timestamp_ms, 1000)
        value = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "unavailable"
    return value.strftime("%Y-%m-%dT%H:%M:%S") + f".{milliseconds:03d}Z"


def _header_text(summary: dict[str, Any]) -> str:
    header = summary.get("header")
    return f"0x{header:02X}" if isinstance(header, int) else "unavailable"


def log_packet(connection_id: str, addr: tuple[str, int], framed: FramedPacket, decision: ResponseDecision) -> None:
    decoded = decision.decoded
    summary = decoded.summary
    registry = summary.get("trusted_device_registry")
    validation = "decoder unavailable" if decode_packet is None else ("VALID" if decoded.valid else "INVALID")
    header_byte = _header_text(summary)
    sequence_id = _summary_value(summary, "sequence_id")
    timestamp_utc = _timestamp_utc(summary)
    lines = [
        f"\n[{utc_timestamp()}] connection_id={connection_id} remote={addr[0]}:{addr[1]}",
        "direction: simulator_to_primary_server",
        f"wire_mode: {framed.wire_mode}",
        f"packet_length: {len(framed.packet)} bytes",
        f"packet_hex: {framed.packet.hex().upper()}",
        f"validation: {validation}",
        f"header_byte: {header_byte}",
        f"sequence_id: {sequence_id}",
        f"timestamp_utc: {timestamp_utc}",
        f"command: {summary.get('command_name')}",
        f"imei: {summary.get('imei')}",
        f"heartbeat_format: {summary.get('heartbeat_format')}",
        f"trusted_device_registry: {json.dumps(registry, sort_keys=True) if registry is not None else 'not present'}",
    ]
    lines.extend(f"validation_error: {error}" for error in decoded.errors)
    lines.extend(
        [
            "direction: primary_server_to_simulator",
            f"response_mode: {decision.mode}",
            f"response_hex: {decision.response.hex().upper()}",
            f"response_ascii: {safe_ascii(decision.response)}",
            f"pending_update_id: {decision.pending_update_id}",
        ]
    )
    _write_text(lines)
    write_trace(
        {
            "event": "application_packet",
            "connection_id": connection_id,
            "remote_ip": addr[0],
            "remote_port": addr[1],
            "direction": "simulator_to_primary_server",
            "wire_mode": framed.wire_mode,
            "packet_type": summary.get("command_name"),
            "packet_length": len(framed.packet),
            "packet_hex": framed.packet.hex().upper(),
            "device_imei": summary.get("imei"),
            "validation": validation,
            "header_byte": header_byte,
            "sequence_id": sequence_id,
            "timestamp_utc": timestamp_utc,
            "validation_errors": decoded.errors,
            "parsed_fields": summary,
            "server_response_hex": decision.response.hex().upper(),
            "server_response_ascii": safe_ascii(decision.response),
            "pending_update_id": decision.pending_update_id,
        }
    )


def handle_client(conn: socket.socket, addr: tuple[str, int]) -> None:
    connection_id = uuid.uuid4().hex[:12]
    opened = time.monotonic()
    conn.settimeout(CONNECTION_TIMEOUT_SECONDS)
    parser = MicroStreamParser()
    _write_text([f"[{utc_timestamp()}] connection opened id={connection_id} remote={addr[0]}:{addr[1]}"])
    write_trace({"event": "connection_opened", "connection_id": connection_id, "remote_ip": addr[0], "remote_port": addr[1]})
    try:
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                _write_text([f"[{utc_timestamp()}] connection timeout id={connection_id}"])
                write_trace({"event": "connection_timeout", "connection_id": connection_id})
                break
            if not chunk:
                break
            _write_text([
                f"[{utc_timestamp()}] recv connection_id={connection_id} chunk_length={len(chunk)} raw_hex={chunk.hex().upper()} ascii={safe_ascii(chunk)}"
            ])
            write_trace({"event": "tcp_chunk", "connection_id": connection_id, "direction": "simulator_to_primary_server", "raw_length": len(chunk), "raw_hex": chunk.hex().upper()})
            framed_packets = parser.feed(chunk)
            if parser.diagnostics:
                log_diagnostics(connection_id, addr, parser.diagnostics)
                parser.diagnostics.clear()
            for framed in framed_packets:
                mode = read_response_mode()
                decision = decide_response(mode, framed.packet)
                log_packet(connection_id, addr, framed, decision)
                if decision.response:
                    try:
                        conn.sendall(decision.response)
                    except OSError as exc:
                        if decision.pending_imei:
                            PENDING_STORE.mark_failed(decision.pending_imei, str(exc))
                        raise
                    if decision.sends_settings_packet and decision.pending_imei:
                        PENDING_STORE.mark_sent(decision.pending_imei, connection_id)
                        write_trace({"event": "settings_update_sent", "connection_id": connection_id, "device_imei": decision.pending_imei, "update_id": decision.pending_update_id, "status": "sent_unconfirmed"})
    except Exception as exc:
        _write_text([f"[{utc_timestamp()}] connection error id={connection_id}: {exc}"])
        write_trace({"event": "connection_error", "connection_id": connection_id, "error": str(exc)})
        traceback.print_exc()
    finally:
        try:
            conn.close()
        except OSError:
            pass
        duration = time.monotonic() - opened
        _write_text([f"[{utc_timestamp()}] connection closed id={connection_id} duration_seconds={duration:.3f}"])
        write_trace({"event": "connection_closed", "connection_id": connection_id, "duration_seconds": round(duration, 3)})


def main() -> None:
    print(f"Starting Micro TCP test server on {HOST}:{PORT}")
    print(f"Packet log: {LOG_FILE}")
    print(f"Protocol trace: {TRACE_FILE}")
    print(f"Response mode file: {RESPONSE_MODE_FILE}")
    print(f"Pending updates: {PENDING_UPDATES_FILE}")
    print("Protocol: canonical Version 7 envelope, extended heartbeat, and command 0x02 configuration updates")
    print("Accepted wire modes: binary 0xAB... and ASCII-HEX text AB...")
    if not RESPONSE_MODE_FILE.exists():
        RESPONSE_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESPONSE_MODE_FILE.write_text("AUTO\n", encoding="utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(10)
        print("Waiting for persistent TCP connections...")
        while True:
            conn, addr = server.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    main()
