"""Opt-in CLI composition of independent QueryPlan, dispatcher and collector programs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any

MAX_BYTES = 2_000_000
CHAT_RECENT_ADAPTER = "recent_utterance_to_last_said_v1"
DOMAINS = {"game_knowledge", "person_profile", "chat_history", "stt_transcript", "channel_knowledge"}


class PipelineError(ValueError):
    def __init__(self, code: str, stage: str = "configuration") -> None:
        self.code, self.stage = code, stage
        super().__init__(code)


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise PipelineError("duplicate_json_key", "input")
        value[key] = child
    return value


def _constant(value):
    raise PipelineError("nonfinite_json_number", "input")


def load_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BYTES:
        raise PipelineError("input_too_large", "input")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PipelineError("invalid_json", "input") from exc
    if not isinstance(value, dict):
        raise PipelineError("object_required", "input")
    return value


def _local_path(value: Any, base: Path, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip() or "://" in value or value.startswith(("\\\\", "//")):
        raise PipelineError("local_path_required")
    path = (base / value).resolve()
    if not (path.is_dir() if directory else path.is_file()):
        raise PipelineError("local_path_missing")
    return path


@dataclass(frozen=True)
class PipelineConfig:
    python: Path
    planner_repo: Path
    dispatcher_repo: Path
    collector_repo: Path
    dispatcher_config: Path
    timeout: float = 60.0
    chat_recent_intent_adapter: str | None = None

    @classmethod
    def read(cls, path: str | Path) -> "PipelineConfig":
        selected = Path(path).resolve()
        value = load_object(selected.read_bytes())
        required = {"schema_version", "python", "planner_repo", "dispatcher_repo", "collector_repo", "dispatcher_config"}
        if set(value) - required - {"timeout_seconds", "chat_recent_intent_adapter"} or required - set(value) or value["schema_version"] != "1.0.1":
            raise PipelineError("pipeline_configuration_invalid")
        chat_adapter = value.get("chat_recent_intent_adapter")
        if chat_adapter is not None and chat_adapter != CHAT_RECENT_ADAPTER:
            raise PipelineError("chat_recent_intent_adapter_invalid")
        base = selected.parent
        python = _local_path(value["python"], base)
        if python.stem.lower() not in {"python", "python3", "python3.11", "python3.12", "python3.13"}:
            raise PipelineError("python_executable_required")
        planner = _local_path(value["planner_repo"], base, directory=True)
        dispatcher = _local_path(value["dispatcher_repo"], base, directory=True)
        collector = _local_path(value["collector_repo"], base, directory=True)
        for repo, entry in ((planner, "source/hgpt_ssr/__main__.py"),
                            (dispatcher, "source/run_hgpt_ssr.py"), (collector, "run.py")):
            if not (repo / entry).is_file():
                raise PipelineError("component_entry_missing")
        dispatch_config = _local_path(value["dispatcher_config"], base)
        settings = load_object(dispatch_config.read_bytes())
        if settings.get("schema_version") != "1.0.1" or not isinstance(settings.get("adapters"), dict):
            raise PipelineError("dispatcher_configuration_invalid")
        if any(not isinstance(adapter, dict) or adapter.get("kind") != "cli" for adapter in settings["adapters"].values()):
            raise PipelineError("only_local_cli_adapters_supported")
        timeout = value.get("timeout_seconds", 60)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise PipelineError("timeout_invalid")
        for adapter in settings["adapters"].values():
            child_timeout = adapter.get("timeout_seconds")
            if type(child_timeout) not in (int, float) or not math.isfinite(child_timeout) or not 0 < child_timeout < timeout:
                raise PipelineError("adapter_timeout_must_be_shorter_than_stage")
        return cls(python, planner, dispatcher, collector, dispatch_config, float(timeout), chat_adapter)


class ModularRetrievalPipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def _invoke(self, stage: str, repo: Path, arguments: list[str], payload: dict,
                allowed_exit_codes: set[int], trace: list[dict]) -> dict:
        command = [str(self.config.python), "-B", "-X", "utf8", *arguments]
        allowed_env = {"systemroot", "windir", "systemdrive", "comspec", "path", "pathext",
                       "temp", "tmp", "userprofile", "localappdata", "appdata", "programfiles", "programdata"}
        env = {key: value for key, value in os.environ.items() if key.lower() in allowed_env}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise PipelineError("stage_input_too_large", stage)
        started = time.monotonic()
        observed = {"stage": stage, "command": command, "cwd": str(repo),
                    "started_at": datetime.now(timezone.utc).isoformat()}
        trace.append(observed)
        try:
            result = subprocess.run(command, cwd=repo, env=env, input=encoded, capture_output=True,
                                    timeout=self.config.timeout, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise PipelineError("component_timeout", stage) from exc
        except OSError as exc:
            raise PipelineError("component_unavailable", stage) from exc
        finally:
            observed["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        observed.update(exit_code=result.returncode, stdout_bytes=len(result.stdout), stderr_bytes=len(result.stderr),
                        stdout_sha256=hashlib.sha256(result.stdout).hexdigest())
        if result.returncode not in allowed_exit_codes:
            raise PipelineError("component_failed", stage)
        try:
            return load_object(result.stdout)
        except PipelineError as exc:
            raise PipelineError("component_invalid_json", stage) from exc

    def plan_only(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run only the configured native planner, never dispatcher/collector."""
        if (not isinstance(request, dict) or not isinstance(request.get("request"), dict)
                or not isinstance(request.get("trusted_context"), dict)):
            raise PipelineError("request_and_trusted_context_required", "input")
        request_id = request["request"].get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or request["request"].get("schema_version") != "1.0.1":
            raise PipelineError("request_identity_or_version_invalid", "input")
        trace: list[dict] = []
        plan = self._invoke("planner", self.config.planner_repo,
                            ["-m", "source.hgpt_ssr", "--contract-version", "1.0.1", "--input", "-"],
                            request, {0, 1, 2, 3}, trace)
        if plan.get("request_id") != request_id or plan.get("schema_version") != "1.0.1" or not isinstance(plan.get("tasks"), list):
            raise PipelineError("plan_identity_mismatch", "planner")
        return {"plan": plan, "stages": trace}

    def run(self, request: dict[str, Any], *, expected_domain: str | None = None) -> dict[str, Any]:
        if expected_domain is not None and (not isinstance(expected_domain, str) or expected_domain not in DOMAINS):
            raise PipelineError("expected_domain_invalid", "input")
        if not isinstance(request.get("request"), dict) or not isinstance(request.get("trusted_context"), dict):
            raise PipelineError("request_and_trusted_context_required", "input")
        request_id = request["request"].get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or request["request"].get("schema_version") != "1.0.1":
            raise PipelineError("request_identity_or_version_invalid", "input")
        trace: list[dict] = []
        plan = self._invoke("planner", self.config.planner_repo,
                            ["-m", "source.hgpt_ssr", "--contract-version", "1.0.1", "--input", "-"],
                            request, {0, 1, 2, 3}, trace)
        if plan.get("request_id") != request_id or plan.get("schema_version") != "1.0.1" or not isinstance(plan.get("tasks"), list):
            raise PipelineError("plan_identity_mismatch", "planner")
        task_ids = [task.get("task_id") for task in plan["tasks"] if isinstance(task, dict)]
        if len(task_ids) != len(plan["tasks"]) or any(not isinstance(key, str) or not key for key in task_ids) or len(set(task_ids)) != len(task_ids):
            raise PipelineError("plan_task_identity_invalid", "planner")
        if expected_domain is not None:
            for task in plan["tasks"]:
                query = task.get("query")
                if (task.get("domain") != expected_domain or not isinstance(query, dict)
                        or not isinstance(query.get("scope"), dict) or query["scope"].get("domain") != expected_domain):
                    # Named routes are single-domain even if a configured planner
                    # returns an unexpected task. Stop before any source is read.
                    raise PipelineError("route_plan_domain_mismatch", "planner")
        dispatch_plan = plan
        adaptations = []
        if self.config.chat_recent_intent_adapter == CHAT_RECENT_ADAPTER:
            dispatch_plan = deepcopy(plan)
            for task in dispatch_plan["tasks"]:
                if task.get("domain") != "chat_history":
                    continue
                query = task.get("query")
                if not isinstance(query, dict):
                    raise PipelineError("chat_recent_adapter_query_invalid", "planner")
                if query.get("intent") != "recent_utterance":
                    continue
                if (query.get("schema_version") != "1.0.1" or not isinstance(query.get("scope"), dict)
                        or query["scope"].get("domain") != "chat_history"):
                    raise PipelineError("chat_recent_adapter_query_invalid", "planner")
                query["intent"] = "last_said"
                adaptations.append({"adapter": CHAT_RECENT_ADAPTER, "task_id": task["task_id"],
                                    "domain": "chat_history", "schema_version": "1.0.1",
                                    "from_intent": "recent_utterance", "to_intent": "last_said"})
        dispatched = self._invoke("dispatcher", self.config.dispatcher_repo,
                                  ["source/run_hgpt_ssr.py", "--plan", "-", "--config", str(self.config.dispatcher_config)],
                                  dispatch_plan, {0, 1}, trace)
        if dispatched.get("request_id") != request_id or dispatched.get("schema_version") != "1.0.1" or not isinstance(dispatched.get("tasks"), list):
            raise PipelineError("dispatcher_identity_mismatch", "dispatcher")
        results = {}
        for task in dispatched["tasks"]:
            if (not isinstance(task, dict) or not isinstance(task.get("task_id"), str)
                    or task["task_id"] not in task_ids or task["task_id"] in results or not isinstance(task.get("result"), dict)):
                raise PipelineError("dispatcher_task_mapping_invalid", "dispatcher")
            results[task["task_id"]] = task["result"]
            if expected_domain is not None and task["result"].get("domain") != expected_domain:
                raise PipelineError("route_result_domain_mismatch", "dispatcher")
        if set(results) != set(task_ids):
            raise PipelineError("dispatcher_task_mapping_incomplete", "dispatcher")
        collected = self._invoke("collector", self.config.collector_repo, ["run.py", "--input", "-"],
                                 {"query_plan": plan, "domain_results": results}, {0, 1}, trace)
        # Collector's aggregate envelope is not a DomainResult. Its public
        # version field is contract_version; keep the envelope unchanged.
        if collected.get("request_id") != request_id or collected.get("contract_version") != "1.0.1":
            raise PipelineError("collector_identity_mismatch", "collector")
        complete = (collected.get("status") in {"ok", "no_match"} and collected.get("partial") is False
                    and not plan.get("missing_inputs") and all(stage["exit_code"] == 0 for stage in trace))
        report = {"status": "complete" if complete else "incomplete", "schema_version": "1.0.1", "request_id": request_id,
                  "plan": plan, "dispatch": dispatched, "collection": collected, "stages": trace,
                  "answer_generation": "not_run", "public_output": "not_attempted"}
        if adaptations:
            report.update(dispatch_plan=dispatch_plan, query_adaptations=adaptations)
        return report
