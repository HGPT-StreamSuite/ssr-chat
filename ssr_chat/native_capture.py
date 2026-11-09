"""Record a native synthetic channel-reader invocation without changing its wire bytes.

This is an operator-configured local CLI adapter, not a mock or a schema converter.
It has no network, authentication, model, or manual-link command path.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from .modular_retrieval import load_object


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--artifact-root", required=True)
    args = parser.parse_args(argv)
    repo, config, root = (Path(v).resolve() for v in (args.repo, args.config, args.artifact_root))
    if repo.name != "hgpt-ssr-channel-search" or not (repo / "source/ssr.py").is_file():
        parser.error("only the configured native channel-search repository is supported")
    settings = load_object(config.read_bytes())
    if (settings.get("domain") != "channel_knowledge" or not settings.get("sources")
            or any(s.get("kind") != "snapshot" or s.get("source_kind") not in ("synthetic", "local_data", "real")
                   for s in settings["sources"])):
        parser.error("only explicit local channel snapshots are supported")
    if not root.is_dir() or root.is_symlink():
        parser.error("an existing operator-owned artifact directory is required")
    raw = sys.stdin.buffer.read(2_000_001)
    if len(raw) > 2_000_000:
        parser.error("query input too large")
    query = load_object(raw)
    if query.get("scope", {}).get("domain") != "channel_knowledge":
        parser.error("channel query required")
    folder = root / ("reader-" + uuid4().hex)
    folder.mkdir(exist_ok=False)
    command = [sys.executable, "-B", "-X", "utf8", str(repo / "source/ssr.py"), "--config", str(config), "--request", "-"]
    (folder / "stdin.bin").write_bytes(raw)
    receipt = {"kind": "actual_native_channel_query", "command": command, "cwd": str(repo),
               "started_at": datetime.now(timezone.utc).isoformat(), "request_id": query.get("request_id"),
               "input_sha256": hashlib.sha256(raw).hexdigest(),
               "data_mode": "synthetic" if all(s["source_kind"] == "synthetic" for s in settings["sources"]) else "local_data",
               "wire_conversion": False}
    output = stderr = b""
    code = 2
    try:
        result = subprocess.run(command, cwd=repo, input=raw, capture_output=True, timeout=8,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output, stderr, code = result.stdout, result.stderr, result.returncode
        receipt["outcome"] = "completed"
    except subprocess.TimeoutExpired as error:
        output, stderr = error.stdout or b"", error.stderr or b""
        receipt["outcome"] = "timeout"
    except OSError:
        receipt["outcome"] = "process_unavailable"
    finally:
        (folder / "stdout.bin").write_bytes(output)
        (folder / "stderr.bin").write_bytes(stderr)
        receipt.update(exit_code=code, finished_at=datetime.now(timezone.utc).isoformat(),
                       stdout_sha256=hashlib.sha256(output).hexdigest(), stderr_sha256=hashlib.sha256(stderr).hexdigest())
        (folder / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.buffer.write(output)
    sys.stderr.buffer.write(stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
