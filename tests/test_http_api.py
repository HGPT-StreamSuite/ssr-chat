from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ssr_chat.api import dispatch
from ssr_chat.application import build_runtime


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ChatApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root = Path(__file__).resolve().parents[1] / ".test-temp"
        cls.temp_root.mkdir(exist_ok=True)

    def test_health_chat_and_invalid_request_routes_use_local_fixture(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            runtime = build_runtime(Path(directory), FIXTURES)
            health_status, health = dispatch(runtime, "GET", "/health")
            self.assertEqual(health_status, 200)
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["mode"], "local_fixture")

            event = json.loads((FIXTURES / "events" / "general.json").read_text(encoding="utf-8"))
            status, result = dispatch(runtime, "POST", "/chat", json.dumps(event).encode("utf-8"))
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "answered")
            self.assertFalse(result["send_result"]["public_reply"])

            status, error = dispatch(runtime, "POST", "/chat", b"{invalid")
            self.assertEqual(status, 400)
            self.assertEqual(error["error"], "invalid_json")

            status, missing = dispatch(runtime, "GET", "/missing")
            self.assertEqual(status, 404)
            self.assertEqual(missing["error"], "not_found")
