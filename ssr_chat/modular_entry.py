"""Explicit local entry commands select independent server-owned gateways."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from uuid import uuid4

from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError, load_object

COMMANDS = {"/game": "game_knowledge", "/channel": "channel_knowledge",
            "/history": "chat_history", "/transcript": "stt_transcript"}
COMMAND_PATTERN = re.compile(r"(/[-a-z]{1,20})[ \t]+([\s\S]+)")
NESTED_COMMAND = re.compile(r"(?<!\S)/(?:game|channel|history|transcript)(?![-\w])")


class EntryGateway:
    def __init__(self, config, retrieval_routes, answer_routes, trace):
        if (not isinstance(config, dict) or not {"schema_version", "routing", "targets"} <= set(config)
                or set(config) - {"schema_version", "routing", "targets", "query_extraction"}
                or config["schema_version"] != "1.0.1" or config["routing"] not in ("explicit_command_v1", "explicit_or_planner_v1")
                or not isinstance(config["targets"], dict) or not config["targets"]
                or set(config["targets"]) - set(COMMANDS)):
            raise PipelineError("entry_configuration_invalid")
        targets = {}
        for command, route in config["targets"].items():
            if not isinstance(route, str):
                raise PipelineError("entry_target_invalid")
            if route in retrieval_routes and route.startswith("/retrieve/"):
                gateway, operation, data_modes = retrieval_routes[route], "retrieve", {"declared_synthetic", "operator_configured"}
            elif route in answer_routes and route.startswith("/answer/"):
                gateway, operation, data_modes = answer_routes[route], "answer", {"synthetic_only", "purpose_policy_gated"}
            else:
                raise PipelineError("entry_target_not_configured")
            profile = gateway.profile
            if (profile.get("domain") != COMMANDS[command] or profile.get("data_mode") not in data_modes
                    or profile.get("public_output_enabled") is not False
                    or profile.get("layer") not in ("raw", "draft", "published")
                    or (operation == "answer" and (profile.get("layer") != "raw" or profile.get("citations_required") is not True))):
                raise PipelineError("entry_target_boundary_invalid")
            targets[command] = (route, COMMANDS[command], operation, gateway)
        self._targets, self.trace = targets, trace
        self.routing = config["routing"]
        self._natural = None
        if self.routing == "explicit_or_planner_v1":
            from .modular_classification import NaturalQuestionRouter
            self._natural = NaturalQuestionRouter(targets)
        self._extractor = None
        if "query_extraction" in config:
            if self._natural is None:
                raise PipelineError("query_extraction_requires_natural_mode")
            from .modular_extraction import EntityQueryExtractor
            self._extractor = EntityQueryExtractor(config["query_extraction"], targets)

    @classmethod
    def read(cls, path, retrieval_routes, answer_routes, trace):
        return cls(load_object(Path(path).read_bytes()), retrieval_routes, answer_routes, trace)

    @property
    def trusted_ingress_contexts(self):
        """Backend-only exact bindings; callers cannot select an identity or scope."""
        return {command: deepcopy((gateway if operation == "retrieve" else gateway.retrieval).trusted_answer_context)
                for command, (_, _, operation, gateway) in self._targets.items()}

    @property
    def profile(self):
        profile = {"configured": True, "routing": self.routing, "data_mode": "synthetic_only",
                "public_output_enabled": False,
                "commands": {command: {"domain": domain, "operation": operation, "layer": gateway.profile["layer"]}
                             for command, (_, domain, operation, gateway) in self._targets.items()}}
        if self._extractor is not None:
            profile["query_extraction"] = {"enabled": True, "mode": "scope_bound_registered_entities_v1"}
        return profile

    def query(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"request_id", "text"}:
            raise RetrievalInputError("only_request_id_and_text_allowed")
        request_id, text = payload["request_id"], payload["text"]
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise RetrievalInputError("request_id_invalid")
        if not isinstance(text, str) or not text.strip() or len(text) > 16_384:
            raise RetrievalInputError("question_text_invalid")
        result = {"status": "held", "request_id": request_id, "entry_id": "entry-" + uuid4().hex,
                  "routing": self.routing, "command": None, "domain": None, "operation": None,
                  "answer": None, "model_invoked": False, "public_output": "not_attempted"}
        audit = {"entry_id": result["entry_id"], "request_id": request_id, "original_text": text}

        def finish(reason=None):
            if reason:
                result["reason_code"] = reason
            self.trace.append(deepcopy({**audit, "phase": "finished", "result": result}))
            return result

        match = COMMAND_PATTERN.fullmatch(text)
        if match is None:
            if self._natural is None or text.lstrip().startswith("/"):
                return finish("entry_command_required")
            if NESTED_COMMAND.search(text):
                return finish("entry_multiple_commands")
            classification = self._natural.classify(payload)
            audit["classification"] = classification
            result["classification"] = self._natural.summary(classification)
            self.trace.append(deepcopy({**audit, "phase": "classified"}))
            if classification["status"] != "selected":
                return finish(classification["reason_code"])
            command, question = classification["command"], text
            audit["selection_source"] = "natural_plan"
        else:
            command, question = match.groups()
            audit["selection_source"] = "explicit_command"
        if command not in COMMANDS:
            return finish("entry_command_unsupported")
        if not question.strip():
            return finish("entry_question_required")
        if NESTED_COMMAND.search(question):
            return finish("entry_multiple_commands")
        if command not in self._targets:
            return finish("entry_target_not_configured")
        route, domain, operation, gateway = self._targets[command]
        result.update(command=command, domain=domain, operation=operation)
        forwarded = {"request_id": request_id, "text": question}
        audit.update(command=command, domain=domain, operation=operation, target_route=route, forwarded_request=forwarded)
        extraction = None
        if (self._extractor is not None and audit["selection_source"] == "natural_plan" and self._extractor.handles(command)):
            extraction, decision = self._extractor.extract(command, question)
            audit["query_extraction"] = decision
            result["query_extraction"] = decision
            self.trace.append(deepcopy({**audit, "phase": "extracted"}))
            if extraction is None:
                return finish(decision["reason_code"])
        # Persist selection before any source retrieval or model invocation.
        # Only one bound gateway is called; authority and contexts never merge.
        self.trace.append(deepcopy({**audit, "phase": "selected"}))
        if operation == "answer":
            # An unexpected downstream failure may occur after generation.
            # Until its result arrives, do not claim that no model was invoked.
            result["model_invoked"] = None
        try:
            if extraction is None:
                target = gateway.answer(forwarded) if operation == "answer" else gateway.retrieve(forwarded)
            else:
                target = gateway.answer(forwarded, extraction=extraction) if operation == "answer" else gateway.retrieve(forwarded, extraction=extraction)
        except PipelineError as error:
            audit["target_error"] = {"code": error.code, "stage": error.stage}
            return finish("entry_target_failed")
        except Exception:
            return finish("entry_target_failed")
        if (not isinstance(target, dict) or target.get("request_id") != request_id
                or target.get("public_output") != "not_attempted"):
            return finish("entry_target_result_invalid")
        result["target_result"] = target
        if operation == "retrieve":
            if target.get("answer_generation") != "not_run":
                return finish("entry_target_result_invalid")
            if target.get("status") != "complete" or target.get("collection", {}).get("partial") is not False:
                return finish("retrieval_incomplete")
            result["status"] = "evidence_ready" if target["collection"].get("evidence") else "no_match"
        else:
            if type(target.get("model_invoked")) is not bool:
                return finish("entry_target_result_invalid")
            result["model_invoked"] = target["model_invoked"]
            if target.get("status") == "answered" and isinstance(target.get("answer"), str) and target["answer"].strip():
                result.update(status="answered", answer=target["answer"])
            elif target.get("status") in ("held", "model_error") and target.get("answer") is None:
                result["status"] = target["status"]
                return finish(target.get("reason_code", "entry_target_held"))
            else:
                return finish("entry_target_result_invalid")
        return finish()
