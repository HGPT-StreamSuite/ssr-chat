from __future__ import annotations

import re

from .models import ConversationSnapshot, QueryTarget, QuestionAnalysis


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
    QUERY_SPLIT_PATTERN = re.compile(
        r"[，,；;。！？?!]+|然後|接著|另外|順便|以及|同時|還有|並且|和"
    )
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
        clauses = [part.strip() for part in self.QUERY_SPLIT_PATTERN.split(query_text) if part.strip()]
        matches_by_clause = [(clause, self._intents_for(clause)) for clause in clauses]
        intents = [
            purpose
            for purpose in self.ORDER
            if any(purpose in matched for _, matched in matches_by_clause)
        ]
        if not intents:
            # The local default is a game-info lookup, never an ungrounded model answer.
            intents = ["game_info"]
            matches_by_clause = [(query_text, ("game_info",))]
        queries = tuple(
            QueryTarget(
                purpose=purpose,
                query_text="；".join(
                    clause for clause, matched in matches_by_clause if purpose in matched
                ),
            )
            for purpose in intents
        )
        return QuestionAnalysis(
            intents=tuple(intents),
            query_text=query_text,
            snapshot_id=snapshot.snapshot_id,
            queries=queries,
        )

    @classmethod
    def _intents_for(cls, text: str) -> tuple[str, ...]:
        lower = text.lower()
        return tuple(
            purpose
            for purpose in cls.ORDER
            if any(keyword.lower() in lower for keyword in cls.KEYWORDS[purpose])
        )
