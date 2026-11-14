"""Persistent storage for message sets and the history of sent messages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .messages import MessageError, normalize_set

_LOGGER = logging.getLogger(__name__)
MAX_HISTORY = 200


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        backup = path.with_suffix(path.suffix + ".broken")
        _LOGGER.error("Could not read %s (%s); moved it to %s", path, err, backup)
        path.replace(backup)
        return default


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class Store:
    """Message sets live in message_sets.json, sent messages in sent_messages.json.

    Both files are in the app's public config folder, so they can also be
    viewed or edited with a file editor and are included in backups.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.sets_path = folder / "message_sets.json"
        self.history_path = folder / "sent_messages.json"
        self.sets: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def load(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        raw_sets = _read_json(self.sets_path, [])
        if isinstance(raw_sets, dict):
            raw_sets = list(raw_sets.values())
        for raw in raw_sets if isinstance(raw_sets, list) else []:
            try:
                item = normalize_set(raw)
            except MessageError as err:
                _LOGGER.warning("Skipping invalid message set %r: %s", raw, err)
                continue
            self.sets[item["id"]] = item
        history = _read_json(self.history_path, [])
        self.history = history if isinstance(history, list) else []

    async def _save_sets(self) -> None:
        data = sorted(self.sets.values(), key=lambda s: s["name"].lower())
        await asyncio.to_thread(_write_json, self.sets_path, data)

    async def _save_history(self) -> None:
        await asyncio.to_thread(_write_json, self.history_path, self.history)

    def list_sets(self) -> list[dict[str, Any]]:
        return sorted(self.sets.values(), key=lambda s: s["name"].lower())

    def get_set(self, set_id: str) -> dict[str, Any]:
        try:
            return self.sets[set_id]
        except KeyError as err:
            raise MessageError(f"Unknown message set: {set_id}") from err

    async def save_set(self, raw: dict[str, Any], previous_id: str | None = None) -> dict[str, Any]:
        item = normalize_set(raw)
        async with self._lock:
            if item["id"] != previous_id and item["id"] in self.sets:
                raise MessageError(f"A message set with ID '{item['id']}' already exists")
            if previous_id and previous_id != item["id"]:
                self.sets.pop(previous_id, None)
            self.sets[item["id"]] = item
            await self._save_sets()
        return item

    async def delete_set(self, set_id: str) -> None:
        async with self._lock:
            if self.sets.pop(set_id, None) is None:
                raise MessageError(f"Unknown message set: {set_id}")
            await self._save_sets()

    def find_record(self, record_id: str) -> dict[str, Any]:
        for record in self.history:
            if record["id"] == record_id:
                return record
        raise MessageError(f"Unknown sent message: {record_id}")

    def find_by_message(self, message_id: str) -> dict[str, Any] | None:
        for record in self.history:
            if record.get("message_id") == message_id:
                return record
        return None

    async def add_record(self, **fields: Any) -> dict[str, Any]:
        record = {"id": uuid.uuid4().hex[:12], "sent_at": now_iso(), **fields}
        async with self._lock:
            self.history.insert(0, record)
            del self.history[MAX_HISTORY:]
            await self._save_history()
        return record

    async def update_record(self, record: dict[str, Any], **fields: Any) -> None:
        async with self._lock:
            record.update(fields, edited_at=now_iso())
            await self._save_history()

    async def delete_record(self, record_id: str) -> None:
        async with self._lock:
            record = self.find_record(record_id)
            self.history.remove(record)
            await self._save_history()
