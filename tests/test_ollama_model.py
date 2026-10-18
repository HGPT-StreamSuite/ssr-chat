from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from ssr_chat.cli import _has_positive_verification_claim, _verification_prompt, main
from ssr_chat.application import build_runtime
from ssr_chat.models import AnswerPrompt
from ssr_chat.ollama_model import OllamaModelClient, OllamaModelError
from ssr_chat.stores import JsonlLog, RunTraceStore


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.closed = False

    def read(self, _size: int) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


@contextmanager
def local_http_server(*, payload=None, status: int = 200, redirect: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            self.server.request_paths.append((self.command, self.path))
            if self.server.redirect is not None:
                self.send_response(302)
                self.send_header("Location", self.server.redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(self.server.payload).encode("utf-8")
            self.send_response(self.server.response_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._respond()

        def do_POST(self) -> None:
            self._respond()

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.payload = {} if payload is None else payload
    server.response_status = status
    server.redirect = redirect
    server.request_paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def response(payload: object, status: int = 200) -> FakeResponse:
    return FakeResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"), status)


class OllamaModelClientTests(unittest.TestCase):
    def test_cli_verification_prompt_is_fixed_synthetic_evidence(self) -> None:
        prompt = _verification_prompt()

        self.assertEqual(prompt.context_snapshot_id, "verification-snapshot-001")
        self.assertEqual(prompt.analysis["intents"], ["game_item"])
        self.assertIn("測試對話", prompt.context_messages[0]["text"])
        self.assertIn("30 點能量", prompt.query_results[0]["materials"][0]["text"])
        self.assertEqual(
            prompt.query_results[0]["sources"][0]["source_id"], "fixture-item-wiki"
        )

    def test_generate_sends_snapshot_analysis_material_and_source(self) -> None:
        client = OllamaModelClient(model="test-model")
        fake_response = response({"message": {"content": "月光藥水回復 30 點能量。"}})
        prompt = AnswerPrompt(
            user_text="藥水回復多少能量？",
            context_snapshot_id="snapshot-test-1",
            context_messages=(
                {"message_id": "context-test-1", "author_id": "viewer", "text": "請接續剛才的測試問題。"},
            ),
            analysis={"intents": ["game_item"], "snapshot_id": "snapshot-test-1"},
            query_results=(
                {
                    "status": "ok",
                    "materials": [{"text": "月光藥水回復 30 點能量。"}],
                    "sources": [{"source_id": "fixture-item-wiki", "locator": "fixture://item-wiki"}],
                },
            ),
        )

        with patch("ssr_chat.ollama_model._open_local_request", return_value=fake_response) as mocked:
            answer = client.generate(prompt)

        request = mocked.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(answer, "月光藥水回復 30 點能量。")
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][1]["role"], "user")
        encoded_prompt = body["messages"][1]["content"]
        self.assertIn("snapshot-test-1", encoded_prompt)
        self.assertIn("剛才的測試問題", encoded_prompt)
        self.assertIn("月光藥水回復 30 點能量", encoded_prompt)
        self.assertIn("fixture-item-wiki", encoded_prompt)
        self.assertTrue(fake_response.closed)

    def test_list_models_only_reports_installed_models(self) -> None:
        client = OllamaModelClient()
        fake_response = response(
            {"models": [{"name": "small:latest", "size": 123, "modified_at": "now"}]}
        )
        with patch("ssr_chat.ollama_model._open_local_request", return_value=fake_response) as mocked:
            models = client.list_models()

        self.assertEqual(models[0]["name"], "small:latest")
        self.assertEqual(mocked.call_args.args[0].full_url, "http://127.0.0.1:11434/api/tags")
        self.assertEqual(mocked.call_args.kwargs["timeout"], 120.0)

    def test_rejects_non_loopback_endpoints_and_invalid_timeouts(self) -> None:
        for endpoint in (
            "https://127.0.0.1:11434",
            "http://example.com:11434",
            "http://127.0.0.1:11434/api",
            "http://user@127.0.0.1:11434",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                OllamaModelClient(endpoint=endpoint)
        for timeout in (0, -1, float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                OllamaModelClient(timeout=timeout)

    def test_unreachable_errors_are_safe_and_do_not_include_request_content(self) -> None:
        client = OllamaModelClient(model="test-model")
        with patch(
            "ssr_chat.ollama_model._open_local_request", side_effect=URLError("private socket detail")
        ):
            with self.assertRaises(OllamaModelError) as raised:
                client.generate(
                    AnswerPrompt("private question", "s", (), {}, ())
                )

        self.assertEqual(raised.exception.code, "endpoint_unreachable")
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(
            raised.exception.safe_diagnostic,
            {"code": "endpoint_unreachable", "detail": "Ollama endpoint is unavailable."},
        )

    def test_invalid_json_and_missing_answer_are_rejected(self) -> None:
        client = OllamaModelClient(model="test-model")
        with patch(
            "ssr_chat.ollama_model._open_local_request",
            return_value=FakeResponse(b"not json"),
        ):
            with self.assertRaises(OllamaModelError) as invalid_json:
                client.list_models()
        self.assertEqual(invalid_json.exception.code, "invalid_json")

        with patch(
            "ssr_chat.ollama_model._open_local_request", return_value=response({"message": {"content": " "}})
        ):
            with self.assertRaises(OllamaModelError) as missing_answer:
                client.generate(AnswerPrompt("q", "s", (), {}, ()))
        self.assertEqual(missing_answer.exception.code, "invalid_response")

    def test_runtime_records_safe_model_failure_and_holds_in_sop(self) -> None:
        test_temp = Path(__file__).resolve().parents[1] / ".test-temp"
        test_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp) as directory:
            state = Path(directory)
            event = json.loads((FIXTURES / "events" / "general.json").read_text(encoding="utf-8"))
            client = OllamaModelClient(model="test-model")

            with patch(
                "ssr_chat.ollama_model._open_local_request", side_effect=URLError("socket details")
            ):
                result = build_runtime(state, FIXTURES, model=client).handle(event)

            traces = RunTraceStore(state / "run_traces.jsonl").read_all()
            sop_events = JsonlLog(state / "sop_events.jsonl").read_all()
            self.assertEqual(result["reason"], "model_error")
            self.assertFalse(result["public_reply"])
            self.assertEqual(traces[0]["run_status"], "model_error")
            self.assertEqual(
                traces[0]["diagnostics"][-1],
                {
                    "stage": "model",
                    "code": "endpoint_unreachable",
                    "detail": "Ollama endpoint is unavailable.",
                },
            )
            self.assertEqual(sop_events[0]["reason"], "model_error")
            self.assertNotIn(event["text"], json.dumps(traces[0]["diagnostics"]))

    def test_verification_gate_accepts_natural_positive_claims(self) -> None:
        for answer in (
            "該道具可恢復30點能量。",
            "可以回復三十點能量。",
            "能量回復30點。",
            "不是40點，而是30點能量。",
            "不只是30點能量，還能恢復其他資源。",
            "It restores 30 points of energy.",
            "Energy is restored by 30 points.",
        ):
            with self.subTest(answer=answer):
                self.assertTrue(_has_positive_verification_claim(answer))

    def test_verification_gate_rejects_wrong_negated_and_300_claims(self) -> None:
        for answer in (
            "該道具可恢復300點能量。",
            "該道具只能恢復20點能量。",
            "該道具不能恢復30點能量。",
            "該道具不恢復30點能量。",
            "能量回復300點。",
            "能量回復300。",
            "回復130點能量。",
            "能量回復三百三十點。",
            "The item does not restore 30 points of energy.",
            "不是30點能量，而是300點能量。",
            "30 points of energy is incorrect.",
        ):
            with self.subTest(answer=answer):
                self.assertFalse(_has_positive_verification_claim(answer))

    def test_local_ollama_request_ignores_process_proxy_environment(self) -> None:
        with local_http_server(
            payload={"models": [{"name": "local:test", "size": 1}]}
        ) as ollama, local_http_server(
            payload={"error": "synthetic proxy should not be used"}, status=502
        ) as proxy:
            proxy_url = f"http://127.0.0.1:{proxy.server_port}"
            fake_proxy_environment = {
                "HTTP_PROXY": proxy_url,
                "http_proxy": proxy_url,
                "ALL_PROXY": proxy_url,
                "all_proxy": proxy_url,
                "NO_PROXY": "",
                "no_proxy": "",
            }
            with patch.dict(os.environ, fake_proxy_environment):
                client = OllamaModelClient(
                    endpoint=f"http://127.0.0.1:{ollama.server_port}"
                )
                models = client.list_models()

        self.assertEqual(models[0]["name"], "local:test")
        self.assertEqual(ollama.request_paths, [("GET", "/api/tags")])
        self.assertEqual(proxy.request_paths, [])

    def test_local_redirect_is_rejected_without_following_target(self) -> None:
        with local_http_server(payload={"models": []}) as redirected:
            target = f"http://127.0.0.1:{redirected.server_port}/redirect-target"
            with local_http_server(redirect=target) as source:
                client = OllamaModelClient(
                    endpoint=f"http://127.0.0.1:{source.server_port}"
                )
                with self.assertRaises(OllamaModelError) as raised:
                    client.list_models()

            self.assertEqual(source.request_paths, [("GET", "/api/tags")])
            self.assertEqual(redirected.request_paths, [])
        self.assertEqual(raised.exception.code, "http_error")
        self.assertIn("302", str(raised.exception))

    def test_cli_failure_records_trace_and_sop_and_returns_nonzero(self) -> None:
        test_temp = Path(__file__).resolve().parents[1] / ".test-temp"
        test_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp) as directory:
            state = Path(directory) / "state"
            output = StringIO()
            with local_http_server(
                payload={"error": "synthetic failure"}, status=503
            ) as server:
                with redirect_stdout(output):
                    exit_code = main(
                        [
                            "chat",
                            "--input",
                            str(FIXTURES / "events" / "general.json"),
                            "--state-dir",
                            str(state),
                            "--model-adapter",
                            "ollama",
                            "--ollama-endpoint",
                            f"http://127.0.0.1:{server.server_port}",
                            "--ollama-model",
                            "synthetic:test",
                        ]
                    )

            result = json.loads(output.getvalue())
            traces = RunTraceStore(state / "run_traces.jsonl").read_all()
            sop_events = JsonlLog(state / "sop_events.jsonl").read_all()

        self.assertEqual(exit_code, 1)
        self.assertEqual(server.request_paths, [("POST", "/api/chat")])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "model_error")
        self.assertFalse(result["public_reply"])
        self.assertEqual(result["send_result"]["status"], "not_attempted")
        self.assertFalse(result["send_result"]["attempted"])
        self.assertEqual(traces[0]["run_status"], "model_error")
        self.assertEqual(traces[0]["send_result"]["status"], "not_attempted")
        self.assertEqual(sop_events[0]["reason"], "model_error")

    def test_empty_model_answer_records_failure_trace_sop_and_skips_output(self) -> None:
        test_temp = Path(__file__).resolve().parents[1] / ".test-temp"
        test_temp.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp) as directory:
            state = Path(directory) / "state"
            output = StringIO()
            with local_http_server(payload={"message": {"content": ""}}) as server:
                with patch("ssr_chat.output.DryRunOutput.send") as output_send:
                    with redirect_stdout(output):
                        exit_code = main(
                            [
                                "chat",
                                "--input",
                                str(FIXTURES / "events" / "general.json"),
                                "--state-dir",
                                str(state),
                                "--model-adapter",
                                "ollama",
                                "--ollama-endpoint",
                                f"http://127.0.0.1:{server.server_port}",
                                "--ollama-model",
                                "synthetic:test",
                            ]
                        )

            result = json.loads(output.getvalue())
            traces = RunTraceStore(state / "run_traces.jsonl").read_all()
            sop_events = JsonlLog(state / "sop_events.jsonl").read_all()

        self.assertEqual(exit_code, 1)
        self.assertEqual(server.request_paths, [("POST", "/api/chat")])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "model_error")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["send_result"]["status"], "not_attempted")
        self.assertFalse(result["send_result"]["attempted"])
        self.assertFalse(result["public_reply"])
        output_send.assert_not_called()
        self.assertEqual(traces[0]["run_status"], "model_error")
        self.assertIsNone(traces[0]["answer"])
        self.assertEqual(traces[0]["send_result"]["status"], "not_attempted")
        self.assertEqual(
            traces[0]["diagnostics"][-1],
            {
                "stage": "model",
                "code": "invalid_response",
                "detail": "Ollama returned no answer text.",
            },
        )
        self.assertEqual(sop_events[0]["reason"], "model_error")


if __name__ == "__main__":
    unittest.main()
