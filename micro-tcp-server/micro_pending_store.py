#!/usr/bin/env python3
"""Durable configuration queue and packet audit for the Micro TCP server."""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from micro_protocol import ProtocolError, decode_application_packet, validate_imei


SCHEMA = """
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY,
    target_imei TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    configuration TEXT NOT NULL,
    packet BLOB NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'sending', 'sent_unconfirmed', 'delivery_unknown',
        'failed', 'cancelled', 'superseded', 'removed'
    )),
    is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    send_count INTEGER NOT NULL DEFAULT 0,
    last_sent_at TEXT,
    last_connection_id TEXT,
    last_error TEXT,
    cancelled_at TEXT,
    requeued_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS updates_current_imei
    ON updates(target_imei) WHERE is_current = 1;
CREATE UNIQUE INDEX IF NOT EXISTS updates_sending_imei
    ON updates(target_imei) WHERE status = 'sending';
CREATE INDEX IF NOT EXISTS updates_imei_history ON updates(target_imei, id);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    timestamp_utc TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    remote_ip TEXT NOT NULL,
    remote_port INTEGER NOT NULL,
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload BLOB,
    imei TEXT,
    wire_mode TEXT,
    valid INTEGER CHECK (valid IN (0, 1)),
    details TEXT,
    update_id INTEGER
);
CREATE INDEX IF NOT EXISTS messages_imei_id ON messages(imei, id);
CREATE INDEX IF NOT EXISTS messages_connection_id ON messages(connection_id, id);
"""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def default_db_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("MICRO_DB") or "micro-data/micro.sqlite3")


def _queue_record(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    record["packet_hex"] = record.pop("packet").hex().upper()
    record["configuration"] = json.loads(record["configuration"])
    record["is_current"] = bool(record["is_current"])
    return record


def _decode_update(imei: str, packet: bytes) -> dict[str, Any]:
    validate_imei(imei)
    decoded = decode_application_packet(packet, target_imei=imei)
    if not decoded.get("valid") or decoded.get("command_name") != "configuration update":
        errors = decoded.get("errors") or ["Expected command 0x02."]
        raise ProtocolError(
            "Queued packet is not a valid canonical configuration update for the selected IMEI: "
            + "; ".join(errors)
        )
    return decoded["settings_update"]


class PendingUpdateStore:
    def __init__(self, path: pathlib.Path | str | None = None):
        self.path = pathlib.Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode = WAL")
            database.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA synchronous = FULL")
            with database:
                yield database
        finally:
            database.close()

    def list_records(self) -> dict[str, Any]:
        with self._connect() as database:
            rows = database.execute("SELECT * FROM updates WHERE is_current = 1 ORDER BY target_imei")
            return {row["target_imei"]: _queue_record(row) for row in rows}

    def get(self, imei: str) -> dict[str, Any] | None:
        validate_imei(imei)
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM updates WHERE target_imei = ? AND is_current = 1", (imei,)
            ).fetchone()
            return _queue_record(row)

    def queue_packet(
        self,
        *,
        imei: str,
        packet: bytes,
        settings: dict[str, Any] | None = None,
        source: str = "micro_update_tool",
    ) -> dict[str, Any]:
        update = _decode_update(imei, packet)
        if settings is None:
            settings = {
                field: update[field]
                for field in (
                    "heartbeat_interval_seconds", "lte_update_interval_seconds",
                    "ble_check_interval_seconds", "safe_zones", "beacons",
                    "trusted_devices", "sending_update",
                )
            }
        configuration = json.dumps(settings)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """UPDATE updates
                   SET is_current = 0,
                       status = CASE WHEN status = 'pending' THEN 'superseded' ELSE status END
                   WHERE target_imei = ? AND is_current = 1""",
                (imei,),
            )
            inserted = database.execute(
                """INSERT INTO updates
                   (target_imei, update_id, created_at, created_by, configuration, packet, status, is_current)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 1)""",
                (imei, update["update_id"], utc_timestamp(), source, configuration, packet),
            )
            row = database.execute("SELECT * FROM updates WHERE id = ?", (inserted.lastrowid,)).fetchone()
            return _queue_record(row)

    def claim_packet(self, imei: str, connection_id: str) -> dict[str, Any] | None:
        validate_imei(imei)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            claimed = database.execute(
                """UPDATE updates SET status = 'sending', last_connection_id = ?, last_error = NULL
                   WHERE target_imei = ? AND is_current = 1 AND status = 'pending'
                     AND NOT EXISTS (
                         SELECT 1 FROM updates WHERE target_imei = ? AND status = 'sending'
                     )""",
                (connection_id, imei, imei),
            )
            if not claimed.rowcount:
                return None
            row = database.execute(
                "SELECT * FROM updates WHERE target_imei = ? AND is_current = 1", (imei,)
            ).fetchone()
            return _queue_record(row)

    def finish_delivery(
        self,
        record_id: int,
        connection_id: str,
        *,
        status: str,
        error: str | None = None,
    ) -> bool:
        if status not in {"sent_unconfirmed", "failed", "delivery_unknown"}:
            raise ValueError(f"Unexpected delivery result {status!r}.")
        sent = int(status == "sent_unconfirmed")
        sent_at = utc_timestamp() if sent else None
        with self._connect() as database:
            done = database.execute(
                """UPDATE updates
                   SET status = ?, last_error = ?, send_count = send_count + ?,
                       last_sent_at = coalesce(?, last_sent_at)
                   WHERE id = ? AND status = 'sending' AND last_connection_id = ?""",
                (status, error, sent, sent_at, record_id, connection_id),
            )
            return bool(done.rowcount)

    def recover_interrupted_deliveries(self) -> int:
        """Close claims left by a previous server process, without sending them again."""
        with self._connect() as database:
            rows = database.execute(
                """UPDATE updates SET status = 'delivery_unknown',
                   last_error = 'The previous server stopped before recording the transmission outcome.'
                   WHERE status = 'sending'"""
            )
            return rows.rowcount

    def _change_current(self, imei: str, status: str) -> dict[str, Any] | None:
        validate_imei(imei)
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM updates WHERE target_imei = ? AND is_current = 1", (imei,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "sending":
                raise ProtocolError("This update is being transmitted. Check its status before trying again.")
            timestamp = utc_timestamp()
            database.execute(
                """UPDATE updates SET status = ?, is_current = ?,
                   cancelled_at = coalesce(?, cancelled_at), requeued_at = coalesce(?, requeued_at),
                   last_error = CASE WHEN ? = 'pending' THEN NULL ELSE last_error END
                   WHERE id = ?""",
                (
                    status, int(status != "removed"),
                    timestamp if status == "cancelled" else None,
                    timestamp if status == "pending" else None,
                    status, row["id"],
                ),
            )
            updated = database.execute("SELECT * FROM updates WHERE id = ?", (row["id"],)).fetchone()
            return _queue_record(updated)

    def cancel(self, imei: str) -> dict[str, Any] | None:
        return self._change_current(imei, "cancelled")

    def requeue(self, imei: str) -> dict[str, Any] | None:
        return self._change_current(imei, "pending")

    def remove(self, imei: str) -> bool:
        return self._change_current(imei, "removed") is not None

    def history(self, imei: str, limit: int = 20) -> list[dict[str, Any]]:
        validate_imei(imei)
        if not 1 <= limit <= 1000:
            raise ValueError("Limit must be between 1 and 1000.")
        with self._connect() as database:
            rows = database.execute(
                "SELECT * FROM updates WHERE target_imei = ? ORDER BY id DESC LIMIT ?", (imei, limit)
            )
            return [_queue_record(row) for row in rows]

    def record_message(
        self,
        *,
        connection_id: str,
        remote_ip: str,
        remote_port: int,
        direction: str,
        kind: str,
        payload: bytes | None,
        imei: str | None = None,
        wire_mode: str | None = None,
        valid: bool | None = None,
        details: dict[str, Any] | None = None,
        update_id: int | None = None,
    ) -> int:
        with self._connect() as database:
            inserted = database.execute(
                """INSERT INTO messages
                   (timestamp_utc, connection_id, remote_ip, remote_port, direction, kind,
                    payload, imei, wire_mode, valid, details, update_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    utc_timestamp(), connection_id, remote_ip, remote_port, direction, kind, payload,
                    imei, wire_mode, valid, json.dumps(details) if details is not None else None, update_id,
                ),
            )
            return inserted.lastrowid

    def messages(
        self,
        *,
        imei: str | None = None,
        connection_id: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Limit must be between 1 and 1000.")
        if imei is not None:
            validate_imei(imei)
        conditions = []
        parameters = []
        for column, value in (("imei", imei), ("connection_id", connection_id), ("kind", kind)):
            if value is not None:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as database:
            rows = database.execute(
                f"SELECT * FROM messages{where} ORDER BY id DESC LIMIT ?", (*parameters, limit)
            )
            results = []
            for row in rows:
                message = dict(row)
                payload = message.pop("payload")
                message["payload_hex"] = payload.hex().upper() if payload is not None else None
                message["valid"] = bool(message["valid"]) if message["valid"] is not None else None
                message["details"] = json.loads(message["details"]) if message["details"] is not None else None
                results.append(message)
            return results

    def devices(self) -> list[dict[str, Any]]:
        with self._connect() as database:
            rows = database.execute(
                """SELECT imei, min(timestamp_utc) AS first_seen_utc,
                   max(timestamp_utc) AS last_seen_utc, count(*) AS valid_packets
                   FROM messages WHERE imei IS NOT NULL AND direction = 'inbound' AND valid = 1
                   GROUP BY imei ORDER BY last_seen_utc DESC, imei"""
            )
            return [dict(row) for row in rows]

    def backup(self, target: pathlib.Path | str) -> None:
        """Take a consistent SQLite snapshot, including commits in the live WAL."""
        destination = pathlib.Path(target)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        try:
            with self._connect() as source:
                output = sqlite3.connect(destination)
                try:
                    source.backup(output)
                finally:
                    output.close()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def import_legacy(self, path: pathlib.Path | str) -> dict[str, list[str]]:
        """Import the previous JSON store once, keeping its delivery statuses."""
        source = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        if not isinstance(source, dict) or not isinstance(source.get("devices"), dict):
            raise ProtocolError("Previous store must contain a JSON object named devices.")
        result: dict[str, list[str]] = {"imported": [], "skipped": []}
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            for imei, record in source["devices"].items():
                if database.execute("SELECT 1 FROM updates WHERE target_imei = ?", (imei,)).fetchone():
                    result["skipped"].append(imei)
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("packet_hex"), str):
                    raise ProtocolError(f"Previous store record for {imei!r} is missing packet_hex.")
                packet = bytes.fromhex(record["packet_hex"])
                update = _decode_update(imei, packet)
                status = record.get("status")
                if status not in ("pending", "sent_unconfirmed", "cancelled", "failed"):
                    raise ProtocolError(f"Previous store record for {imei!r} has an invalid status.")
                database.execute(
                    """INSERT INTO updates
                       (target_imei, update_id, created_at, created_by, configuration, packet, status,
                        is_current, send_count, last_sent_at, last_connection_id, last_error,
                        cancelled_at, requeued_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                    (
                        imei, update["update_id"], record.get("created_at") or utc_timestamp(),
                        record.get("created_by") or "import-legacy",
                        json.dumps(record.get("configuration") or {}), packet, status,
                        int(record.get("send_count") or 0), record.get("last_sent_at"),
                        record.get("last_connection_id"), record.get("last_error"),
                        record.get("cancelled_at"), record.get("requeued_at"),
                    ),
                )
                result["imported"].append(imei)
        return result
