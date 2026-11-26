#!/usr/bin/env python3
"""Incremental parser for server tokens and canonical binary V8 configuration packets."""
from __future__ import annotations
from dataclasses import dataclass
from micro_protocol import FIXED_PREFIX_BYTES, HEADER, PROPERTY


@dataclass(frozen=True)
class ResponseEvent:
    kind: str
    raw: bytes
    packet: bytes | None = None


class ServerResponseParser:
    """Parse the canonical V8 heartbeat response stream.

    Normal successful heartbeat responses are either ``OK\n`` or one complete
    binary Micro command-0x02 configuration packet.  ``SUP`` is accepted only
    as a legacy diagnostic token; it is not needed to gate binary parsing.
    """

    def __init__(self, max_packet_bytes: int = 4096):
        self.buffer = bytearray()
        self.max_packet_bytes = max_packet_bytes

    def feed(self, data: bytes) -> list[ResponseEvent]:
        if len(data) > self.max_packet_bytes - len(self.buffer):
            raw = bytes(self.buffer) + data
            self.buffer.clear()
            return [ResponseEvent("RESPONSE_BUFFER_OVERFLOW", raw)]

        self.buffer.extend(data)
        events: list[ResponseEvent] = []
        while True:
            if self.buffer[:1] == bytes((HEADER,)):
                event = self._extract_binary_settings()
                if event is None:
                    break
                events.append(event)
                continue

            newline = self.buffer.find(b"\n")
            if newline < 0:
                break

            raw = bytes(self.buffer[: newline + 1])
            del self.buffer[: newline + 1]
            token = raw.strip().decode("ascii", errors="replace")
            if token == "OK":
                events.append(ResponseEvent("OK", raw))
            elif token.startswith("ERROR"):
                events.append(ResponseEvent("ERROR", raw))
            elif token == "SUP":
                events.append(ResponseEvent("LEGACY_SUP", raw))
            elif token == "FWUP":
                events.append(ResponseEvent("FWUP", raw))
            else:
                events.append(ResponseEvent("UNKNOWN", raw))
        return events

    def connection_closed(self) -> ResponseEvent | None:
        if not self.buffer:
            return None
        raw = bytes(self.buffer)
        self.buffer.clear()
        if raw[:1] == bytes((HEADER,)):
            return ResponseEvent("INCOMPLETE_SETTINGS", raw)
        return ResponseEvent("INCOMPLETE_TOKEN", raw)

    def timeout(self) -> ResponseEvent:
        """Clear an incomplete response and return a diagnostic event."""
        raw = bytes(self.buffer)
        self.buffer.clear()
        if raw[:1] == bytes((HEADER,)):
            return ResponseEvent("SETTINGS_TIMEOUT", raw)
        return ResponseEvent("RESPONSE_TIMEOUT", raw)

    def _extract_binary_settings(self) -> ResponseEvent | None:
        if not self.buffer:
            return None
        if self.buffer[0] != HEADER:
            return None
        if len(self.buffer) < 2:
            return None
        if self.buffer[1] != PROPERTY:
            raw = bytes(self.buffer[:1])
            del self.buffer[:1]
            return ResponseEvent("INVALID_SETTINGS_PREFIX", raw)
        if len(self.buffer) < 4:
            return None

        declared = int.from_bytes(self.buffer[2:4], "big")
        total = FIXED_PREFIX_BYTES + declared
        if declared < 1 or total > self.max_packet_bytes:
            raw = bytes(self.buffer[:4])
            del self.buffer[:4]
            return ResponseEvent("INVALID_SETTINGS_LENGTH", raw)
        if len(self.buffer) < total:
            return None

        packet = bytes(self.buffer[:total])
        del self.buffer[:total]
        if packet[FIXED_PREFIX_BYTES] != 0x02:
            return ResponseEvent("UNEXPECTED_BINARY_COMMAND", packet, packet)
        return ResponseEvent("SETTINGS_PACKET", packet, packet)
