from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FixtureWeatherAdapter:
    """A deterministic local adapter; it never makes a network request."""

    def __init__(
        self, fixture_path: str | Path, config_path: str | Path | None = None
    ) -> None:
        self.fixture_path = Path(fixture_path)
        self.config_path = Path(config_path) if config_path is not None else None

    def fetch(
        self, question: str, context_messages: tuple[dict[str, str], ...]
    ) -> dict[str, Any] | None:
        config: dict[str, Any] = {}
        if self.config_path is not None:
            with self.config_path.open("r", encoding="utf-8") as handle:
                config = json.load(handle)
            if not config.get("enabled", False):
                raise RuntimeError("weather fixture adapter is disabled")
            if not config.get("interface_version"):
                raise ValueError("weather adapter interface_version is required")

        with self.fixture_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        combined = " ".join([question, *(item["text"] for item in context_messages)])
        for row in data.get("forecasts", []):
            if row.get("location", "").lower() in combined.lower():
                result = dict(row)
                result["source_id"] = config.get(
                    "source_id", result.get("source_id", "fixture-weather")
                )
                return result
        return None
