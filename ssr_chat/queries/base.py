from __future__ import annotations

from ..local_knowledge import LocalKnowledgeReader
from ..models import ConversationSnapshot, QueryResult


class KnowledgeQuery:
    purpose = ""

    def __init__(self, reader: LocalKnowledgeReader) -> None:
        self.reader = reader

    def execute(
        self, question: str, snapshot: ConversationSnapshot
    ) -> QueryResult:
        return self.reader.query(self.purpose, question, snapshot.messages)
