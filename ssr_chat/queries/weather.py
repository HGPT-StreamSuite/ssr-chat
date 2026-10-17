from __future__ import annotations

from ..models import ConversationSnapshot, QueryResult
from ..weather_adapter import FixtureWeatherAdapter


class WeatherQuery:
    purpose = "weather"

    def __init__(self, adapter: FixtureWeatherAdapter) -> None:
        self.adapter = adapter

    def execute(
        self, question: str, snapshot: ConversationSnapshot
    ) -> QueryResult:
        try:
            forecast = self.adapter.fetch(question, snapshot.messages)
        except Exception as exc:
            return QueryResult(
                purpose=self.purpose,
                status="tool_error",
                diagnostic=f"{type(exc).__name__}: {exc}",
            )
        if forecast is None:
            return QueryResult(purpose=self.purpose, status="no_data")
        source = {
            "source_id": forecast.get("source_id", "fixture-weather"),
            "name": "虛構天氣 fixture",
            "locator": "fixture://weather",
        }
        material = {
            "title": f"{forecast.get('location', '未知地點')}天氣（測試資料）",
            "text": (
                f"{forecast.get('summary', '')}，"
                f"{forecast.get('temperature_c', '')}°C；"
                f"{forecast.get('note', '僅供本機測試')}"
            ),
            "source_id": source["source_id"],
        }
        return QueryResult(
            purpose=self.purpose,
            status="ok",
            materials=(material,),
            sources=(source,),
        )
