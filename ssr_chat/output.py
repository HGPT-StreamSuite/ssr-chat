from __future__ import annotations

from typing import Any


class DryRunOutput:
    """Records intent but never sends to a public platform."""

    def send(self, message: dict[str, Any], answer: str, run_id: str) -> dict[str, Any]:
        return {
            "status": "dry_run_not_sent",
            "attempted": False,
            "public_reply": False,
            "message_id": message["message_id"],
            "run_id": run_id,
        }
