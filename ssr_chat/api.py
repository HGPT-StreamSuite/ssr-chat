from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .runtime import ChatRuntime


def dispatch(runtime: ChatRuntime, method: str, path: str, payload: bytes = b"") -> tuple[int, dict[str, Any]]:
    """Handle one API request without opening a socket (suitable for local tests)."""
    if method == "GET" and path == "/health":
        return 200, {"status": "ok", "mode": "local_fixture"}
    if method != "POST" or path != "/chat":
        return 404, {"error": "not_found"}
    try:
        if not 1 <= len(payload) <= 1_000_000:
            raise ValueError("invalid_body_length")
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return 400, {"error": "invalid_json", "detail": str(exc)}
    return 200, runtime.handle(event)


def handler_for(runtime: ChatRuntime) -> type[BaseHTTPRequestHandler]:
    class ChatHandler(BaseHTTPRequestHandler):
        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            status, body = dispatch(runtime, "GET", self.path)
            self._respond(status, body)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            payload = self.rfile.read(length) if 0 <= length <= 1_000_000 else b""
            status, body = dispatch(runtime, "POST", self.path, payload)
            self._respond(status, body)

        def log_message(self, format: str, *args: Any) -> None:
            # Keep access logging quiet; run trace contains the useful local record.
            return

    return ChatHandler


def serve(runtime: ChatRuntime, port: int) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(runtime))
    try:
        print(f"Local API listening on http://127.0.0.1:{port}")
        server.serve_forever()
    finally:
        server.server_close()
