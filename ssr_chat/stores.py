from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class JsonlLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        records.append(value)
        return records


class ConversationEventStore:
    def __init__(self, path: str | Path) -> None:
        self.log = JsonlLog(path)
        self._lock = threading.RLock()
        self._seen_message_keys: set[tuple[str, str, str]] = set()
        for record in self.log.read_all():
            validation = record.get("validation", {})
            message = record.get("normalized_event")
            if validation.get("valid") and isinstance(message, dict):
                source = message.get("source")
                conversation_id = message.get("conversation_id")
                message_id = message.get("message_id")
                if all(isinstance(value, str) for value in (source, conversation_id, message_id)):
                    self._seen_message_keys.add((source, conversation_id, message_id))

    def append_event(self, record: dict[str, Any]) -> bool:
        """Persist every arrival and return whether it repeats a valid source/message key."""
        validation = record.get("validation", {})
        message = record.get("normalized_event")
        key = None
        if validation.get("valid") and isinstance(message, dict):
            source = message.get("source")
            conversation_id = message.get("conversation_id")
            message_id = message.get("message_id")
            if all(isinstance(value, str) for value in (source, conversation_id, message_id)):
                key = (source, conversation_id, message_id)
        with self._lock:
            duplicate = key is not None and key in self._seen_message_keys
            self.log.append(record)
            if key is not None:
                self._seen_message_keys.add(key)
            return duplicate

    def prior_messages(
        self, conversation_id: str, excluding_message_id: str, limit: int
    ) -> tuple[dict[str, str], ...]:
        messages: list[dict[str, str]] = []
        for record in self.log.read_all():
            validation = record.get("validation", {})
            message = record.get("normalized_event")
            if not validation.get("valid") or not isinstance(message, dict):
                continue
            if message.get("conversation_id") != conversation_id:
                continue
            if message.get("message_id") == excluding_message_id or message.get("is_self"):
                continue
            messages.append(
                {
                    "message_id": str(message.get("message_id", "")),
                    "author_id": str(message.get("author_id", "")),
                    "text": str(message.get("text", "")),
                    "created_at": str(message.get("created_at", "")),
                }
            )
        return tuple(messages[-limit:])


class RunTraceStore:
    def __init__(self, path: str | Path) -> None:
        self.log = JsonlLog(path)

    def append(self, record: dict[str, Any]) -> None:
        self.log.append(record)

    def read_all(self) -> list[dict[str, Any]]:
        return self.log.read_all()


class SopEventStore:
    def __init__(self, path: str | Path) -> None:
        self.log = JsonlLog(path)

    def append(self, record: dict[str, Any]) -> None:
        self.log.append(record)

    def read_all(self) -> list[dict[str, Any]]:
        return self.log.read_all()


class KnowledgeGapStore:
    def __init__(self, path: str | Path) -> None:
        self.log = JsonlLog(path)

    def append(self, record: dict[str, Any]) -> None:
        self.log.append(record)

    def read_all(self) -> list[dict[str, Any]]:
        return self.log.read_all()
