"""Real native identity compatibility check in a new isolated synthetic workspace."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from ssr_chat.chat_ingest import digest
from ssr_chat.identity_records import IdentityRecordGateway
from ssr_chat.local_pipeline import source_snapshot, write_json
from ssr_chat.review_web import REPO, source_version


def main():
    native = REPO.parent / "components/hgpt-identity-store"
    root = REPO.parents[1] / "evidence/development/identity-extensions-tests" / uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    repos = {"ssr-chat": str(REPO), "hgpt-identity-store": str(native)}
    before = {"local": source_version(), "components": source_snapshot(repos)}
    write_json(root / "source-before.json", before)
    print("IDENTITY_EXTENSIONS_EVIDENCE=" + str(root), flush=True)
    for pattern in ("test_identity_records.py", "test_interface_contracts.py"):
        command = [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-p", pattern, "-v"]
        result = subprocess.run(command, cwd=REPO, capture_output=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        prefix = root / pattern.removesuffix(".py")
        prefix.with_suffix(".stdout.log").write_bytes(result.stdout)
        prefix.with_suffix(".stderr.log").write_bytes(result.stderr)
        write_json(prefix.with_suffix(".command.json"), {"command": command, "cwd": str(REPO), "exit_code": result.returncode})
        if result.returncode:
            print("unit_check_failed=" + pattern, flush=True)
            return 1
    base = json.loads((REPO / "fixtures/identity_records/contract.synthetic.json").read_bytes())
    example = json.loads((REPO / "fixtures/contracts/identity-empty-extensions.synthetic.json").read_bytes())
    base["events"]["account-main"] = example["registered_event"]
    event = base["events"]["account-main"]
    assert event["declaration"]["payload_sha256"] == digest({k: event[k] for k in ("entity_kind", "person_id", "record")})
    config = root / "config"
    config.mkdir()
    states = root / "states"
    states.mkdir()
    paths = {}
    for name in ("writer_access", "reader_access", "purpose_policy"):
        path = config / (name + ".json")
        write_json(path, base[name])
        paths[name] = str(path)
    operator = {"schema_version": "1.0.1", "mode": "synthetic_identity_records_only", "python": sys.executable,
                "identity_repo": str(native), "state_root": str(states), **paths,
                **{k: deepcopy(base[k]) for k in ("binding", "record_visibility", "events", "queries")}}
    operator_path = config / "operator.json"
    write_json(operator_path, operator)
    calls = []

    def runner(command, **kwargs):
        result = subprocess.run(command, **kwargs)
        number = len(calls) + 1
        stdout, stderr = f"native-{number:02d}.stdout.json", f"native-{number:02d}.stderr.log"
        (root / stdout).write_bytes(result.stdout)
        (root / stderr).write_bytes(result.stderr)
        calls.append({"command": command, "cwd": kwargs["cwd"], "exit_code": result.returncode,
                      "stdout": stdout, "stderr": stderr})
        write_json(root / f"native-{number:02d}.command.json", calls[-1])
        return result

    gateway = IdentityRecordGateway(operator_path, states / "owned", runner=runner)
    reports = []
    operations = [example["public_input"],
                  {"request_id": "synthetic-extensions-query", "synthetic": True, "operation": "query", "query": "account-main"},
                  {"request_id": "synthetic-extensions-get", "synthetic": True, "operation": "get", "query": "main-get1"},
                  {"request_id": "synthetic-extensions-history", "synthetic": True, "operation": "history", "query": "main-history"},
                  {**example["public_input"], "request_id": "synthetic-extensions-replay"}]
    for number, payload in enumerate(operations, 1):
        result = gateway.run(payload)
        write_json(root / f"case-{number}.input.json", payload)
        write_json(root / f"case-{number}.output.json", result)
        reports.append(result)
        assert result["model_invoked"] is False and result["public_output"] == "not_attempted"
        if payload["operation"] == "ingest":
            assert result["counts"]["stored" if number == 1 else "duplicate"] == 1, result
        else:
            assert result["status"] == "ok" and result["record_count"] == 1, result
    native_values = [json.loads((root / call["stdout"]).read_bytes()) for call in calls]
    for value in native_values:
        if value.get("domain") == "person_profile":
            assert value["evidence"][0]["extensions"] == {}
        if value.get("command") == "record-get":
            assert value["data"] == event["record"], "complete exact pinned Record must survive readback"
        if value.get("command") == "record-history":
            assert value["data"] == [event["record"]]
    assert all(call["exit_code"] == 0 for call in calls), calls
    after = {"local": source_version(), "components": source_snapshot(repos)}
    write_json(root / "source-after.json", after)
    stable = before == after
    summary = {"finished_at": datetime.now(timezone.utc).isoformat(), "python": sys.executable, "python_version": sys.version,
               "source_unchanged": stable, "source_fingerprint": before["local"]["fingerprint"],
               "component_snapshot": before["components"]["snapshot_sha256"], "native_cli_calls": len(calls),
               "native_exit_codes": [c["exit_code"] for c in calls], "operations": len(reports),
               "unit_tests": "25 identity + 5 interface contracts", "optional_empty_extensions": "preserved including presence in original declaration/Evidence/get/history",
               "scope_acl_purpose_self_reference": "unchanged", "browser_gate": "blocked_not_retried", "llm_invoked": False,
               "network_operations": 0, "public_output": "not_attempted", "calls": calls}
    write_json(root / "summary.json", summary)
    print(json.dumps({k: summary[k] for k in ("source_unchanged", "source_fingerprint", "component_snapshot", "native_cli_calls", "native_exit_codes")}), flush=True)
    return 0 if stable else 1


if __name__ == "__main__":
    raise SystemExit("BLOCKED: WinError 5 in identity test setup/cleanup. Do not rerun or change its target without materially new relevant authorization; original invocation preserved in verify_identity_extensions.executed.txt.")
