from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import ValidationResult


class EventValidator:
    """Small local validation boundary; the source allowlist is fixture-only."""

    def __init__(self, allowed_sources: set[str] | None = None) -> None:
        self.allowed_sources = allowed_sources or {"local-fixture"}

    def validate(self, event: Any) -> ValidationResult:
        if not isinstance(event, dict):
            return ValidationResult(False, "event_must_be_object", None)

        for field in ("conversation_id", "author_id", "text", "source"):
            value = event.get(field)
            if not isinstance(value, str) or not value.strip():
                return ValidationResult(False, f"missing_or_invalid_{field}", None)

        source = event["source"].strip()
        if source not in self.allowed_sources:
            return ValidationResult(False, "source_not_allowed", None)

        is_self = event.get("is_self", False)
        if not isinstance(is_self, bool):
            return ValidationResult(False, "invalid_is_self_flag", None)

        message_id = event.get("message_id")
        if message_id is None:
            return ValidationResult(False, "missing_message_id", None)
        if not isinstance(message_id, str) or not message_id.strip():
            return ValidationResult(False, "invalid_message_id", None)

        created_at = event.get("created_at")
        if created_at is None:
            created_at = datetime.now(timezone.utc).isoformat()
        if not isinstance(created_at, str) or not created_at.strip():
            return ValidationResult(False, "invalid_created_at", None)

        normalized = {
            "message_id": message_id.strip(),
            "conversation_id": event["conversation_id"].strip(),
            "author_id": event["author_id"].strip(),
            "text": event["text"].strip(),
            "source": source,
            "created_at": created_at,
            "is_self": is_self,
        }
        return ValidationResult(True, None, normalized)
