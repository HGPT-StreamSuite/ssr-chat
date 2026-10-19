from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .stores import SopEventStore


class FixedSop:
    """Conservative fixed SOP: record and hold; never publish a reply."""

    def __init__(self, store: SopEventStore) -> None:
        self.store = store

    def hold(
        self, reason: str, raw_event: Any, message_id: str | None = None
    ) -> dict[str, Any]:
        record = {
            "sop_id": f"sop-{uuid4().hex}",
            "message_id": message_id,
            "reason": reason,
            "action": "record_and_hold",
            "public_reply": False,
            "raw_event": raw_event,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.append(record)
        return record
