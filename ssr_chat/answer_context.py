from __future__ import annotations

from .models import (
    AnswerPrompt,
    ChatMessage,
    ConversationSnapshot,
    QueryResult,
    QuestionAnalysis,
)


class AnswerContextBuilder:
    def __init__(self) -> None:
        self.snapshots_seen: list[ConversationSnapshot] = []

    def build(
        self,
        message: ChatMessage,
        snapshot: ConversationSnapshot,
        analysis: QuestionAnalysis,
        results: list[QueryResult],
    ) -> AnswerPrompt:
        if analysis.snapshot_id != snapshot.snapshot_id:
            raise ValueError("analysis and prompt must use the same context snapshot")
        self.snapshots_seen.append(snapshot)
        return AnswerPrompt(
            user_text=analysis.query_text,
            context_snapshot_id=snapshot.snapshot_id,
            context_messages=snapshot.messages,
            analysis=analysis.as_dict(),
            query_results=tuple(result.public_dict() for result in results),
        )
