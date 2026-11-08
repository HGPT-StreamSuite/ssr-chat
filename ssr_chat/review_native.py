"""Fixed synthetic tools for the review page, using the real offline native entry."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from types import MappingProxyType
from uuid import uuid4

from . import local_pipeline

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures" / "review_native"
INTERFACE_VERSION = "ssr-chat.review-native.v1"
TOOLS = MappingProxyType({
    "channel-evidence": ("evidence", "allowed"),
    "channel-prepare": ("prepare", "allowed"),
    "channel-purpose-denied": ("prepare", "prohibited"),
})


def native_examples():
    return [{"id": key, "label": label, "request": {"tool": tool, "text": text}} for key, label, tool, text in (
        ("prepare", "原生檢索 → 準備 prompt（不生成）", "channel-prepare", "/channel launch"),
        ("evidence", "原生證據檢索", "channel-evidence", "/channel launch"),
        ("denied", "用途拒絕控制", "channel-purpose-denied", "/channel launch"),
        ("no-match", "查無資料", "channel-evidence", "/channel absent-synthetic-term-92837"),
        ("unknown-command", "非法指令", "channel-evidence", "/unknown launch"),
        ("nested-command", "巢狀指令", "channel-evidence", "/channel launch /history anything"),
    )]


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {"tool", "text"}:
        raise ValueError("native_request_fields_invalid")
    if not isinstance(request["tool"], str) or request["tool"] not in TOOLS:
        raise ValueError("native_tool_not_registered")
    if not isinstance(request["text"], str) or not request["text"].strip() or len(request["text"]) > 2000:
        raise ValueError("question_text_invalid")


def browser_projection(value):
    """Keep native values, replacing private artifact locations with hash references."""
    if isinstance(value, list):
        return [browser_projection(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"path", "sha256"}:
        return {"storage": "private_local_artifact", "sha256": value["sha256"]}
    return {key: browser_projection(child) for key, child in value.items()
            if key not in {"cwd", "path", "directory", "artifact_dir", "artifact_root"}
            and not (key == "command" and isinstance(child, list))}


class NativeReview:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(exist_ok=False)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "synthetic-source"
        self.reader_io = self.root / "native-query-io"
        for directory in (self.config_dir, self.data_dir, self.reader_io):
            directory.mkdir()
        self.operators = self._build_registered_bundle()

    def _save(self, name, value):
        path = self.config_dir / name
        local_pipeline.write_json(path, value)
        return str(path)

    def _build_registered_bundle(self):
        provenance = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        contents = {}
        for name, expected in provenance["files_sha256"].items():
            if Path(name).name != name:
                raise ValueError("registered_fixture_name_invalid")
            raw = (FIXTURES / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("registered_fixture_integrity_failed")
            contents[name] = raw
        reader = json.loads(contents["reader-config.json"])
        for source in reader["sources"]:
            name = source["path"]
            if Path(name).name != name or source.get("kind") != "snapshot" or source.get("source_kind") != "synthetic":
                raise ValueError("registered_synthetic_source_required")
            raw = contents[name]
            for line in raw.decode("utf-8-sig").splitlines():
                if line.strip() and "synthetic" not in json.loads(line).get("quality_flags", []):
                    raise ValueError("registered_synthetic_record_required")
            destination = self.data_dir / name
            if not destination.exists():
                with destination.open("xb") as handle:
                    handle.write(raw)
        reader_path = self.data_dir / "config.json"
        local_pipeline.write_json(reader_path, reader)
        template = json.loads(contents["host-template.json"])
        selector = deepcopy(template["trusted_context"]["access_scope"])
        names = ("hgpt-ssr-query-planner", "hgpt-ssr-dispatcher", "hgpt-ssr-channel-search", "hgpt-ssr-evidence-collector")
        repos = {"ssr-chat": str(REPO), **{name: str(REPO.parent / "components" / name) for name in names}}
        dispatcher = self._save("dispatcher.json", {"schema_version": "1.0.1", "access_scope": selector, "adapters": {
            "channel_knowledge": {"kind": "cli", "command": [sys.executable, "-B", "-X", "utf8", "-m", "ssr_chat.native_capture",
                "--repo", repos[names[2]], "--config", str(reader_path), "--artifact-root", str(self.reader_io)],
                "cwd": str(REPO), "timeout_seconds": 10, "max_output_bytes": 1048576, "access_scope": deepcopy(selector)}}})
        pipeline = self._save("pipeline.json", {"schema_version": "1.0.1", "python": sys.executable,
            "planner_repo": repos[names[0]], "dispatcher_repo": repos[names[1]], "collector_repo": repos[names[3]],
            "dispatcher_config": dispatcher, "timeout_seconds": 20})
        template_path = self._save("template.json", template)
        retrieval = self._save("retrieval.json", {"schema_version": "1.0.1", "routes": {"/retrieve/channel-knowledge": {
            "domain": "channel_knowledge", "pipeline_config": pipeline, "template": template_path}}})
        answers = {}
        for policy in ("allowed", "prohibited"):
            name = "purpose-policy.json" if policy == "allowed" else "purpose-policy-prohibited.json"
            purpose = self._save("purpose-" + policy + ".json", json.loads(contents[name]))
            answers[policy] = self._save("answer-" + policy + ".json", {"schema_version": "1.0.1", "routes": {
                "/answer/channel-knowledge": {"retrieval_route": "/retrieve/channel-knowledge", "purpose_policy": purpose}}})
        operators = {}
        for tool, (mode, policy) in TOOLS.items():
            entry = self._save(tool + "-entry.json", {"schema_version": "1.0.1", "routing": "explicit_command_v1", "targets": {
                "/channel": "/retrieve/channel-knowledge" if mode == "evidence" else "/answer/channel-knowledge"}})
            operators[tool] = self._save(tool + "-operator.json", {"schema_version": "1.0.1", "synthetic": True,
                "repos": repos, "retrieval_routes_config": retrieval, "answer_routes_config": answers[policy], "entry_routes_config": entry})
        return MappingProxyType(operators)

    def run(self, request_id, request):
        validate_request(request)
        operation = self.root / ("operation-" + uuid4().hex)
        operation.mkdir()
        payload = {"request_id": request_id, "text": request["text"]}
        input_path = operation / "input.json"
        local_pipeline.write_json(input_path, payload)
        previous_readers = {p.name for p in self.reader_io.iterdir()}
        execution = operation / "execution"
        receipt, exit_code = local_pipeline.execute(self.operators[request["tool"]], input_path, execution, mode=TOOLS[request["tool"]][0])
        def artifact(name):
            return json.loads((execution / name).read_text(encoding="utf-8")) if (execution / name).is_file() else None
        entry = artifact("entry-result.json")
        prepared = artifact("prepared-prompt.json")
        readers = []
        for folder in self.reader_io.iterdir():
            if folder.name not in previous_readers and (folder / "receipt.json").is_file():
                readers.append(json.loads((folder / "receipt.json").read_text(encoding="utf-8")))
        target = (entry or {}).get("target_result", {})
        retrieval = target.get("retrieval", target)
        evidence = retrieval.get("collection", {}).get("evidence", [])
        stages = retrieval.get("stages", [])
        if receipt.get("llm_invoked") is not False or (entry or {}).get("model_invoked", False) is not False:
            raise ValueError("native_review_must_never_invoke_model")
        if entry is None:
            entry = {"status": "error", "reason_code": receipt.get("error_code", "native_execution_failed"),
                     "model_invoked": False, "public_output": "not_attempted"}
        snapshot = artifact("source-before.json")
        return {"operation_id": request_id, "execution_mode": "actual_native_pipeline", "tool": request["tool"],
                "entrypoint": "ssr_chat.local_pipeline.execute (in-process)", "pipeline_exit_code": exit_code,
                "execution_status": receipt.get("status"), "execution_error_code": receipt.get("error_code"),
                "result": browser_projection(entry), "prompt_status": receipt.get("prompt_status", "not_prepared"),
                "model_invoked": False, "public_output": "not_attempted", "generated_answer": None,
                "source_snapshot_sha256": (snapshot or {}).get("snapshot_sha256"),
                "source_unchanged": receipt.get("source_unchanged"), "private_artifact_id": operation.name,
                "evidence": {"input": payload, "records": evidence, "prompt": prepared,
                             "trace": browser_projection(stages), "native_reader_receipts": browser_projection(readers),
                             "llm_boundary": browser_projection(receipt.get("llm_boundary", {}))}}
