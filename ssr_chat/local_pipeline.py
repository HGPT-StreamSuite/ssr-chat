"""Offline native channel pipeline with a first-class disabled LLM boundary.

Evidence mode returns the actual native results. Prepare mode returns an exact
permitted prepared prompt and a held result; it never constructs a model client.
No HTTP server, socket, login, public output or schema/intent conversion is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from .modular_api import load_retrieval_routes
from .modular_answer import load_answer_routes
from .modular_entry import EntryGateway
from .modular_retrieval import PipelineError, load_object
from .stores import JsonlLog


def write_json(path, value):
    body = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    with Path(path).open("xb") as output:
        output.write(body)
    return {"path": str(path), "sha256": hashlib.sha256(body).hexdigest()}


def _file(path):
    selected = Path(path)
    if (not selected.is_absolute() or str(selected).startswith(("\\\\", "//")) or "://" in str(selected)
            or not selected.is_file()
            or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (selected, *selected.parents))):
        raise ValueError("explicit_existing_local_file_required")
    return selected.resolve()


def source_snapshot(repos):
    """Read Git identity and relevant source bytes; never mutate or inspect secrets."""
    versions = {}
    for name, value in repos.items():
        path = Path(value).resolve()
        if path.name != name or not (path / ".git").is_dir():
            raise ValueError("canonical_repository_required")
        def git(*args):
            result = subprocess.run(["git", "--no-optional-locks", "-C", str(path), *args], capture_output=True, timeout=10)
            if result.returncode:
                raise ValueError("source_version_unavailable")
            return result.stdout.decode("utf-8", "strict").strip()
        manifest = {}
        for folder in ("ssr_chat", "source", "tests", "fixtures", "contracts", "tools", "web"):
            for file in (path / folder).rglob("*"):
                if (file.is_file() and not file.is_symlink() and "__pycache__" not in file.parts
                        and file.suffix in {".py", ".json", ".jsonl", ".js", ".cjs", ".html", ".css", ".toml", ".lock"}):
                    manifest[file.relative_to(path).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        for filename in ("run.py", "pyproject.toml", "uv.lock", "README.md", ".gitignore"):
            file = path / filename
            if file.is_file():
                manifest[filename] = hashlib.sha256(file.read_bytes()).hexdigest()
        status = git("status", "--porcelain", "--untracked-files=all")
        versions[name] = {"path": str(path), "head": git("rev-parse", "HEAD"), "branch": git("symbolic-ref", "--short", "HEAD"),
                          "dirty": bool(status), "git_status": status.splitlines(), "source_files": manifest}
    body = json.dumps(versions, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {"repos": versions, "snapshot_sha256": hashlib.sha256(body).hexdigest()}


def effective_files(config_path, config):
    """Fingerprint only the fixed synthetic route/policy/snapshot inputs, no secrets or environment."""
    files = {config_path}
    for key in ("retrieval_routes_config", "answer_routes_config", "entry_routes_config"):
        files.add(_file(config[key]))
    routes_path = _file(config["retrieval_routes_config"])
    routes = load_object(routes_path.read_bytes())
    for route in routes["routes"].values():
        pipeline_path = _file(str((routes_path.parent / route["pipeline_config"]).resolve()))
        files.update({pipeline_path, _file(str((routes_path.parent / route["template"]).resolve()))})
        pipeline = load_object(pipeline_path.read_bytes())
        for field, repo_name in (("planner_repo", "hgpt-ssr-query-planner"),
                                 ("dispatcher_repo", "hgpt-ssr-dispatcher"),
                                 ("collector_repo", "hgpt-ssr-evidence-collector")):
            if not Path(pipeline[field]).is_absolute() or Path(pipeline[field]).resolve() != Path(config["repos"][repo_name]).resolve():
                raise ValueError("pipeline_component_source_binding_mismatch")
        if not Path(pipeline["python"]).is_absolute() or Path(pipeline["python"]).resolve() != Path(sys.executable).resolve():
            raise ValueError("pipeline_python_binding_mismatch")
        dispatcher_path = _file(str((pipeline_path.parent / pipeline["dispatcher_config"]).resolve()))
        files.add(dispatcher_path)
        dispatcher = load_object(dispatcher_path.read_bytes())
        if set(dispatcher["adapters"]) != {"channel_knowledge"}:
            raise ValueError("only_channel_snapshot_adapter_allowed")
        for adapter in dispatcher["adapters"].values():
            command = adapter["command"]
            if (adapter.get("kind") != "cli" or len(command) != 12
                    or any(not Path(command[index]).is_absolute() for index in (0, 7, 9, 11))
                    or not Path(adapter["cwd"]).is_absolute()
                    or Path(command[0]).resolve() != Path(sys.executable).resolve()
                    or command[1:7] != ["-B", "-X", "utf8", "-m", "ssr_chat.native_capture", "--repo"]
                    or Path(command[7]).resolve() != Path(config["repos"]["hgpt-ssr-channel-search"]).resolve()
                    or command[8] != "--config" or command[10] != "--artifact-root"
                    or Path(adapter["cwd"]).resolve() != Path(config["repos"]["ssr-chat"]).resolve()):
                raise ValueError("native_reader_source_binding_mismatch")
            capture_root = Path(command[11]).absolute()
            if (not capture_root.is_dir() or not capture_root.resolve().is_relative_to(config_path.parent.parent)
                    or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (capture_root, *capture_root.parents))):
                raise ValueError("private_reader_capture_root_required")
            reader_path = _file(command[command.index("--config") + 1])
            reader = load_object(reader_path.read_bytes())
            if reader.get("domain") != "channel_knowledge" or not reader.get("sources"):
                raise ValueError("channel_snapshot_configuration_required")
            files.add(reader_path)
            for source in reader["sources"]:
                if source.get("kind") != "snapshot" or source.get("source_kind") not in ("synthetic", "local_data", "real"):
                    raise ValueError("only_explicit_local_snapshots_allowed")
                files.add(_file(str((reader_path.parent / source["path"]).resolve())))
    answers_path = _file(config["answer_routes_config"])
    for answer in load_object(answers_path.read_bytes())["routes"].values():
        files.add(_file(str((answers_path.parent / answer["purpose_policy"]).resolve())))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


def execute(config_path, input_path, run_dir, *, mode="prepare"):
    # The library entry has the same bounded modes as the CLI, before any I/O.
    if not isinstance(mode, str) or mode not in ("evidence", "prepare"):
        raise ValueError("offline_mode_invalid")
    config_path, input_path = _file(config_path), _file(input_path)
    config = load_object(config_path.read_bytes())
    required = {"schema_version", "synthetic", "retrieval_routes_config", "answer_routes_config", "entry_routes_config", "repos"}
    if set(config) != required or config["schema_version"] != "1.0.1" or type(config["synthetic"]) is not bool:
        raise ValueError("explicit_local_run_configuration_required")
    names = {"ssr-chat", "hgpt-ssr-query-planner", "hgpt-ssr-dispatcher", "hgpt-ssr-channel-search", "hgpt-ssr-evidence-collector"}
    if not isinstance(config["repos"], dict) or set(config["repos"]) != names:
        raise ValueError("five_native_component_versions_required")
    checkout = Path(__file__).resolve().parents[1]
    for name, configured in config["repos"].items():
        expected = checkout if name == "ssr-chat" else checkout.parent / "components" / name
        if Path(configured).resolve() != expected.resolve():
            raise ValueError("canonical_workspace_binding_mismatch")
    for key in ("retrieval_routes_config", "answer_routes_config", "entry_routes_config"):
        _file(config[key])
    payload = load_object(input_path.read_bytes())
    if set(payload) != {"request_id", "text"}:
        raise ValueError("only_request_id_and_text_allowed")
    output = Path(run_dir).absolute()
    if str(output).startswith(("\\\\", "//")) or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (output, *output.parents)):
        raise ValueError("new_local_run_directory_required")
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"receipt_schema": "ssr-chat.local-native-execution.v1", "execution_id": "local-" + uuid4().hex,
               "request_id": payload.get("request_id"), "started_at": datetime.now(timezone.utc).isoformat(),
               "mode": mode, "input_kind": "synthetic" if config["synthetic"] else "operator_configured", "whole_bot_complete": False,
               "llm_invoked": False, "network_operations_requested": 0, "public_output": "not_attempted"}
    code = 2
    try:
        source_before = source_snapshot(config["repos"])
        files_before = effective_files(config_path, config)
        receipt["source_before"] = write_json(output / "source-before.json", source_before)
        receipt["effective_synthetic_files_before"] = write_json(output / "effective-files-before.json", files_before)
        receipt["input"] = write_json(output / "input.json", payload)
        receipt["operator_config"] = write_json(output / "operator-config.json", config)
        retrieval = load_retrieval_routes(config["retrieval_routes_config"], artifact_root=output / "native-stages")
        expected_mode = "declared_synthetic" if config["synthetic"] else "operator_configured"
        if set(retrieval) != {"/retrieve/channel-knowledge"} or retrieval["/retrieve/channel-knowledge"].profile.get("data_mode") != expected_mode:
            raise ValueError("channel_route_data_mode_mismatch")
        answers = load_answer_routes(config["answer_routes_config"], retrieval, None,
                                     JsonlLog(output / "answer-trace.jsonl"), prepare_only=True) if mode == "prepare" else {}
        entry = EntryGateway.read(config["entry_routes_config"], retrieval, answers, JsonlLog(output / "entry-trace.jsonl"))
        if set(entry.profile["commands"]) != {"/channel"} or entry.routing != "explicit_command_v1":
            raise ValueError("explicit_channel_command_only_no_aliases")
        receipt["entry_profile"] = entry.profile
        result = entry.query(payload)
        receipt["entry_result"] = write_json(output / "entry-result.json", result)
        receipt["status"] = result["status"]
        target = result.get("target_result", {})
        prepared = []
        trace_path = output / "answer-trace.jsonl"
        if trace_path.is_file():
            prepared = [load_object(line) for line in trace_path.read_bytes().splitlines() if line.strip()]
            prepared = [row for row in prepared if row.get("phase") == "prepared"]
        if prepared:
            if len(prepared) != 1 or prepared[0].get("request_id") != payload["request_id"] or prepared[0].get("model_invoked") is not False:
                raise ValueError("prepared_prompt_identity_invalid")
            receipt["prepared_prompt"] = write_json(output / "prepared-prompt.json", prepared[0]["prompt"])
            receipt["prompt_status"] = "prepared_not_sent"
        else:
            receipt["prompt_status"] = "not_prepared"
        if result.get("model_invoked") is not False or target.get("model_invoked", False) is not False:
            raise ValueError("offline_mode_must_never_invoke_a_model")
        receipt["llm_boundary"] = {"status": "not_attempted", "reason": "generation_disabled_no_connection_attempt",
                                   "sent_messages_artifact": None, "actual_response_artifact": None}
        receipt["output"] = {"kind": "local_structured_result", "artifact": receipt["entry_result"],
                             "generated_answer": None, "text_output_adapter_called": False, "public_reply": False}
        source_after = source_snapshot(config["repos"])
        files_after = effective_files(config_path, config)
        receipt["source_after"] = write_json(output / "source-after.json", source_after)
        receipt["effective_synthetic_files_after"] = write_json(output / "effective-files-after.json", files_after)
        receipt["source_unchanged"] = source_before == source_after and files_before == files_after
        if not receipt["source_unchanged"]:
            raise ValueError("source_changed_during_execution")
        if mode == "evidence" and result["status"] in {"evidence_ready", "no_match"}:
            receipt["completed_scope"] = "actual_native_evidence_pipeline"
            code = 0
        elif mode == "prepare" and result["status"] == "held" and result.get("reason_code") == "answer_generation_not_enabled" and prepared:
            receipt["completed_scope"] = "actual_native_retrieval_and_permitted_prompt_boundary"
            code = 3
        else:
            receipt["completed_scope"] = "held_before_requested_boundary"
            code = 1
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        receipt.update(status="error", error_code=getattr(error, "code", str(error) if isinstance(error, ValueError) else type(error).__name__))
    finally:
        receipt.update(exit_code=code, finished_at=datetime.now(timezone.utc).isoformat())
        write_json(output / "receipt.json", receipt)
    return receipt, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--mode", choices=("evidence", "prepare"), default="prepare")
    args = parser.parse_args(argv)
    try:
        receipt, code = execute(args.config, args.input, args.run_dir, mode=args.mode)
        print(json.dumps(receipt, ensure_ascii=False, allow_nan=False))
        return code
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "error", "reason_code": type(error).__name__, "llm_invoked": False,
                          "public_output": "not_attempted"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
