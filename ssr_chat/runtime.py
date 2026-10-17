from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .analysis import QuestionAnalyzer, TriggerDetector
from .answer_context import AnswerContextBuilder
from .context import ConversationContext
from .input_validation import EventValidator
from .models import ChatMessage, QueryResult
from .output import DryRunOutput
from .sop import FixedSop
from .stores import (
    ConversationEventStore,
    KnowledgeGapStore,
    RunTraceStore,
)


class ChatRuntime:
    KNOWLEDGE_PURPOSES = {"person", "game_info", "game_guide", "game_item"}

    def __init__(
        self,
        validator: EventValidator,
        trigger_detector: TriggerDetector,
        conversation_store: ConversationEventStore,
        context: ConversationContext,
        analyzer: QuestionAnalyzer,
        queries: dict[str, Any],
        answer_context: AnswerContextBuilder,
        model: Any,
        output: Any,
        run_traces: RunTraceStore,
        sop: FixedSop,
        gaps: KnowledgeGapStore,
        bot_id: str = "ssr-bot-fixture",
    ) -> None:
        self.validator = validator
        self.trigger_detector = trigger_detector
        self.conversation_store = conversation_store
        self.context = context
        self.analyzer = analyzer
        self.queries = queries
        self.answer_context = answer_context
        self.model = model
        self.output = output
        self.run_traces = run_traces
        self.sop = sop
        self.gaps = gaps
        self.bot_id = bot_id

    def handle(self, raw_event: Any) -> dict[str, Any]:
        validation = self.validator.validate(raw_event)
        normalized = validation.normalized
        message_id = normalized.get("message_id") if normalized else None
        self.conversation_store.append_event(
            {
                "received_at": datetime.now(timezone.utc).isoformat(),
                "raw_event": raw_event,
                "validation": {
                    "valid": validation.valid,
                    "reason": validation.reason,
                },
                "normalized_event": normalized,
            }
        )

        if not validation.valid or normalized is None:
            held = self.sop.hold(validation.reason or "invalid_event", raw_event)
            return {
                "status": "held",
                "reason": validation.reason or "invalid_event",
                "message_id": None,
                "public_reply": False,
                "sop_id": held["sop_id"],
            }

        message = ChatMessage.from_dict(normalized)
        if message.is_self or message.author_id == self.bot_id:
            held = self.sop.hold("self_event_loop_guard", raw_event, message.message_id)
            return {
                "status": "held",
                "reason": "self_event_loop_guard",
                "message_id": message.message_id,
                "public_reply": False,
                "sop_id": held["sop_id"],
            }

        if not self.trigger_detector.is_triggered(message.text):
            held = self.sop.hold("not_triggered", raw_event, message.message_id)
            return {
                "status": "held",
                "reason": "not_triggered",
                "message_id": message.message_id,
                "public_reply": False,
                "sop_id": held["sop_id"],
            }

        run_id = f"run-{uuid4().hex}"
        snapshot = self.context.snapshot_for(message)
        analysis = self.analyzer.analyze(message.text, snapshot)
        results: list[QueryResult] = []
        for purpose in analysis.intents:
            query = self.queries.get(purpose)
            if query is None:
                result = QueryResult(
                    purpose=purpose,
                    status="tool_error",
                    diagnostic="query_module_unavailable",
                )
            else:
                try:
                    result = query.execute(analysis.query_text, snapshot)
                except Exception as exc:
                    result = QueryResult(
                        purpose=purpose,
                        status="tool_error",
                        diagnostic=f"{type(exc).__name__}: {exc}",
                    )
            results.append(result)
            if (
                purpose in self.KNOWLEDGE_PURPOSES
                and result.status == "no_data"
            ):
                self.gaps.append(
                    {
                        "gap_id": f"gap-{uuid4().hex}",
                        "run_id": run_id,
                        "message_id": message.message_id,
                        "purpose": purpose,
                        "question": analysis.query_text,
                        "reason": "knowledge_gap",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

        prompt = self.answer_context.build(message, snapshot, analysis, results)
        answer = self.model.generate(prompt)
        send_result = self.output.send(message.as_dict(), answer, run_id)
        diagnostics = [
            {
                "purpose": result.purpose,
                "detail": result.diagnostic,
            }
            for result in results
            if result.diagnostic
        ]
        trace = {
            "run_id": run_id,
            "message_id": message.message_id,
            "context_snapshot_id": snapshot.snapshot_id,
            "analysis": analysis.as_dict(),
            "queries": [result.public_dict() for result in results],
            "diagnostics": diagnostics,
            "answer": answer,
            "send_result": send_result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.run_traces.append(trace)
        return {
            "status": "answered",
            "message_id": message.message_id,
            "run_id": run_id,
            "context_snapshot_id": snapshot.snapshot_id,
            "analysis": analysis.as_dict(),
            "queries": [result.public_dict() for result in results],
            "answer": answer,
            "send_result": send_result,
        }
