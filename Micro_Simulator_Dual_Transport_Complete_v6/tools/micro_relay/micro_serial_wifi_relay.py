#!/usr/bin/env python3
"""Windows serial-to-Wi-Fi TCP relay for the Micro nRF9151 simulator.

The script owns the nRF9151 DK COM port and acts as the user's terminal.
It forwards tagged packet lines from the board to a TCP server using the
Windows laptop's network connection, then returns the TCP response to the
board as HEX-safe serial text.

Board -> relay messages:
    MICRO_RELAY_CONNECT:<host>:<port>
    MICRO_RELAY_DISCONNECT
    MICRO_RELAY_TX_ASCII_HEX:<hex packet>
    MICRO_RELAY_TX_BINARY_HEX:<hex packet>

Relay -> board messages:
    MICRO_RELAY_CONNECTED:<host>:<port>
    MICRO_RELAY_DISCONNECTED
    MICRO_RELAY_RX_HEX:<hex response>
    MICRO_RELAY_RX_EMPTY
    MICRO_RELAY_RX_ERROR:<error text>
"""

from __future__ import annotations

import argparse
import json
import pathlib
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import serial
except ImportError as exc:  # pragma: no cover - user-facing startup error
    raise SystemExit(
        "pyserial is not installed. Run: py -m pip install pyserial"
    ) from exc




class RelayLogger:
    """Append local relay protocol events as JSON Lines."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def write(self, event: str, **values: object) -> None:
        record = {
            "timestamp_unix": time.time(),
            "event": event,
            **values,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

@dataclass
class Endpoint:
    host: str
    port: int


MAX_VERSION7_PACKET_BYTES = 65543  # 8-byte fixed prefix + uint16 Length


def _version7_packet_total_length(data: bytes, offset: int = 0) -> Optional[int]:
    """Return a complete Version 7 packet size, or None until the prefix is complete."""
    if len(data) < offset + 4:
        return None
    if data[offset : offset + 2] != b"\xAB\x10":
        raise ValueError("response packet does not begin with AB10")
    declared = int.from_bytes(data[offset + 2 : offset + 4], "big")
    total = 8 + declared
    if declared < 1 or total > MAX_VERSION7_PACKET_BYTES:
        raise ValueError(f"invalid Version 7 response packet length: {declared}")
    return total


def response_is_complete(data: bytes) -> bool:
    """Return True when one complete server response transaction is buffered.

    Text responses end with a newline. SUP is special: the newline must be
    followed by one complete binary Version 7 packet. Raw binary diagnostic
    responses that begin with AB10 are also length-framed.
    """
    newline = data.find(b"\n")
    if newline >= 0:
        token = data[:newline].rstrip(b"\r ")
        if token != b"SUP":
            return True
        packet_offset = newline + 1
        if len(data) < packet_offset + 4:
            return False
        total = _version7_packet_total_length(data, packet_offset)
        assert total is not None
        return len(data) >= packet_offset + total

    if data.startswith(b"\xAB\x10"):
        total = _version7_packet_total_length(data)
        return total is not None and len(data) >= total
    return False


class PersistentTcpClient:
    """Thread-safe persistent TCP client with reconnect support."""

    def __init__(self, endpoint: Endpoint, connect_timeout: float, response_timeout: float) -> None:
        self.endpoint = endpoint
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def set_endpoint(self, endpoint: Endpoint) -> None:
        with self.lock:
            if endpoint != self.endpoint:
                self._close_unlocked()
            self.endpoint = endpoint

    def connect(self) -> None:
        with self.lock:
            self._connect_unlocked()

    def _connect_unlocked(self) -> None:
        self._close_unlocked()
        sock = socket.create_connection(
            (self.endpoint.host, self.endpoint.port),
            timeout=self.connect_timeout,
        )
        sock.settimeout(self.response_timeout)
        self.sock = sock

    def close(self) -> None:
        with self.lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def send_and_receive(self, payload: bytes) -> bytes:
        """Send one payload and receive one complete response transaction.

        TCP may split a response token or the binary settings packet across many
        reads. This method therefore buffers until it has a newline-terminated
        response, or, for SUP, until the following Version 7 packet is complete.
        Raw AB10-framed diagnostic packets are also read by their Length field.
        """
        with self.lock:
            if self.sock is None:
                self._connect_unlocked()

            assert self.sock is not None
            previous_timeout = self.sock.gettimeout()
            try:
                self.sock.sendall(payload)
                response = bytearray()
                deadline = time.monotonic() + self.response_timeout

                while time.monotonic() < deadline:
                    remaining = max(0.001, deadline - time.monotonic())
                    self.sock.settimeout(min(0.25, remaining))
                    try:
                        chunk = self.sock.recv(4096)
                    except socket.timeout:
                        continue

                    if chunk == b"":
                        if response:
                            raise ConnectionError(
                                "TCP server closed the connection before the response completed"
                            )
                        raise ConnectionError("TCP server closed the connection")

                    response.extend(chunk)
                    try:
                        if response_is_complete(bytes(response)):
                            return bytes(response)
                    except ValueError:
                        # Return the complete bytes to the board so its normal
                        # response parser can log and reject the malformed data.
                        return bytes(response)

                # Preserve partial/diagnostic bytes for board-side logging. An
                # entirely empty timeout remains the legacy no-response result.
                return bytes(response)
            except Exception:
                self._close_unlocked()
                raise
            finally:
                if self.sock is not None:
                    self.sock.settimeout(previous_timeout)


class MicroRelay:
    def __init__(
        self,
        com_port: str,
        baud: int,
        endpoint: Endpoint,
        connect_timeout: float,
        response_timeout: float,
        log_path: pathlib.Path,
    ) -> None:
        self.ser = serial.Serial(com_port, baudrate=baud, timeout=0.05)
        self.serial_write_lock = threading.Lock()
        self.tcp = PersistentTcpClient(endpoint, connect_timeout, response_timeout)
        self.stop_event = threading.Event()
        self.line_buffer = ""
        self.logger = RelayLogger(log_path)
        self.logger.write("relay_started", serial_port=com_port, baud=baud, host=endpoint.host, port=endpoint.port)

    def serial_write_line(self, text: str) -> None:
        clean = text.replace("\r", " ").replace("\n", " ")
        with self.serial_write_lock:
            self.ser.write((clean + "\n").encode("utf-8", errors="replace"))
            self.ser.flush()

    def send_board_command(self, command: str) -> None:
        with self.serial_write_lock:
            self.ser.write((command + "\n").encode("utf-8"))
            self.ser.flush()

    def _notify_error(self, exc: Exception) -> None:
        message = str(exc).replace("\r", " ").replace("\n", " ")
        print(f"\n[relay] ERROR: {message}")
        if hasattr(self, "logger"):
            self.logger.write("relay_error", message=message)
        self.serial_write_line(f"MICRO_RELAY_RX_ERROR:{message}")

    def _handle_connect(self, payload: str) -> None:
        try:
            host, port_text = payload.rsplit(":", 1)
            endpoint = Endpoint(host=host.strip(), port=int(port_text))
            if not endpoint.host or not (1 <= endpoint.port <= 65535):
                raise ValueError("invalid TCP endpoint")

            self.tcp.set_endpoint(endpoint)
            self.tcp.connect()
            print(f"\n[relay] TCP connected to {endpoint.host}:{endpoint.port}")
            self.logger.write("tcp_connected", host=endpoint.host, port=endpoint.port)
            self.serial_write_line(
                f"MICRO_RELAY_CONNECTED:{endpoint.host}:{endpoint.port}"
            )
        except Exception as exc:
            self._notify_error(exc)

    def _handle_disconnect(self) -> None:
        endpoint = self.tcp.endpoint
        self.tcp.close()
        self.logger.write("tcp_disconnected", host=endpoint.host, port=endpoint.port)
        print("\n[relay] TCP disconnected")
        self.serial_write_line("MICRO_RELAY_DISCONNECTED")

    def _handle_tx(self, hex_payload: str, binary: bool) -> None:
        try:
            clean_hex = "".join(hex_payload.split())
            if len(clean_hex) == 0 or len(clean_hex) % 2 != 0:
                raise ValueError("packet HEX is empty or has an odd length")

            raw_bytes = bytes.fromhex(clean_hex)
            tcp_payload = raw_bytes if binary else clean_hex.encode("ascii")

            mode_name = "binary" if binary else "ASCII hex"
            endpoint = self.tcp.endpoint
            print(
                f"\n[relay] Sending {len(tcp_payload)} bytes ({mode_name}) "
                f"to {endpoint.host}:{endpoint.port}"
            )

            self.logger.write(
                "tcp_send",
                host=endpoint.host,
                port=endpoint.port,
                wire_mode="binary" if binary else "ascii_hex",
                byte_count=len(tcp_payload),
                raw_hex=tcp_payload.hex().upper(),
            )
            response = self.tcp.send_and_receive(tcp_payload)
            if response:
                print(
                    f"[relay] Received {len(response)} response bytes: "
                    f"{response.hex().upper()}"
                )
                try:
                    decoded = response.decode("utf-8")
                    print(f"[relay] Response text: {decoded.rstrip()}")
                except UnicodeDecodeError:
                    pass
                self.logger.write(
                    "tcp_receive",
                    host=endpoint.host,
                    port=endpoint.port,
                    byte_count=len(response),
                    raw_hex=response.hex().upper(),
                )
                serial_line = f"MICRO_RELAY_RX_HEX:{response.hex().upper()}"
                self.serial_write_line(serial_line)
                self.logger.write("serial_control_to_board", marker="MICRO_RELAY_RX_HEX", byte_count=len(response))
            else:
                print("[relay] Server returned no response before timeout")
                self.logger.write("tcp_response_timeout", host=endpoint.host, port=endpoint.port)
                self.serial_write_line("MICRO_RELAY_RX_EMPTY")
        except Exception as exc:
            self._notify_error(exc)

    def handle_board_line(self, raw_line: str) -> None:
        # A serial terminal prompt or echoed command can precede the protocol
        # marker. Search for the marker instead of requiring column zero.
        marker_pos = raw_line.find("MICRO_RELAY_")
        if marker_pos < 0:
            return

        line = raw_line[marker_pos:].strip()

        if line.startswith("MICRO_RELAY_CONNECT:"):
            self._handle_connect(line.split(":", 1)[1])
        elif line == "MICRO_RELAY_DISCONNECT":
            self._handle_disconnect()
        elif line.startswith("MICRO_RELAY_TX_ASCII_HEX:"):
            self._handle_tx(line.split(":", 1)[1], binary=False)
        elif line.startswith("MICRO_RELAY_TX_BINARY_HEX:"):
            self._handle_tx(line.split(":", 1)[1], binary=True)

    def serial_reader_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                data = self.ser.read(self.ser.in_waiting or 1)
                if not data:
                    continue

                text = data.decode("utf-8", errors="replace")
                sys.stdout.write(text)
                sys.stdout.flush()

                self.line_buffer += text
                while "\n" in self.line_buffer:
                    line, self.line_buffer = self.line_buffer.split("\n", 1)
                    self.handle_board_line(line.rstrip("\r"))
            except serial.SerialException as exc:
                print(f"\n[relay] Serial error: {exc}")
                self.stop_event.set()
            except Exception as exc:
                print(f"\n[relay] Reader error: {exc}")
                time.sleep(0.2)

    def run(self) -> int:
        reader = threading.Thread(target=self.serial_reader_loop, daemon=True)
        reader.start()

        print("[relay] Windows Wi-Fi relay started")
        print(f"[relay] Serial port: {self.ser.port} @ {self.ser.baudrate}")
        print(
            f"[relay] Initial TCP endpoint: "
            f"{self.tcp.endpoint.host}:{self.tcp.endpoint.port}"
        )
        print("[relay] This window is now the board's serial terminal.")
        print("[relay] Local commands: /quit, /tcpstatus, /reconnect")
        print("[relay] Board example: transport relay")
        print("[relay] Board example: connect 137.184.163.176 5000")

        try:
            while not self.stop_event.is_set():
                try:
                    command = input()
                except EOFError:
                    break

                stripped = command.strip()
                if stripped == "/quit":
                    break
                if stripped == "/tcpstatus":
                    endpoint = self.tcp.endpoint
                    status = "connected" if self.tcp.connected else "disconnected"
                    print(f"[relay] TCP {status}: {endpoint.host}:{endpoint.port}")
                    continue
                if stripped == "/reconnect":
                    try:
                        self.tcp.connect()
                        endpoint = self.tcp.endpoint
                        print(f"[relay] TCP connected to {endpoint.host}:{endpoint.port}")
                        self.serial_write_line(
                            f"MICRO_RELAY_CONNECTED:{endpoint.host}:{endpoint.port}"
                        )
                    except Exception as exc:
                        self._notify_error(exc)
                    continue

                self.send_board_command(command)
        except KeyboardInterrupt:
            print("\n[relay] Stopping")
        finally:
            self.stop_event.set()
            self.tcp.close()
            try:
                self.ser.close()
            except serial.SerialException:
                pass
            reader.join(timeout=1.0)

        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Micro nRF9151 serial-to-Wi-Fi TCP relay for Windows"
    )
    parser.add_argument("--port", required=True, help="Board COM port, for example COM7")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--host", default="137.184.163.176")
    parser.add_argument("--tcp-port", type=int, default=5000)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--response-timeout", type=float, default=2.0)
    parser.add_argument(
        "--log-file",
        type=pathlib.Path,
        default=pathlib.Path(__file__).with_name("micro_relay_trace.jsonl"),
        help="Local JSON Lines relay log.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    endpoint = Endpoint(args.host, args.tcp_port)

    try:
        relay = MicroRelay(
            com_port=args.port,
            baud=args.baud,
            endpoint=endpoint,
            connect_timeout=args.connect_timeout,
            response_timeout=args.response_timeout,
            log_path=args.log_file,
        )
    except serial.SerialException as exc:
        print(f"Could not open {args.port}: {exc}", file=sys.stderr)
        print("Close the nRF Connect serial terminal and try again.", file=sys.stderr)
        return 2

    return relay.run()


if __name__ == "__main__":
    raise SystemExit(main())
