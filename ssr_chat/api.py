from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from typing import Any

from .runtime import ChatRuntime
from .modular_api import ModularRetrievalGateway, RetrievalInputError
from .modular_retrieval import PipelineError, load_object
from .api_lifecycle import FileLifecycleControl, RequestLifecycle


def dispatch(runtime: ChatRuntime, method: str, path: str, payload: bytes = b"", *,
             retrieval_gateway: ModularRetrievalGateway | None = None,
             retrieval_routes: dict[str, ModularRetrievalGateway] | None = None,
             answer_routes: dict[str, Any] | None = None,
             entry_gateway: Any = None, twitch_replay_gateway: Any = None, twitch_archive_gateway: Any = None,
             lifecycle: RequestLifecycle | None = None) -> tuple[int, dict[str, Any]]:
    """Handle one API request without opening a socket (suitable for local tests)."""
    routes = retrieval_routes or {}
    answers = answer_routes or {}
    if method == "GET" and path == "/health":
        profile = runtime.execution_profile
        fixture_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}
        body = {"status": "ok", "mode": "local_fixture" if profile == fixture_profile and retrieval_gateway is None and not routes and not answers and entry_gateway is None and twitch_replay_gateway is None and twitch_archive_gateway is None else "local_adapters",
                     "configured_adapters": profile,
                     "modular_retrieval": retrieval_gateway.profile if retrieval_gateway else {"configured": False},
                     "modular_retrieval_routes": {name: gateway.profile for name, gateway in routes.items()},
                     "modular_answer_routes": {name: gateway.profile for name, gateway in answers.items()},
                     "modular_entry": entry_gateway.profile if entry_gateway else {"configured": False},
                     "twitch_replay": twitch_replay_gateway.profile if twitch_replay_gateway else {"configured": False},
                     "twitch_archive": twitch_archive_gateway.profile if twitch_archive_gateway else {"configured": False}}
        if lifecycle is not None:
            body["lifecycle"] = lifecycle.snapshot()
            body["status"] = body["lifecycle"]["state"]
            body["ready"] = body["lifecycle"]["ready"]
            return (200 if body["ready"] else 503), body
        return 200, body
    is_retrieval = path == "/retrieve" or path.startswith("/retrieve/")
    is_answer = path == "/answer" or path.startswith("/answer/")
    is_entry = path == "/query"
    is_replay = path == "/twitch/replay"
    is_archive = path == "/twitch/archive"
    if method != "POST" or (path != "/chat" and not is_retrieval and not is_answer and not is_entry and not is_replay and not is_archive):
        return 404, {"error": "not_found"}
    selected_gateway = retrieval_gateway if path == "/retrieve" else routes.get(path)
    if is_archive:
        selected_gateway = twitch_archive_gateway
        if selected_gateway is None:
            return 503, {"status": "not_configured", "reason_code": "twitch_archive_not_configured"}
    if is_replay:
        selected_gateway = twitch_replay_gateway
        if selected_gateway is None:
            return 503, {"status": "not_configured", "reason_code": "twitch_replay_not_configured"}
    if is_entry:
        selected_gateway = entry_gateway
        if selected_gateway is None:
            return 503, {"status": "not_configured", "reason_code": "modular_entry_not_configured"}
    if is_answer:
        selected_gateway = answers.get(path)
        if selected_gateway is None:
            return 503, {"status": "not_configured", "reason_code": "modular_answer_not_configured"}
    if is_retrieval and selected_gateway is None:
        return 503, {"status": "not_configured", "reason_code": "modular_retrieval_not_configured"}
    try:
        if not 1 <= len(payload) <= 1_000_000:
            raise ValueError("invalid_body_length")
        event = load_object(payload) if is_retrieval or is_answer or is_entry or is_replay or is_archive else json.loads(payload.decode("utf-8"))
    except PipelineError as exc:
        return 400, {"status": "missing_input", "reason_code": exc.code}
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return 400, {"error": "invalid_json", "detail": str(exc)}
    if is_retrieval or is_answer or is_entry or is_replay or is_archive:
        try:
            if is_archive:
                return 200, selected_gateway.archive(event)
            if is_replay:
                return 200, selected_gateway.replay(event)
            if is_entry:
                return 200, selected_gateway.query(event)
            return 200, selected_gateway.answer(event) if is_answer else selected_gateway.retrieve(event)
        except RetrievalInputError as exc:
            return 400, {"status": "missing_input", "reason_code": str(exc)}
        except PipelineError as exc:
            return 502, {"status": "error", "stage": exc.stage, "reason_code": exc.code}
        except Exception:
            return 500, {"status": "error", "reason_code": "twitch_archive_failed" if is_archive else "twitch_replay_failed" if is_replay else "modular_entry_failed" if is_entry else "modular_answer_failed" if is_answer else "modular_retrieval_failed"}
    return 200, runtime.handle(event)


def handler_for(runtime: ChatRuntime, retrieval_gateway: ModularRetrievalGateway | None = None,
                retrieval_routes: dict[str, ModularRetrievalGateway] | None = None,
                answer_routes: dict[str, Any] | None = None,
                entry_gateway: Any = None, twitch_replay_gateway: Any = None, twitch_archive_gateway: Any = None,
                *, lifecycle: RequestLifecycle | None = None) -> type[BaseHTTPRequestHandler]:
    class ChatHandler(BaseHTTPRequestHandler):
        # Opt-in servers bound idle header/body reads so graceful close can settle.
        timeout = 30 if lifecycle is not None else None

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            status, body = dispatch(runtime, "GET", self.path, retrieval_gateway=retrieval_gateway, retrieval_routes=retrieval_routes, answer_routes=answer_routes, entry_gateway=entry_gateway, twitch_replay_gateway=twitch_replay_gateway, twitch_archive_gateway=twitch_archive_gateway, lifecycle=lifecycle)
            self._respond(status, body)

        def do_POST(self) -> None:
            lease = lifecycle.admit() if lifecycle is not None else None
            if lifecycle is not None and lease is None:
                # Never consume or execute work after drain. Close this connection
                # rather than leave an unread request body available for reuse.
                self.close_connection = True
                self._respond(503, {"status": "draining", "reason_code": "api_draining"})
                return
            status = 500
            failed = False
            try:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                payload = self.rfile.read(length) if 0 <= length <= 1_000_000 else b""
                status, body = dispatch(runtime, "POST", self.path, payload, retrieval_gateway=retrieval_gateway, retrieval_routes=retrieval_routes, answer_routes=answer_routes, entry_gateway=entry_gateway, twitch_replay_gateway=twitch_replay_gateway, twitch_archive_gateway=twitch_archive_gateway)
                self._respond(status, body)
            except BaseException:
                failed = True
                raise
            finally:
                if lifecycle is not None:
                    lifecycle.complete(lease, error=failed or status >= 500)

        def log_message(self, format: str, *args: Any) -> None:
            # Keep access logging quiet; run trace contains the useful local record.
            return

    return ChatHandler


class LifecycleHTTPServer(ThreadingHTTPServer):
    """Poll local controls in the accept loop; stop waits for admitted work."""

    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, lifecycle: RequestLifecycle, control: FileLifecycleControl):
        if address[0] != "127.0.0.1":
            raise ValueError("lifecycle_server_requires_loopback")
        self.lifecycle = lifecycle
        self.control = control
        self._shutdown_started = False
        self.control_failed = False
        super().__init__(address, handler)

    def service_actions(self) -> None:
        super().service_actions()
        try:
            self.control.poll()
        except (OSError, ValueError, RuntimeError):
            # Loss of the immutable owner/status binding closes admission. Never
            # try to adopt another directory or terminate a different process.
            self.control_failed = True
            self.lifecycle.request_stop()
        if self.lifecycle.stop_ready and not self._shutdown_started:
            self._shutdown_started = True
            # shutdown must run outside the serve_forever thread.
            threading.Thread(target=self.shutdown, daemon=True).start()


def serve(runtime: ChatRuntime, port: int, *, retrieval_gateway: ModularRetrievalGateway | None = None,
          retrieval_routes: dict[str, ModularRetrievalGateway] | None = None,
          answer_routes: dict[str, Any] | None = None,
          entry_gateway: Any = None, twitch_replay_gateway: Any = None, twitch_archive_gateway: Any = None,
          lifecycle_dir: str | Path | None = None) -> None:
    lifecycle = RequestLifecycle() if lifecycle_dir is not None else None
    control = FileLifecycleControl(lifecycle, lifecycle_dir, port) if lifecycle is not None else None
    handler = handler_for(runtime, retrieval_gateway, retrieval_routes, answer_routes, entry_gateway,
                          twitch_replay_gateway, twitch_archive_gateway, lifecycle=lifecycle)
    server = None
    try:
        if lifecycle is None:
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        else:
            server = LifecycleHTTPServer(("127.0.0.1", port), handler, lifecycle, control)
        print(f"Local API listening on http://127.0.0.1:{port}")
        if lifecycle is None:
            server.serve_forever()
        else:
            server.serve_forever(poll_interval=0.1)
    finally:
        if lifecycle is not None:
            lifecycle.drain()
        if server is not None:
            server.server_close()
        if lifecycle is not None:
            lifecycle.mark_stopped()
            if server is not None and server.control_failed:
                raise RuntimeError("lifecycle_control_unavailable")
            control.publish()
