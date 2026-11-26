"""Regression coverage for durable claims, update history and packet audit."""

import json
import pathlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from micro_pending_store import PendingUpdateStore
from micro_protocol import ProtocolError, build_settings_update_packet, default_settings


IMEI = "861352064050787"
OTHER_IMEI = "861352064050788"


def update(imei: str = IMEI, update_id: int = 55) -> bytes:
    return build_settings_update_packet(
        target_imei=imei, update_id=update_id, settings=default_settings(),
    )


class PendingUpdateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = pathlib.Path(directory.name) / "store.sqlite3"
        self.store = PendingUpdateStore(self.path)

    def test_simultaneous_heartbeats_claim_only_once(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update())
        start = Barrier(2, timeout=5)

        def claim(connection_id: str):
            reopened = PendingUpdateStore(self.path)
            start.wait()
            return reopened.claim_packet(IMEI, connection_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, ["first", "second"]))
        winner, = [record for record in claims if record is not None]
        self.assertTrue(self.store.finish_delivery(
            winner["id"], winner["last_connection_id"], status="sent_unconfirmed",
        ))
        self.assertEqual(self.store.get(IMEI)["send_count"], 1)
        self.assertIsNone(self.store.claim_packet(IMEI, "third"))

    def test_new_packet_stays_pending_when_old_claim_finishes(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update(update_id=1))
        old = self.store.claim_packet(IMEI, "old")
        new = self.store.queue_packet(imei=IMEI, packet=update(update_id=2))
        self.store.queue_packet(imei=OTHER_IMEI, packet=update(OTHER_IMEI))

        self.assertIsNone(self.store.claim_packet(IMEI, "new"))
        self.assertIsNotNone(self.store.claim_packet(OTHER_IMEI, "other"))
        self.assertFalse(self.store.finish_delivery(old["id"], "wrong", status="sent_unconfirmed"))
        self.assertTrue(self.store.finish_delivery(old["id"], "old", status="sent_unconfirmed"))
        self.assertEqual(self.store.get(IMEI)["status"], "pending")
        self.assertEqual(self.store.history(IMEI)[1]["status"], "sent_unconfirmed")
        self.assertEqual(self.store.claim_packet(IMEI, "new")["id"], new["id"])

    def test_recovery_requires_explicit_retry(self) -> None:
        old = self.store.queue_packet(imei=IMEI, packet=update(update_id=1))
        new = self.store.queue_packet(imei=IMEI, packet=update(update_id=2))
        self.store.claim_packet(IMEI, "interrupted")
        self.assertEqual(self.store.recover_interrupted_deliveries(), 1)

        self.assertIsNone(self.store.claim_packet(IMEI, "after-restart"))
        history = self.store.history(IMEI)
        self.assertEqual([(r["id"], r["status"]) for r in history], [
            (new["id"], "delivery_unknown"),
            (old["id"], "superseded"),
        ])
        self.store.requeue(IMEI)
        self.assertEqual(self.store.claim_packet(IMEI, "manual")["id"], new["id"])
        with self.assertRaises(ProtocolError):
            self.store.remove(IMEI)
        self.assertTrue(self.store.finish_delivery(
            new["id"], "manual", status="failed", error="timeout",
        ))
        self.assertTrue(self.store.remove(IMEI))
        self.assertIsNone(self.store.get(IMEI))
        self.assertEqual(self.store.history(IMEI)[0]["last_error"], "timeout")

    def test_message_filter_and_backup(self) -> None:
        self.store.queue_packet(imei=IMEI, packet=update())
        for imei, valid in ((IMEI, True), (IMEI, False), (OTHER_IMEI, True)):
            self.store.record_message(
                connection_id="session", remote_ip="127.0.0.1", remote_port=1234,
                direction="inbound", kind="packet", imei=imei, payload=b"\xab\x10",
                wire_mode="binary", valid=valid, details={"sequence_id": 1},
            )
        self.store.record_message(
            connection_id="session", remote_ip="127.0.0.1", remote_port=1234,
            direction="inbound", kind="raw_chunk", payload=b"AB10",
        )
        snapshot = self.path.with_name("snapshot.sqlite3")
        self.store.backup(snapshot)
        with self.assertRaises(FileExistsError):
            self.store.backup(snapshot)
        backup = PendingUpdateStore(snapshot)
        self.assertEqual(backup.get(IMEI), self.store.get(IMEI))
        self.assertEqual(len(backup.messages()), 4)
        self.assertEqual(backup.messages(imei=IMEI, limit=1)[0]["payload_hex"], "AB10")
        self.assertFalse(backup.messages(imei=IMEI, limit=1)[0]["valid"])
        self.assertEqual(backup.messages(connection_id="session", kind="raw_chunk")[0]["payload_hex"], "41423130")
        self.assertEqual(len(backup.messages(imei=IMEI, connection_id="session", kind="packet")), 2)
        self.assertEqual(backup.messages(connection_id="another-session"), [])
        self.assertEqual({item["imei"]: item["valid_packets"] for item in backup.devices()}, {
            IMEI: 1, OTHER_IMEI: 1,
        })

    def test_import_preserves_previous_transmission_state(self) -> None:
        legacy = self.path.with_name("pending.json")
        legacy.write_text(json.dumps({"version": 1, "devices": {
            IMEI: {"packet_hex": update().hex(), "status": "sent_unconfirmed", "send_count": 1},
            OTHER_IMEI: {"packet_hex": update(OTHER_IMEI).hex(), "status": "pending"},
        }}), encoding="utf-8")
        self.assertEqual(self.store.import_legacy(legacy), {
            "imported": [IMEI, OTHER_IMEI], "skipped": [],
        })
        self.assertIsNone(self.store.claim_packet(IMEI, "after-migration"))
        self.assertEqual(self.store.get(IMEI)["send_count"], 1)
        self.assertEqual(self.store.claim_packet(OTHER_IMEI, "after-migration")["status"], "sending")
        self.assertEqual(self.store.import_legacy(legacy), {
            "imported": [], "skipped": [IMEI, OTHER_IMEI],
        })


if __name__ == "__main__":
    unittest.main()
