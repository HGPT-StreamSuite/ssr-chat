"""Opt-in synthetic evidence -> purpose gate -> local model; no output adapter."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from .adapters import ModelAdapterError
from .models import AnswerPrompt
from .modular_retrieval import PipelineError, _local_path, load_object
from .ollama_model import OllamaModelClient

SCOPE_FIELDS = ("domain", "platform", "channel_id", "game_id", "session_id", "content_version")
CONTENT_FIELDS = ("record_id", "source_id", "source_event_id", "revision", "event_time", "scope", "layer",
                  "text", "quality_flags", "source_refs", "source_kind")
ANSWER_DOMAINS = {"channel_knowledge", "chat_history", "stt_transcript"}
ACTOR_DOMAINS = ("chat_history", "stt_transcript")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def content_fields(evidence):
    domain = evidence["scope"]["domain"]
    return (CONTENT_FIELDS + (("actor_account_id",) if domain in ACTOR_DOMAINS else ())
            + (("extensions",) if domain == "stt_transcript" else ()))


def content_digest(evidence):
    return hashlib.sha256(encoded({key: evidence[key] for key in content_fields(evidence)})).hexdigest()


def _identity(value):
    fields = ("scope", "source_id", "record_id", "revision", "layer", "source_kind")
    if value["scope"]["domain"] in ACTOR_DOMAINS:
        fields += ("actor_account_id",)
    return encoded({key: value[key] for key in fields})


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp_zone_required")
    return parsed


class PurposePolicy:
    """Exact synthetic Record/revision/content receipts, independent of read ACL."""
    def __init__(self, value):
        if (not isinstance(value, dict) or set(value) != {"schema_version", "policy_id", "grants"}
                or value["schema_version"] != "1.0.1" or not isinstance(value["policy_id"], str)
                or not value["policy_id"].strip() or not isinstance(value["grants"], list) or len(value["grants"]) > 200):
            raise PipelineError("answer_policy_invalid")
        self.policy_id, self.sha256 = value["policy_id"], hashlib.sha256(encoded(value)).hexdigest()
        self._grants = {}
        for grant in value["grants"]:
            fields = {"scope", "source_id", "record_id", "revision", "layer", "source_kind", "content_sha256", "ai_input"}
            if isinstance(grant, dict) and isinstance(grant.get("scope"), dict) and grant["scope"].get("domain") in ACTOR_DOMAINS:
                fields.add("actor_account_id")
            if not isinstance(grant, dict) or set(grant) != fields:
                raise PipelineError("answer_policy_invalid")
            scope, use = grant["scope"], grant["ai_input"]
            if (not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS)
                    or not isinstance(scope["domain"], str) or scope["domain"] not in ANSWER_DOMAINS
                    or any(item is not None and (not isinstance(item, str) or not item.strip()) for item in scope.values())
                    or scope["platform"] is None or scope["channel_id"] is None
                    or (scope["domain"] in ACTOR_DOMAINS and (scope["session_id"] is None
                        or not isinstance(grant["actor_account_id"], str) or not grant["actor_account_id"].strip()))
                    or grant["source_kind"] != "synthetic" or grant["layer"] != "raw"
                    or any(not isinstance(grant[key], str) or not grant[key].strip() for key in ("source_id", "record_id"))
                    or type(grant["revision"]) is not int or grant["revision"] < 1
                    or not isinstance(grant["content_sha256"], str) or re.fullmatch("[0-9a-f]{64}", grant["content_sha256"]) is None
                    or not isinstance(use, dict) or set(use) != {"status", "basis"}
                    or not isinstance(use["status"], str) or use["status"] not in {"allowed", "unknown", "prohibited"}
                    or not isinstance(use["basis"], str) or (use["status"] == "allowed" and not use["basis"].strip())):
                raise PipelineError("answer_policy_invalid")
            key = _identity(grant)
            if key in self._grants:
                raise PipelineError("answer_policy_duplicate_record")
            self._grants[key] = deepcopy(grant)

    @classmethod
    def read(cls, path):
        raw = Path(path).read_bytes()
        policy = cls(load_object(raw))
        policy.sha256 = hashlib.sha256(raw).hexdigest()
        return policy

    def check(self, evidence):
        try:
            if (evidence["source_kind"] != "synthetic" or evidence["layer"] != "raw"
                    or evidence["scope"]["domain"] not in ANSWER_DOMAINS or "synthetic" not in evidence["quality_flags"]):
                return "unsupported_answer_source"
            grant = self._grants.get(_identity(evidence))
            if grant is None or grant["ai_input"]["status"] != "allowed":
                return "ai_input_not_allowed"
            if content_digest(evidence) != grant["content_sha256"]:
                return "approved_content_mismatch"
            refs = evidence["source_refs"]
            if not isinstance(refs, list) or not refs:
                return "unverified_source_reference"
            # Raw self references are the verified first slice. Cross-record or
            # published references require a separate provenance resolver.
            if any(not isinstance(ref, dict) or (ref.get("source_id"), ref.get("record_id"), ref.get("revision"))
                   != (evidence["source_id"], evidence["record_id"], evidence["revision"]) for ref in refs):
                return "unverified_source_reference"
            return None
        except (KeyError, TypeError, ValueError):
            return "invalid_answer_evidence"


class EvidenceAnswerGateway:
    def __init__(self, retrieval, policy, model, trace):
        profile = retrieval.profile
        if (not isinstance(profile.get("domain"), str) or profile["domain"] not in ANSWER_DOMAINS or profile.get("layer") != "raw"
                or profile.get("data_mode") != "declared_synthetic"):
            raise PipelineError("answer_requires_synthetic_raw_supported_route")
        self.domain = profile["domain"]
        self._chat_context = None
        self._stt_context = None
        if self.domain == "chat_history":
            self._chat_context = retrieval.trusted_answer_context
            try:
                scope = self._chat_context["scope"]
                if (not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS) or scope["domain"] != self.domain
                        or any(not isinstance(scope[key], str) or not scope[key] for key in ("platform", "channel_id", "session_id"))
                        or not isinstance(self._chat_context["actor_account_id"], str) or not self._chat_context["actor_account_id"].strip()
                        or not isinstance(self._chat_context["current_event"], dict)
                        or not isinstance(self._chat_context["current_event"].get("source_event_id"), str)
                        or not self._chat_context["current_event"]["source_event_id"].strip()):
                    raise ValueError("chat_binding_required")
                _instant(self._chat_context["current_event"]["event_time"])
            except (KeyError, TypeError, ValueError) as error:
                raise PipelineError("answer_chat_binding_invalid") from error
        if self.domain == "stt_transcript":
            self._stt_context = retrieval.trusted_answer_context
            try:
                scope = self._stt_context["scope"]
                if (not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS) or scope["domain"] != self.domain
                        or any(not isinstance(scope[key], str) or not scope[key] for key in ("platform", "channel_id", "session_id"))
                        or not isinstance(self._stt_context["actor_account_id"], str) or not self._stt_context["actor_account_id"].strip()
                        or type(self._stt_context["limit"]) is not int or self._stt_context["limit"] != 1
                        or _instant(self._stt_context["time_from"]) >= _instant(self._stt_context["time_to"])):
                    raise ValueError("stt_binding_required")
            except (KeyError, TypeError, ValueError) as error:
                raise PipelineError("answer_stt_binding_invalid") from error
        self.retrieval, self.policy, self.model, self.trace = retrieval, policy, model, trace

    @property
    def profile(self):
        return {"configured": True, "domain": self.domain, "layer": "raw", "data_mode": "synthetic_only",
                "model": "ollama" if isinstance(self.model, OllamaModelClient) else "custom",
                "purpose_gate": "exact_record_revision_content", "citations_required": True, "public_output_enabled": False}

    def _chat_boundary(self, retrieval, evidence):
        try:
            context = self._chat_context
            tasks = retrieval["plan"]["tasks"]
            if len(tasks) != 1 or len(evidence) != 1:
                return "chat_recent_boundary_missing"
            query = tasks[0]["query"]
            row = evidence[0]
            if (query["scope"] != context["scope"] or row["scope"] != context["scope"]
                    or query["actor_account_id"] != context["actor_account_id"] or row["actor_account_id"] != context["actor_account_id"]):
                return "answer_actor_scope_mismatch"
            current = context["current_event"]
            before = _instant(query["conditions"]["before_event_time"])
            if (query.get("intent") != "recent_utterance" or query.get("limit") != 1 or query.get("layer") != "raw"
                    or not isinstance(query["exclude_event_ids"], list) or current["source_event_id"] not in query["exclude_event_ids"]
                    or "session_ids" in query["conditions"] or before > _instant(current["event_time"])
                    or _instant(row["event_time"]) >= before or row["source_event_id"] in query["exclude_event_ids"]
                    or not isinstance(row["text"], str) or not row["text"].strip()):
                return "chat_recent_boundary_missing"
            return None
        except (KeyError, TypeError, ValueError):
            return "chat_recent_boundary_missing"

    def _stt_boundary(self, retrieval, evidence):
        try:
            context = self._stt_context
            tasks = retrieval["plan"]["tasks"]
            if len(tasks) != 1 or len(evidence) != 1:
                return "stt_window_mismatch"
            query, row = tasks[0]["query"], evidence[0]
            if (query["scope"] != context["scope"] or row["scope"] != context["scope"]
                    or query["actor_account_id"] != context["actor_account_id"] or row["actor_account_id"] != context["actor_account_id"]):
                return "answer_actor_scope_mismatch"
            start, stop = _instant(context["time_from"]), _instant(context["time_to"])
            if (_instant(query["time_from"]) != start or _instant(query["time_to"]) != stop
                    or query.get("limit") != 1 or query.get("layer") != "raw"
                    or query.get("intent") != "stt_lookup" or query.get("conditions") != {"revision_view": "current"}
                    or not start <= _instant(row["event_time"]) < stop):
                return "stt_window_mismatch"
            stt = row["extensions"]["stt"]
            if stt["state"] != "final":
                return "stt_final_segment_required"
            if (stt["segment_id"] != row["source_event_id"] or stt["segment_revision"] != row["revision"]
                    or not isinstance(stt["speaker_id"], str) or not stt["speaker_id"].strip()
                    or "revision_view:current" not in row["match_reasons"] or "revision_effective:true" not in row["match_reasons"]
                    or "invalid_revision" in row["quality_flags"] or not isinstance(row["text"], str) or not row["text"].strip()):
                return "stt_segment_metadata_invalid"
            if not start <= _instant(stt["started_at"]) <= _instant(stt["ended_at"]) <= stop:
                return "stt_window_mismatch"
            confidence = stt["source_confidence"]
            if confidence is not None and (type(confidence) not in (int, float) or not 0 <= confidence <= 1):
                return "stt_segment_metadata_invalid"
            return None
        except (KeyError, TypeError, ValueError):
            return "stt_segment_metadata_invalid"

    def answer(self, payload, *, extraction=None):
        retrieval = self.retrieval.retrieve(payload) if extraction is None else self.retrieval.retrieve(payload, extraction=extraction)
        run_id = "answer-" + uuid4().hex
        result = {"status": "held", "request_id": payload["request_id"], "run_id": run_id, "answer": None,
                  "retrieval": retrieval, "provided_evidence": [], "cited_evidence_ids": [],
                  "model_invoked": False, "public_output": "not_attempted"}
        audit = {"run_id": run_id, "request_id": payload["request_id"], "policy_id": self.policy.policy_id,
                 "policy_sha256": self.policy.sha256, "retrieval": retrieval, "model_invoked": False}

        def finish(reason=None):
            if reason:
                result["reason_code"] = reason
            self.trace.append({**audit, "phase": "finished", "result": result})
            return result

        collection = retrieval.get("collection", {})
        if (retrieval.get("request_id") != payload["request_id"] or retrieval.get("status") != "complete"
                or collection.get("contract_version") != "1.0.1" or collection.get("partial") is not False):
            return finish("retrieval_incomplete")
        evidence = collection.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return finish("no_answer_evidence")
        if len(evidence) > 10:
            return finish("answer_evidence_limit")
        if self.domain == "chat_history":
            reason = self._chat_boundary(retrieval, evidence)
            if reason:
                return finish(reason)
        if self.domain == "stt_transcript":
            reason = self._stt_boundary(retrieval, evidence)
            if reason:
                return finish(reason)
        for event in evidence:
            reason = self.policy.check(event)
            if reason:
                return finish(reason)
        rows = [{"evidence_id": f"E{index}", **{key: deepcopy(event[key]) for key in content_fields(event)},
                 "content_sha256": content_digest(event)} for index, event in enumerate(evidence, 1)]
        prompt = AnswerPrompt(user_text=payload["text"], context_snapshot_id=run_id, context_messages=(),
            analysis={"intents": [self.domain], "query_text": payload["text"], "snapshot_id": run_id,
                      "queries": [{"purpose": self.domain, "query_text": payload["text"]}]},
            query_results=({"status": "ok", "materials": rows, "sources": [
                {**{key: deepcopy(row[key]) for key in ("evidence_id", "source_id", "record_id", "revision", "scope", "source_refs", "content_sha256")},
                 **({"actor_account_id": row["actor_account_id"]} if self.domain in ACTOR_DOMAINS else {})}
                for row in rows]},), evidence_citations=True)
        prompt_data = prompt.as_dict()
        if len(encoded(prompt_data)) > 12_000:
            return finish("answer_prompt_limit")
        result["provided_evidence"] = rows
        result["prompt_sha256"] = hashlib.sha256(encoded(prompt_data)).hexdigest()
        audit.update(prompt=prompt_data, prompt_sha256=result["prompt_sha256"])
        if isinstance(self.model, OllamaModelClient):
            audit["model_request"] = self.model.request_body(prompt)
            audit["model_request_sha256"] = hashlib.sha256(encoded(audit["model_request"])).hexdigest()
        # Persist the exact permitted prompt before model invocation; a trace
        # failure therefore prevents generation. No TextOutputAdapter is held.
        self.trace.append({**audit, "phase": "prepared"})
        result["model_invoked"] = audit["model_invoked"] = True
        try:
            answer = self.model.generate(prompt)
        except ModelAdapterError as error:
            audit["model_error"] = error.safe_diagnostic
            result["status"] = "model_error"
            return finish("answer_model_failed")
        except Exception:
            result["status"] = "model_error"
            return finish("answer_model_failed")
        audit["adapter_answer"] = answer
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
            return finish("answer_text_invalid")
        if self.domain in ACTOR_DOMAINS:
            try:
                reply = load_object(answer.encode("utf-8"))
            except PipelineError:
                return finish("answer_format_invalid")
            if set(reply) != {"quote", "evidence_id"} or not isinstance(reply["quote"], str) or not isinstance(reply["evidence_id"], str):
                return finish("answer_format_invalid")
            if reply["evidence_id"] not in {row["evidence_id"] for row in rows}:
                return finish("answer_citations_invalid")
            if reply["quote"] != rows[0]["text"]:
                return finish("answer_quote_not_preserved")
            audit["decoded_model_quote"] = reply["quote"]
            audit["model_reply_format"] = "quoted_evidence_json"
            result["quoted_evidence"] = deepcopy(reply)
            answer = reply["quote"] + " [" + reply["evidence_id"] + "]"
            if self.domain == "stt_transcript":
                answer = "逐字稿辨識文字：" + answer
                result["evidence_type"] = "stt_recognition"
                result["recognition"] = {**deepcopy(rows[0]["extensions"]["stt"]),
                    "quality_flags": deepcopy(rows[0]["quality_flags"]), "not_validated_against_audio": True}
            cited = [reply["evidence_id"]]
        else:
            cited = list(dict.fromkeys("E" + number for number in re.findall(r"\[E([0-9]+)\]", answer)))
        if not cited or set(cited) - {row["evidence_id"] for row in rows}:
            return finish("answer_citations_invalid")
        result.update(status="answered", answer=answer, cited_evidence_ids=cited)
        return finish()


def load_answer_routes(path, retrieval_routes, model, trace):
    selected = Path(path).resolve()
    value = load_object(selected.read_bytes())
    if (set(value) != {"schema_version", "routes"} or value["schema_version"] != "1.0.1"
            or not isinstance(value["routes"], dict) or not value["routes"]):
        raise PipelineError("answer_routes_configuration_invalid")
    routes = {}
    for name, spec in value["routes"].items():
        if (not isinstance(name, str) or re.fullmatch(r"/answer/[a-z][a-z0-9-]{0,39}", name) is None
                or not isinstance(spec, dict) or set(spec) != {"retrieval_route", "purpose_policy"}
                or not isinstance(spec["retrieval_route"], str) or spec["retrieval_route"] not in retrieval_routes):
            raise PipelineError("answer_route_invalid")
        routes[name] = EvidenceAnswerGateway(retrieval_routes[spec["retrieval_route"]],
            PurposePolicy.read(_local_path(spec["purpose_policy"], selected.parent)), model, trace)
    return routes
