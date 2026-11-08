"""Exact local interface checks; no HTTP socket, browser, model or installation."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from ssr_chat.local_pipeline import source_snapshot, write_json
from ssr_chat.review_web import REPO, source_version

OUT = Path(__file__).resolve().parent
REPOS = {"ssr-chat": str(REPO), **{name: str(REPO.parent / "components" / name) for name in (
    "hgpt-ssr-query-planner", "hgpt-ssr-dispatcher", "hgpt-ssr-channel-search", "hgpt-ssr-evidence-collector")}}


def main():
    environment = {key: os.environ.get(key) for key in ("UV_PROJECT_ENVIRONMENT", "UV_PYTHON_DOWNLOADS", "SSR_CHAT_REVIEW_HTTP_TESTS")}
    rows = []
    commands = [
        ("mode-boundary", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_local_pipeline.py", "-v"]),
        ("interface-contract", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_interface_contracts.py", "-v"]),
        ("review-regression", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_review_web.py", "-v"]),
        ("native-prepare", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_review_native.py", "-k", "test_prepare_uses_actual_receipts_and_same_prompt_without_generation", "-v"]),
    ]
    for name, arguments in commands:
        before = {"local": source_version(), "native_graph": source_snapshot(REPOS)}
        write_json(OUT / (name + ".source-before.json"), before)
        command = [sys.executable, "-B", "-X", "utf8", *arguments]
        started = datetime.now(timezone.utc).isoformat()
        result = subprocess.run(command, cwd=REPO, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for suffix, content in (("stdout", result.stdout), ("stderr", result.stderr)):
            with (OUT / (name + "." + suffix + ".log")).open("xb") as output:
                output.write(content)
        after = {"local": source_version(), "native_graph": source_snapshot(REPOS)}
        write_json(OUT / (name + ".source-after.json"), after)
        row = {"id": name, "command": command, "cwd": str(REPO), "environment": environment,
               "python_version": sys.version, "started_at": started, "exit_code": result.returncode,
               "local_source_unchanged": before["local"] == after["local"],
               "native_graph_unchanged": before["native_graph"] == after["native_graph"],
               "source_before": name + ".source-before.json", "source_after": name + ".source-after.json",
               "stdout": name + ".stdout.log", "stderr": name + ".stderr.log"}
        rows.append(row)
        print(json.dumps({key: row[key] for key in ("id", "exit_code", "local_source_unchanged", "native_graph_unchanged")}), flush=True)
        write_json(OUT / (name + ".execution.json"), row)
        if result.returncode or not row["local_source_unchanged"]:
            break
    write_json(OUT / "index.json", {"kind": "local_interface_test_receipts", "results": rows,
        "browser_gate": "blocked_not_retried", "full_latest_bot_acceptance": False,
        "native_integration_scope": "one actual prepare call only; unchanged other scenarios retain prior c9 evidence"})
    return 0 if len(rows) == len(commands) and all(row["exit_code"] == 0 and row["local_source_unchanged"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
