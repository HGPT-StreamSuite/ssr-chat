from __future__ import annotations

from .models import ConversationSnapshot, QuestionAnalysis


class TriggerDetector:
    def __init__(self, prefixes: tuple[str, ...] = ("!ask",)) -> None:
        self.prefixes = prefixes

    def is_triggered(self, text: str) -> bool:
        stripped = text.lstrip()
        return any(stripped.startswith(prefix) for prefix in self.prefixes)

    def remove_prefix(self, text: str) -> str:
        stripped = text.lstrip()
        for prefix in self.prefixes:
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].lstrip(" :：,，")
        return stripped


class QuestionAnalyzer:
    KEYWORDS = {
        "weather": ("天氣", "下雨", "幾度", "溫度"),
        "person": ("人物", "角色", "誰是", "介紹一下露娜"),
        "game_guide": ("攻略", "怎麼打", "如何打", "打法", "通關", "boss", "Boss"),
        "game_item": ("道具", "物品", "藥水", "效果", "怎麼用"),
        "game_info": ("遊戲", "特色", "介紹"),
    }
    ORDER = ("weather", "person", "game_guide", "game_item", "game_info")

    def __init__(self, trigger_detector: TriggerDetector | None = None) -> None:
        self.trigger_detector = trigger_detector or TriggerDetector()
        self.snapshots_seen: list[ConversationSnapshot] = []

    def analyze(
        self, text: str, snapshot: ConversationSnapshot
    ) -> QuestionAnalysis:
        self.snapshots_seen.append(snapshot)
        query_text = self.trigger_detector.remove_prefix(text)
        lower = query_text.lower()
        intents = [
            purpose
            for purpose in self.ORDER
            if any(keyword.lower() in lower for keyword in self.KEYWORDS[purpose])
        ]
        if not intents:
            # The local default is a game-info lookup, never an ungrounded model answer.
            intents = ["game_info"]
        return QuestionAnalysis(
            intents=tuple(intents),
            query_text=query_text,
            snapshot_id=snapshot.snapshot_id,
        )
