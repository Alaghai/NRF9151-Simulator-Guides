#!/usr/bin/env python3
"""Atomic JSON development store for pending settings updates by IMEI."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from micro_protocol import ProtocolError, decode_application_packet, validate_imei


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PendingUpdateStore:
    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        self.lock = threading.RLock()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "devices": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"Could not read pending-update store: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("devices", {}), dict):
            raise ProtocolError("Pending-update store has an invalid structure.")
        data.setdefault("version", 1)
        data.setdefault("devices", {})
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def list_records(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy(self._read_unlocked().get("devices", {}))

    def get(self, imei: str) -> dict[str, Any] | None:
        validate_imei(imei)
        with self.lock:
            record = self._read_unlocked().get("devices", {}).get(imei)
            return deepcopy(record) if record else None

    def queue_packet(
        self,
        *,
        imei: str,
        packet: bytes,
        settings: list[dict[str, Any]] | None = None,
        source: str = "micro_update_tool",
    ) -> dict[str, Any]:
        validate_imei(imei)
        decoded = decode_application_packet(packet, target_imei=imei)
        if not decoded.get("valid") or decoded.get("command_name") != "configuration update":
            raise ProtocolError("Queued packet is not a valid canonical configuration update for the selected IMEI: " + "; ".join(decoded.get("errors", [])))
        update = decoded["settings_update"]
        record = {
            "target_imei": imei,
            "update_id": update["update_id"],
            "created_at": utc_timestamp(),
            "created_by": source,
            "configuration": settings if settings is not None else {
                "heartbeat_interval_seconds": update["heartbeat_interval_seconds"],
                "lte_update_interval_seconds": update["lte_update_interval_seconds"],
                "ble_check_interval_seconds": update["ble_check_interval_seconds"],
                "safe_zones": update["safe_zones"],
                "beacons": update["beacons"],
                "trusted_devices": update["trusted_devices"],
                "sending_update": update["sending_update"],
            },
            "packet_hex": packet.hex().upper(),
            "status": "pending",
            "send_count": 0,
            "last_sent_at": None,
            "last_connection_id": None,
            "last_error": None,
        }
        with self.lock:
            data = self._read_unlocked()
            data["devices"][imei] = record
            self._write_unlocked(data)
        return deepcopy(record)

    def mark_sent(self, imei: str, connection_id: str) -> dict[str, Any] | None:
        with self.lock:
            data = self._read_unlocked()
            record = data.get("devices", {}).get(imei)
            if not record:
                return None
            record["status"] = "sent_unconfirmed"
            record["send_count"] = int(record.get("send_count", 0)) + 1
            record["last_sent_at"] = utc_timestamp()
            record["last_connection_id"] = connection_id
            record["last_error"] = None
            self._write_unlocked(data)
            return deepcopy(record)

    def mark_failed(self, imei: str, error: str) -> None:
        with self.lock:
            data = self._read_unlocked()
            record = data.get("devices", {}).get(imei)
            if record:
                record["status"] = "failed"
                record["last_error"] = error
                self._write_unlocked(data)

    def cancel(self, imei: str) -> dict[str, Any] | None:
        with self.lock:
            data = self._read_unlocked()
            record = data.get("devices", {}).get(imei)
            if not record:
                return None
            record["status"] = "cancelled"
            record["cancelled_at"] = utc_timestamp()
            self._write_unlocked(data)
            return deepcopy(record)

    def requeue(self, imei: str) -> dict[str, Any] | None:
        with self.lock:
            data = self._read_unlocked()
            record = data.get("devices", {}).get(imei)
            if not record:
                return None
            record["status"] = "pending"
            record["requeued_at"] = utc_timestamp()
            record["last_error"] = None
            self._write_unlocked(data)
            return deepcopy(record)

    def remove(self, imei: str) -> bool:
        with self.lock:
            data = self._read_unlocked()
            if imei not in data.get("devices", {}):
                return False
            del data["devices"][imei]
            self._write_unlocked(data)
            return True
