#!/usr/bin/env python3
"""Incremental parser for server text tokens and SUP settings packets."""
from __future__ import annotations
from dataclasses import dataclass
from micro_protocol import FIXED_PREFIX_BYTES, HEADER, PROPERTY

@dataclass(frozen=True)
class ResponseEvent:
    kind: str
    raw: bytes
    packet: bytes | None = None

class ServerResponseParser:
    def __init__(self, max_packet_bytes: int = 4096):
        self.buffer = bytearray()
        self.awaiting_settings = False
        self.max_packet_bytes = max_packet_bytes

    def feed(self, data: bytes) -> list[ResponseEvent]:
        self.buffer.extend(data)
        events: list[ResponseEvent] = []
        while True:
            if self.awaiting_settings:
                event = self._extract_settings()
                if event is None:
                    break
                events.append(event)
                self.awaiting_settings = False
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
                events.append(ResponseEvent("SUP", raw))
                self.awaiting_settings = True
            elif token == "FWUP":
                events.append(ResponseEvent("FWUP", raw))
            else:
                events.append(ResponseEvent("UNKNOWN", raw))
        return events

    def connection_closed(self) -> ResponseEvent | None:
        if self.awaiting_settings and self.buffer:
            raw = bytes(self.buffer)
            self.buffer.clear()
            self.awaiting_settings = False
            return ResponseEvent("INCOMPLETE_SETTINGS", raw)
        if self.buffer:
            raw = bytes(self.buffer)
            self.buffer.clear()
            return ResponseEvent("INCOMPLETE_TOKEN", raw)
        return None

    def timeout(self) -> ResponseEvent:
        """Clear an incomplete transaction and return a diagnostic event."""
        raw = bytes(self.buffer)
        self.buffer.clear()
        if self.awaiting_settings:
            self.awaiting_settings = False
            return ResponseEvent("SETTINGS_TIMEOUT", raw)
        return ResponseEvent("RESPONSE_TIMEOUT", raw)

    def _extract_settings(self) -> ResponseEvent | None:
        if not self.buffer:
            return None
        if self.buffer[0] != HEADER:
            # Preserve enough context for diagnostics but avoid unbounded growth.
            raw = bytes(self.buffer)
            self.buffer.clear()
            return ResponseEvent("INVALID_SETTINGS_PREFIX", raw)
        if len(self.buffer) < 4:
            return None
        if self.buffer[1] != PROPERTY:
            raw = bytes(self.buffer[:2])
            del self.buffer[:2]
            return ResponseEvent("INVALID_SETTINGS_PREFIX", raw)
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
        return ResponseEvent("SETTINGS_PACKET", packet, packet)
