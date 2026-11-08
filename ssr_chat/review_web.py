"""Local synthetic review UI: fixed native CLI tools and optional legacy Fixture events."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
from uuid import uuid4

from .application import build_runtime
from .model import FixtureModelClient
from .modular_retrieval import PipelineError, load_object
from .stores import JsonlLog
from .review_native import INTERFACE_VERSION, NativeReview, TOOLS, native_examples, validate_request

REPO = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).with_name("review_assets")
STANDARD = REPO / "docs" / "INDEPENDENT_ACCEPTANCE.md"
EVIDENCE = REPO / "docs" / "evidence" / "review-web-acceptance.txt"
MAX_BODY = 16_384
EVENT_FIELDS = {"message_id", "conversation_id", "author_id", "source", "text", "is_self", "created_at"}


class ReviewError(ValueError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


class RecordingFixtureModel(FixtureModelClient):
    def __init__(self):
        self.last_prompt = None
        self.calls = 0

    def generate(self, prompt):
        self.last_prompt = asdict(prompt)
        self.calls += 1
        return super().generate(prompt)


def source_version():
    paths = [p for folder in ("ssr_chat", "tests", "fixtures", "contracts") for p in (REPO / folder).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".json", ".jsonl", ".html", ".css", ".js"}]
    paths += [REPO / "pyproject.toml", REPO / "uv.lock", REPO / "README.md", STANDARD]
    manifest = {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        head = subprocess.run(["git", "--no-optional-locks", "rev-parse", "HEAD"], cwd=REPO,
                              capture_output=True, text=True, timeout=5, check=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        dirty = bool(subprocess.run(["git", "--no-optional-locks", "status", "--porcelain"], cwd=REPO,
                                   capture_output=True, text=True, timeout=5, check=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout)
    except (OSError, subprocess.SubprocessError):
        head, dirty = "unknown", None
    return {"head": head, "dirty": dirty, "fingerprint": fingerprint}


def criteria():
    rows = []
    for line in STANDARD.read_text(encoding="utf-8").splitlines():
        if line.startswith("| WEB"):
            parts = [value.strip().replace("`", "") for value in line.strip("|").split("|")]
            rows.append({"id": parts[0], "input": parts[1], "expected": parts[2]})
    return rows


def examples():
    base = {"message_id": "review-example", "conversation_id": "review-room", "author_id": "synthetic-viewer",
            "source": "local-fixture", "text": "!ask 星界遠征是什麼遊戲？"}
    cases = [("normal", "正常提問", {}), ("followup", "同聊天室追問", {"text": "!ask 那款遊戲還有哪些特色？"}),
             ("untriggered", "未觸發訊息", {"text": "這是合成的一般聊天。"}),
             ("self", "自身訊息", {"is_self": True}), ("missing", "缺少訊息 ID", {}),
             ("other-room", "另一聊天室", {"conversation_id": "review-other-room", "text": "!ask 那款遊戲還有哪些特色？"})]
    result = []
    for key, label, update in cases:
        event = {**base, **update}
        if key == "missing":
            event.pop("message_id")
        result.append({"id": key, "label": label, "event": event})
    return result


class ReviewWorkspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.token = secrets.token_urlsafe(32)
        self.version = source_version()
        self.lock = threading.RLock()
        self.generations = 0
        self.reset()

    def reset(self):
        with self.lock:
            if self.generations >= 20:
                raise ReviewError("workspace_limit", 429)
            self.workspace_id = uuid4().hex
            self.state = self.root / self.workspace_id
            self.state.mkdir()
            self.model = RecordingFixtureModel()
            self.runtime = build_runtime(self.state, model=self.model)
            self.receipts = {}
            self.native_receipts = {}
            self.native = None
            self.generations += 1
            return {"workspace_id": self.workspace_id, "status": "new_synthetic_workspace"}

    def meta(self):
        with self.lock:
            return {"repo": "ssr-chat", "version": self.version, "workspace_id": self.workspace_id,
                    "csrf_token": self.token, "runtime": "ChatRuntime", "model": "fixture",
                    "knowledge": "fixture", "weather": "fixture", "output": "dry_run",
                    "default_flow": "native",
                    "native": {"interface_version": INTERFACE_VERSION, "tools": list(TOOLS),
                               "data": "registered_synthetic_snapshots", "generation": "disabled"},
                    "external_connections": False}

    def run_native(self, body):
        if not isinstance(body, dict) or set(body) != {"synthetic", "workspace_id", "request_id", "request"}:
            raise ReviewError("only_synthetic_native_envelope_allowed")
        if body["synthetic"] is not True:
            raise ReviewError("synthetic_confirmation_required")
        if not isinstance(body["request_id"], str) or not re.fullmatch(r"review-[a-zA-Z0-9-]{1,80}", body["request_id"]):
            raise ReviewError("request_id_invalid")
        try:
            validate_request(body["request"])
        except ValueError as error:
            raise ReviewError(str(error)) from None
        with self.lock:
            if body["workspace_id"] != self.workspace_id:
                raise ReviewError("workspace_changed", 409)
            fingerprint = hashlib.sha256(json.dumps(body["request"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if body["request_id"] in self.receipts:
                raise ReviewError("request_id_conflict", 409)
            old = self.native_receipts.get(body["request_id"])
            if old:
                if old[0] != fingerprint:
                    raise ReviewError("request_id_conflict", 409)
                return {**deepcopy(old[1]), "replayed": True}
            if len(self.receipts) + len(self.native_receipts) >= 100:
                raise ReviewError("operation_limit", 429)
            if self.native is None:
                try:
                    self.native = NativeReview(self.state / "native")
                except (OSError, ValueError, KeyError):
                    raise ReviewError("native_registration_unavailable", 503) from None
            response = {**self.native.run(body["request_id"], body["request"]),
                        "interface_version": INTERFACE_VERSION,
                        "workspace_id": self.workspace_id, "replayed": False}
            self.native_receipts[body["request_id"]] = (fingerprint, deepcopy(response))
            return response

    def run(self, body):
        if not isinstance(body, dict) or set(body) != {"synthetic", "workspace_id", "request_id", "event"}:
            raise ReviewError("only_synthetic_event_envelope_allowed")
        if body["synthetic"] is not True:
            raise ReviewError("synthetic_confirmation_required")
        if not isinstance(body["request_id"], str) or not re.fullmatch(r"review-[a-zA-Z0-9-]{1,80}", body["request_id"]):
            raise ReviewError("request_id_invalid")
        event = body["event"]
        if not isinstance(event, dict) or set(event) - EVENT_FIELDS:
            raise ReviewError("event_fields_invalid")
        if event.get("source") != "local-fixture":
            raise ReviewError("synthetic_source_required")
        if "conversation_id" in event and (not isinstance(event["conversation_id"], str)
                                           or event["conversation_id"] not in {"review-room", "review-other-room"}):
            raise ReviewError("synthetic_conversation_required")
        for key, pattern in (("message_id", r"review-[a-zA-Z0-9-]{1,80}"), ("author_id", r"synthetic-[a-zA-Z0-9-]{1,40}")):
            if key in event and (not isinstance(event[key], str) or re.fullmatch(pattern, event[key]) is None):
                raise ReviewError("synthetic_" + key + "_required")
        for key in ("text", "created_at"):
            if key in event and (not isinstance(event[key], str) or len(event[key]) > (2_000 if key == "text" else 64)):
                raise ReviewError(key + "_invalid")
        if "is_self" in event and type(event["is_self"]) is not bool:
            raise ReviewError("is_self_invalid")
        with self.lock:
            if body["workspace_id"] != self.workspace_id:
                raise ReviewError("workspace_changed", 409)
            payload_hash = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            previous = self.receipts.get(body["request_id"])
            if body["request_id"] in self.native_receipts:
                raise ReviewError("request_id_conflict", 409)
            if previous:
                if previous[0] != payload_hash:
                    raise ReviewError("request_id_conflict", 409)
                return {**deepcopy(previous[1]), "replayed": True}
            if len(self.receipts) + len(self.native_receipts) >= 100:
                raise ReviewError("operation_limit", 429)
            self.model.last_prompt = None
            result = self.runtime.handle(deepcopy(event))
            run_id, sop_id = result.get("run_id"), result.get("sop_id")
            trace = [item for item in self.runtime.run_traces.read_all() if run_id and item.get("run_id") == run_id]
            for item in trace:
                item["diagnostics"] = [{k: v for k, v in diagnostic.items() if k != "detail"} for diagnostic in item.get("diagnostics", [])]
            sop = [item for item in JsonlLog(self.state / "sop_events.jsonl").read_all() if sop_id and item.get("sop_id") == sop_id]
            response = {"operation_id": body["request_id"], "workspace_id": self.workspace_id, "replayed": False,
                        "execution_mode": "executed_fixture_runtime", "result": result,
                        "evidence": {"input": deepcopy(event), "prompt": deepcopy(self.model.last_prompt), "trace": trace, "sop": sop},
                        "adapters": self.runtime.execution_profile}
            self.receipts[body["request_id"]] = (payload_hash, deepcopy(response))
            return response


def handler_for(app: ReviewWorkspace):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def _host_allowed(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def _send(self, status, body, mime="application/json; charset=utf-8"):
            raw = json.dumps(body, ensure_ascii=False).encode() if mime.startswith("application/json") else body
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self._host_allowed():
                return self._send(403, {"status": "error", "reason_code": "local_host_required"})
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/styles.css": ("styles.css", "text/css; charset=utf-8")}
            if self.path in assets:
                name, mime = assets[self.path]
                return self._send(200, (ASSETS / name).read_bytes(), mime)
            if self.path == "/api/meta":
                return self._send(200, app.meta())
            if self.path == "/api/examples":
                return self._send(200, examples())
            if self.path == "/api/native/examples":
                return self._send(200, native_examples())
            if self.path == "/api/criteria":
                standard = STANDARD.read_text(encoding="utf-8").replace(str(REPO.parents[1]), "<StreamSuite>")
                return self._send(200, {"criteria": criteria(), "standard": standard})
            if self.path == "/api/evidence":
                return self._send(200, {"current_review": EVIDENCE.read_text(encoding="utf-8") if EVIDENCE.exists() else None,
                                       "historical": "改動前歷史紀錄：493 Python 與 11 PowerShell 通過；不代表本次網頁已驗證。",
                                       "g01": {"status": "blocked", "mode": "read_only_record", "input_intent": "chat_history",
                                               "native_result": "missing_input / unsupported_intent", "native_head": "032102704ba39075f79ac82e8984ec4c9444a6ef",
                                               "note": "未接通 native 執行；一般映射契約與 owner 待定，既有 recent_utterance 轉接保持原狀。"}})
            return self._send(404, {"status": "error", "reason_code": "route_not_found"})

        def do_POST(self):
            origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            try:
                token_valid = secrets.compare_digest(self.headers.get("X-SSR-Review-Token", ""), app.token)
            except TypeError:
                token_valid = False
            if (not self._host_allowed() or self.headers.get("Origin") not in origins
                    or not token_valid):
                return self._send(403, {"status": "error", "reason_code": "local_page_token_required"})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._send(415, {"status": "error", "reason_code": "json_required"})
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ReviewError("content_length_invalid") from None
                if not 0 < length <= MAX_BODY:
                    raise ReviewError("body_size_invalid", 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ReviewError("incomplete_body")
                body = load_object(raw)
                if self.path == "/api/run":
                    result = app.run(body)
                elif self.path == "/api/native/run":
                    result = app.run_native(body)
                elif self.path == "/api/reset":
                    with app.lock:
                        if set(body) != {"workspace_id"} or body["workspace_id"] != app.workspace_id:
                            raise ReviewError("workspace_changed", 409)
                        result = app.reset()
                else:
                    raise ReviewError("route_not_found", 404)
                return self._send(200, result)
            except ReviewError as error:
                return self._send(error.status, {"status": "error", "reason_code": error.code})
            except PipelineError as error:
                return self._send(400, {"status": "error", "reason_code": error.code})
            except Exception:
                return self._send(500, {"status": "error", "reason_code": "review_operation_failed"})

    return Handler


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8826)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    parent = (REPO / ".test-temp" / "review-web").resolve()
    if not parent.is_relative_to(REPO):
        raise RuntimeError("test workspace outside checkout")
    parent.mkdir(parents=True, exist_ok=True)
    owned = tempfile.TemporaryDirectory(prefix="session-", dir=parent)
    try:
        app = ReviewWorkspace(Path(owned.name))
        try:
            server = ReviewServer(("127.0.0.1", args.port), handler_for(app))
        except OSError as error:
            print(json.dumps({"status": "not_started", "reason_code": "port_bind_failed", "port": args.port}), flush=True)
            return 2
        print(f"SSR chat review: http://127.0.0.1:{server.server_port} (native synthetic / prepare-only / optional Fixture)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if Path(owned.name).resolve().parent != parent:
            raise RuntimeError("refusing cleanup outside owned test parent")
        owned.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
