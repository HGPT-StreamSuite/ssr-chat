from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None
    normalized: dict[str, Any] | None


@dataclass(frozen=True)
class ChatMessage:
    message_id: str
    conversation_id: str
    author_id: str
    text: str
    source: str
    created_at: str
    is_self: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChatMessage":
        return cls(
            message_id=value["message_id"],
            conversation_id=value["conversation_id"],
            author_id=value["author_id"],
            text=value["text"],
            source=value["source"],
            created_at=value["created_at"],
            is_self=value["is_self"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "author_id": self.author_id,
            "text": self.text,
            "source": self.source,
            "created_at": self.created_at,
            "is_self": self.is_self,
        }


@dataclass(frozen=True)
class ConversationSnapshot:
    snapshot_id: str
    conversation_id: str
    messages: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class QuestionAnalysis:
    intents: tuple[str, ...]
    query_text: str
    snapshot_id: str
    queries: tuple["QueryTarget", ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "intents": list(self.intents),
            "query_text": self.query_text,
            "snapshot_id": self.snapshot_id,
            "queries": [query.as_dict() for query in self.queries],
        }


@dataclass(frozen=True)
class QueryTarget:
    purpose: str
    query_text: str

    def as_dict(self) -> dict[str, str]:
        return {"purpose": self.purpose, "query_text": self.query_text}


@dataclass(frozen=True)
class QueryResult:
    purpose: str
    status: str
    materials: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    sources: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    diagnostic: str | None = None

    def public_dict(self) -> dict[str, Any]:
        # Diagnostic details belong in the run trace only, never in a Prompt.
        return {
            "status": self.status,
            "materials": [dict(item) for item in self.materials],
            "sources": [dict(item) for item in self.sources],
        }


@dataclass(frozen=True)
class AnswerPrompt:
    user_text: str
    context_snapshot_id: str
    context_messages: tuple[dict[str, str], ...]
    analysis: dict[str, Any]
    query_results: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_text": self.user_text,
            "context_snapshot_id": self.context_snapshot_id,
            "context_messages": [dict(item) for item in self.context_messages],
            "analysis": dict(self.analysis),
            "query_results": [
                {
                    **result,
                    "materials": [dict(item) for item in result.get("materials", [])],
                    "sources": [dict(item) for item in result.get("sources", [])],
                }
                for result in self.query_results
            ],
        }
