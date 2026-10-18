from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from ssr_chat.cli import _verification_prompt
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

        with patch("ssr_chat.ollama_model.urlopen", return_value=fake_response) as mocked:
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
        with patch("ssr_chat.ollama_model.urlopen", return_value=fake_response) as mocked:
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
            "ssr_chat.ollama_model.urlopen", side_effect=URLError("private socket detail")
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
            "ssr_chat.ollama_model.urlopen",
            return_value=FakeResponse(b"not json"),
        ):
            with self.assertRaises(OllamaModelError) as invalid_json:
                client.list_models()
        self.assertEqual(invalid_json.exception.code, "invalid_json")

        with patch(
            "ssr_chat.ollama_model.urlopen", return_value=response({"message": {"content": " "}})
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
                "ssr_chat.ollama_model.urlopen", side_effect=URLError("socket details")
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


if __name__ == "__main__":
    unittest.main()
