from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import QueryResult


class LocalKnowledgeReader:
    """Reads only this repo's published fixture file; no service or other repo."""

    BUCKETS = {
        "person": "people",
        "game_info": "game_info",
        "game_guide": "game_guide",
        "game_item": "game_item",
    }

    def __init__(self, fixture_path: str | Path) -> None:
        self.fixture_path = Path(fixture_path)

    def query(
        self,
        purpose: str,
        question: str,
        context_messages: tuple[dict[str, str], ...],
    ) -> QueryResult:
        bucket_name = self.BUCKETS[purpose]
        with self.fixture_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        bucket = data.get(bucket_name, {})
        context = " ".join([question, *(item["text"] for item in context_messages)])
        matched = [
            entry
            for label, entries in bucket.items()
            if label.lower() in context.lower()
            for entry in entries
        ]
        if not matched:
            return QueryResult(purpose=purpose, status="no_data")

        source_map = data.get("sources", {})
        source_ids = list(dict.fromkeys(item.get("source_id", "") for item in matched))
        sources = tuple(
            dict(source_map[source_id])
            for source_id in source_ids
            if source_id and source_id in source_map
        )
        materials = tuple(dict(item) for item in matched)
        return QueryResult(
            purpose=purpose,
            status="ok",
            materials=materials,
            sources=sources,
        )
