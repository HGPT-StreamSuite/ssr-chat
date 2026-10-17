from __future__ import annotations

from typing import Any, Protocol

from .models import AnswerPrompt


class ModelAdapter(Protocol):
    """Repository-local contract for a model implementation."""

    def generate(self, prompt: AnswerPrompt) -> str: ...


class TextOutputAdapter(Protocol):
    """Repository-local contract for text delivery or dry-run output."""

    def send(self, message: dict[str, Any], answer: str, run_id: str) -> dict[str, Any]: ...


class WeatherAdapter(Protocol):
    """Repository-local contract for a weather source adapter."""

    def fetch(
        self, question: str, context_messages: tuple[dict[str, str], ...]
    ) -> dict[str, Any] | None: ...


class KnowledgeReaderAdapter(Protocol):
    """Repository-local contract for purpose-specific published knowledge reads."""

    def query(
        self,
        purpose: str,
        question: str,
        context_messages: tuple[dict[str, str], ...],
    ) -> Any: ...
