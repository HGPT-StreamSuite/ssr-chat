"""Native task-14 plan probes select a route without executing any domain."""
from __future__ import annotations

import re

from .modular_retrieval import DOMAINS, PipelineError

SAFE_SKIPS = {"no_routing_signal", "stable_identity_available"}
PERSONAL_SHAPES = {
    "chat_history": re.compile(r"我(?:剛剛|剛才)在(?:聊天|這個|本)頻道(?:打了什麼訊息|說了哪則訊息)[？?。]?"),
    "stt_transcript": re.compile(r"(?:查詢|找出|請查詢|請找出)?我的逐字稿[？?。]?"),
}


def _single_question(text):
    body = re.sub(r"[?？。.!！]$", "", text.strip())
    return not re.search(r"[,，;；。\r\n!?！？]|以及|還有|还有|同時|同时|順便|顺便|並且|并且|另外|也|\b(?:and|also|then)\b", body, re.I)


def _validate(plan, request_id, text):
    if (not isinstance(plan, dict) or set(plan) != {"schema_version", "request_id", "tasks", "skipped_domains", "missing_inputs"}
            or plan["schema_version"] != "1.0.1" or plan["request_id"] != request_id
            or not isinstance(plan["tasks"], list) or not isinstance(plan["skipped_domains"], list)
            or not isinstance(plan["missing_inputs"], list) or any(not isinstance(item, str) for item in plan["missing_inputs"])):
        raise PipelineError("classification_plan_invalid", "planner")
    seen, ids = [], []
    for task in plan["tasks"]:
        if (not isinstance(task, dict) or set(task) != {"task_id", "domain", "query", "depends_on"}
                or not isinstance(task["domain"], str) or task["domain"] not in DOMAINS
                or not isinstance(task["task_id"], str) or not task["task_id"]
                or not isinstance(task["depends_on"], list) or any(not isinstance(item, str) for item in task["depends_on"])):
            raise PipelineError("classification_plan_invalid", "planner")
        query = task["query"]
        if (not isinstance(query, dict) or query.get("schema_version") != "1.0.1" or query.get("request_id") != request_id
                or query.get("text") != text or not isinstance(query.get("scope"), dict) or query["scope"].get("domain") != task["domain"]):
            raise PipelineError("classification_plan_invalid", "planner")
        seen.append(task["domain"]); ids.append(task["task_id"])
    for row in plan["skipped_domains"]:
        if (not isinstance(row, dict) or set(row) != {"domain", "reason"} or not isinstance(row["domain"], str)
                or row["domain"] not in DOMAINS or not isinstance(row["reason"], str) or not row["reason"]):
            raise PipelineError("classification_plan_invalid", "planner")
        seen.append(row["domain"])
    if len(seen) != len(DOMAINS) or set(seen) != DOMAINS or len(ids) != len(set(ids)):
        raise PipelineError("classification_plan_invalid", "planner")


class NaturalQuestionRouter:
    def __init__(self, targets):
        self.providers = {}
        for command, (_, domain, operation, gateway) in targets.items():
            provider = gateway if operation == "retrieve" else gateway.retrieval
            if not callable(getattr(provider, "classification_plan", None)):
                raise PipelineError("entry_classification_provider_invalid")
            self.providers[command] = (domain, provider)

    def classify(self, payload):
        decision = {"status": "held", "method": "task14_plan_only_per_route_v1", "probes": {}, "signals": [],
                    "domain_execution": "not_run", "model_invoked": False}
        if not _single_question(payload["text"]):
            return {**decision, "reason_code": "entry_compound_question"}
        try:
            for command, (domain, provider) in self.providers.items():
                probe = provider.classification_plan(payload)
                plan, stages = probe["plan"], probe["stages"]
                _validate(plan, payload["request_id"], payload["text"])
                if (not isinstance(stages, list) or len(stages) != 1 or stages[0].get("stage") != "planner"
                        or type(stages[0].get("exit_code")) is not int or stages[0]["exit_code"] not in {0, 1, 2, 3}):
                    raise PipelineError("classification_execution_invalid", "planner")
                own_signal = any(row["domain"] == domain for row in plan["tasks"]) or any(
                    row["domain"] == domain and row["reason"] != "no_routing_signal" for row in plan["skipped_domains"])
                eligible = (stages[0]["exit_code"] == 0 and not plan["missing_inputs"] and len(plan["tasks"]) == 1
                            and plan["tasks"][0]["domain"] == domain and not plan["tasks"][0]["depends_on"]
                            and all(row["reason"] in SAFE_SKIPS for row in plan["skipped_domains"]))
                decision["probes"][command] = {**probe, "domain": domain, "own_signal": own_signal, "eligible": eligible}
                if own_signal: decision["signals"].append(command)
        except PipelineError as error:
            return {**decision, "reason_code": "entry_classifier_failed", "error": {"stage": error.stage, "code": error.code}}
        except Exception:
            return {**decision, "reason_code": "entry_classifier_failed"}
        if not decision["signals"]:
            return {**decision, "reason_code": "entry_classification_unknown"}
        if len(decision["signals"]) != 1:
            return {**decision, "reason_code": "entry_classification_ambiguous"}
        command = decision["signals"][0]
        probe = decision["probes"][command]
        if not probe["eligible"]:
            return {**decision, "reason_code": "entry_classification_incomplete"}
        shape = PERSONAL_SHAPES.get(probe["domain"])
        if shape is not None and shape.fullmatch(payload["text"].strip()) is None:
            return {**decision, "reason_code": "entry_personal_question_unresolved"}
        return {**decision, "status": "selected", "command": command, "reason_code": "entry_natural_selected"}

    @staticmethod
    def summary(decision):
        return {key: decision[key] for key in ("status", "method", "reason_code", "domain_execution", "model_invoked")} | {
            "candidate_domains": [decision["probes"][command]["domain"] for command in decision["signals"]]}
