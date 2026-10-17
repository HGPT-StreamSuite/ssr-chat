from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .adapters import ModelAdapter, TextOutputAdapter
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
        model: ModelAdapter,
        output: TextOutputAdapter,
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
        duplicate = self.conversation_store.append_event(
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

        if duplicate and normalized is not None:
            held = self.sop.hold("duplicate_message_id", raw_event, message_id)
            return {
                "status": "held",
                "reason": "duplicate_message_id",
                "message_id": message_id,
                "public_reply": False,
                "sop_id": held["sop_id"],
            }

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
        diagnostics = [
            {
                "stage": "query",
                "purpose": result.purpose,
                "detail": result.diagnostic,
            }
            for result in results
            if result.diagnostic
        ]

        try:
            answer = self.model.generate(prompt)
        except Exception as exc:
            diagnostics.append(
                {"stage": "model", "detail": f"{type(exc).__name__}: model adapter failed"}
            )
            send_result = {
                "status": "not_attempted",
                "attempted": False,
                "public_reply": False,
                "delivery_state": "confirmed_not_sent",
                "message_id": message.message_id,
                "run_id": run_id,
            }
            self.sop.hold("model_error", raw_event, message.message_id)
            self._append_trace(
                run_id, message, snapshot, analysis, results, diagnostics,
                run_status="model_error", answer=None, send_result=send_result,
            )
            return {
                "status": "failed",
                "reason": "model_error",
                "message_id": message.message_id,
                "run_id": run_id,
                "context_snapshot_id": snapshot.snapshot_id,
                "analysis": analysis.as_dict(),
                "queries": [result.public_dict() for result in results],
                "answer": None,
                "send_result": send_result,
                "public_reply": False,
            }

        try:
            send_result = self.output.send(message.as_dict(), answer, run_id)
            if not isinstance(send_result, dict):
                raise ValueError("output adapter returned a non-object result")
            if (
                not isinstance(send_result.get("status"), str)
                or not isinstance(send_result.get("attempted"), bool)
                or not isinstance(send_result.get("public_reply"), bool)
            ):
                raise ValueError("output adapter result is missing status flags")
            if send_result["status"] not in {"sent", "dry_run_not_sent"}:
                raise RuntimeError("output adapter reported unsuccessful delivery")
            if send_result["status"] == "sent" and (
                not send_result["attempted"] or not send_result["public_reply"]
            ):
                raise ValueError("sent output must confirm an attempted public reply")
            if send_result["status"] == "dry_run_not_sent" and send_result["public_reply"]:
                raise ValueError("dry-run output cannot report a public reply")
            send_result = {
                **send_result,
                "message_id": message.message_id,
                "run_id": run_id,
                "delivery_state": (
                    "confirmed_sent" if send_result["status"] == "sent" else "confirmed_not_sent"
                ),
            }
        except Exception as exc:
            diagnostics.append(
                {"stage": "output", "detail": f"{type(exc).__name__}: output adapter failed"}
            )
            failed_result = send_result if "send_result" in locals() and isinstance(send_result, dict) else {}
            failed_result = {
                **failed_result,
                "status": "output_error",
                "attempted": bool(failed_result.get("attempted", True)),
                "public_reply": bool(failed_result.get("public_reply", False)),
                "delivery_state": "unknown",
                "message_id": message.message_id,
                "run_id": run_id,
            }
            self.sop.hold("output_error", raw_event, message.message_id)
            self._append_trace(
                run_id, message, snapshot, analysis, results, diagnostics,
                run_status="output_error", answer=answer, send_result=failed_result,
            )
            return {
                "status": "delivery_failed",
                "reason": "output_error",
                "message_id": message.message_id,
                "run_id": run_id,
                "context_snapshot_id": snapshot.snapshot_id,
                "analysis": analysis.as_dict(),
                "queries": [result.public_dict() for result in results],
                "answer": answer,
                "send_result": failed_result,
                "public_reply": failed_result["public_reply"],
            }

        self._append_trace(
            run_id, message, snapshot, analysis, results, diagnostics,
            run_status="completed", answer=answer, send_result=send_result,
        )
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

    def _append_trace(
        self,
        run_id: str,
        message: ChatMessage,
        snapshot: Any,
        analysis: Any,
        results: list[QueryResult],
        diagnostics: list[dict[str, Any]],
        *,
        run_status: str,
        answer: str | None,
        send_result: dict[str, Any],
    ) -> None:
        self.run_traces.append({
            "run_id": run_id,
            "message_id": message.message_id,
            "context_snapshot_id": snapshot.snapshot_id,
            "run_status": run_status,
            "analysis": analysis.as_dict(),
            "queries": [result.public_dict() for result in results],
            "diagnostics": diagnostics,
            "answer": answer,
            "send_result": send_result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })
