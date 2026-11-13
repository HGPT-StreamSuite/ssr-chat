"""Long-lived worker for ssr_chat.warm_pool: runs an unchanged component CLI entry point once per request.

Started as ``python -B -X utf8 warm_worker.py`` so that it needs no package context. Requests and responses use the
framed JSON protocol of ``warm_pool``. The component sees an ordinary process: fresh sys.argv, stdin, stdout and
stderr for every call and the same exit-code semantics. Only module-level state of the component survives.
"""
from __future__ import annotations

import base64
import io
import os
import runpy
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Appended, never inserted first: the component's own directories must win every name lookup.
sys.path.append(ROOT)

from ssr_chat.warm_pool import read_frame, write_frame  # noqa: E402


def _install_dispatcher_hook() -> str:
    """Route the dispatcher's CLI adapters through warm workers instead of a new process per task."""
    try:
        from hgpt_ssr import runner, transport
        from hgpt_ssr.jsonio import dumps
        from ssr_chat.warm_pool import WarmCrashed, WarmPool, WarmTimeout, WarmUnavailable, parse_command
        original = runner.invoke
        outcome_type = transport.Outcome
    except Exception as exc:  # unknown dispatcher layout: stay on the cold path
        return "failed:" + type(exc).__name__
    pool = WarmPool()

    def invoke(adapter, query):
        entry = parse_command(adapter.command, adapter.cwd) if adapter.kind == "cli" else None
        if entry is None:
            return original(adapter, query)
        limit = adapter.max_output_bytes
        try:
            done = pool.call(entry, dumps(query).encode("utf-8"), adapter.timeout_seconds, env=dict(os.environ))
        except WarmTimeout:
            return outcome_type(reason_code="adapter_timeout")
        except WarmCrashed:
            return outcome_type(reason_code="adapter_exit_nonzero")
        except WarmUnavailable:
            return outcome_type(reason_code="adapter_start_failed")
        code, out, err = done.exit_code, done.stdout, done.stderr
        reason = None
        if len(out) > limit:
            out, reason = out[:limit], "adapter_output_limit"
        if len(err) > min(limit, 16384):
            err, reason = err[:min(limit, 16384)], "adapter_output_limit"
        if reason is None and code != 0:
            reason = "adapter_exit_nonzero"
        return outcome_type(out, err, code, reason, done.pid)

    runner.invoke = invoke
    return "installed"


def _prepare(request: dict, state: dict) -> None:
    if state.get("ready"):
        return
    entry = request["entry"]
    # Same sys.path[0] that `python script.py` / `python -m module` would give.
    first = os.path.dirname(entry["target"]) if entry["kind"] == "script" else entry["cwd"]
    sys.path.insert(0, first)
    state["ready"] = True
    state["hook"] = _install_dispatcher_hook() if request.get("hook") == "dispatcher_adapters" else None


def _run(request: dict, state: dict) -> dict:
    entry = request["entry"]
    out, err = io.BytesIO(), io.BytesIO()
    new_in = io.TextIOWrapper(io.BytesIO(base64.b64decode(request["stdin"])), encoding="utf-8", errors="surrogateescape")
    new_out = io.TextIOWrapper(out, encoding="utf-8", errors="surrogateescape", write_through=True)
    new_err = io.TextIOWrapper(err, encoding="utf-8", errors="backslashreplace", write_through=True)
    saved = (sys.stdin, sys.stdout, sys.stderr, sys.argv, os.getcwd())
    exit_code = 0
    try:
        sys.stdin, sys.stdout, sys.stderr = new_in, new_out, new_err
        sys.argv = [entry["target"], *request["args"]]
        os.chdir(entry["cwd"])
        _prepare(request, state)
        if entry["kind"] == "script":
            runpy.run_path(entry["target"], run_name="__main__")
        else:
            runpy.run_module(entry["target"], run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            exit_code = 0
        elif isinstance(code, int):
            exit_code = code
        else:
            print(code, file=sys.stderr)
            exit_code = 1
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        exit_code = 1
    finally:
        sys.stdin, sys.stdout, sys.stderr, sys.argv = saved[:4]
        os.chdir(saved[4])
    new_out.flush()
    new_err.flush()
    response = {"exit": exit_code, "stdout": base64.b64encode(out.getvalue()).decode("ascii"),
                "stderr": base64.b64encode(err.getvalue()).decode("ascii"), "hook": state.get("hook")}
    for wrapper in (new_in, new_out, new_err):
        wrapper.detach()
    return response


def main() -> int:
    protocol_in = sys.stdin.buffer
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(2, 1)  # stray writes to fd 1 can no longer corrupt the protocol stream
    state: dict = {}
    while (request := read_frame(protocol_in)) is not None:
        write_frame(protocol_out, _run(request, state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
