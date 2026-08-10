from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
import types
import unittest
from pathlib import Path


RELAY_PATH = Path(__file__).resolve().parents[1] / "micro_relay" / "micro_serial_wifi_relay.py"
if "serial" not in sys.modules:
    serial_stub = types.ModuleType("serial")
    serial_stub.SerialException = OSError
    serial_stub.Serial = object
    sys.modules["serial"] = serial_stub
SPEC = importlib.util.spec_from_file_location("micro_serial_wifi_relay_for_test", RELAY_PATH)
assert SPEC is not None and SPEC.loader is not None
relay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = relay
SPEC.loader.exec_module(relay)


SETTINGS_PACKET = bytes.fromhex(
    "AB10001A63A80001203836313335323036343035303738370100010101020002003C"
)


class RelayResponseFramingTests(unittest.TestCase):
    def test_ok_split_requires_newline(self) -> None:
        self.assertFalse(relay.response_is_complete(b"O"))
        self.assertFalse(relay.response_is_complete(b"OK"))
        self.assertTrue(relay.response_is_complete(b"OK\n"))

    def test_sup_requires_complete_following_packet(self) -> None:
        self.assertFalse(relay.response_is_complete(b"SUP\n"))
        self.assertFalse(relay.response_is_complete(b"SUP\n" + SETTINGS_PACKET[:11]))
        self.assertTrue(relay.response_is_complete(b"SUP\n" + SETTINGS_PACKET))

    def test_raw_binary_packet_uses_length(self) -> None:
        self.assertFalse(relay.response_is_complete(SETTINGS_PACKET[:8]))
        self.assertTrue(relay.response_is_complete(SETTINGS_PACKET))

    def test_fragmented_sup_transaction_waits_beyond_old_drain_window(self) -> None:
        client_sock, server_sock = socket.socketpair()
        client = relay.PersistentTcpClient(
            relay.Endpoint("unused", 1), connect_timeout=1.0, response_timeout=1.0
        )
        client.sock = client_sock

        def server() -> None:
            try:
                self.assertEqual(server_sock.recv(32), b"heartbeat")
                server_sock.sendall(b"S")
                time.sleep(0.08)
                server_sock.sendall(b"UP\n" + SETTINGS_PACKET[:10])
                time.sleep(0.08)
                server_sock.sendall(SETTINGS_PACKET[10:])
            finally:
                server_sock.close()

        thread = threading.Thread(target=server)
        thread.start()
        try:
            received = client.send_and_receive(b"heartbeat")
            self.assertEqual(received, b"SUP\n" + SETTINGS_PACKET)
        finally:
            client.close()
            thread.join(timeout=2.0)


if __name__ == "__main__":
    unittest.main()
