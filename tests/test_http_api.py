from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
import sys

from ssr_chat.api import dispatch
from ssr_chat.application import build_runtime
from ssr_chat.catalog_knowledge import CatalogKnowledgeReader
from ssr_chat.ollama_model import OllamaModelClient


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
            self.assertEqual(health["configured_adapters"], {
                "knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run",
            })

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

    def test_health_reports_catalog_and_ollama_configuration_without_connecting(self) -> None:
        # Constructor-only configuration test; this is not a catalog or model health check.
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            root = Path(directory)
            (root / "ssr_knowledge").mkdir()
            (root / "ssr_knowledge/__main__.py").write_text("", encoding="utf-8")
            (root / "knowledge.sqlite3").write_bytes(b"synthetic placeholder; never queried")
            reader = CatalogKnowledgeReader(root, sys.executable, root)
            model = OllamaModelClient(endpoint="http://127.0.0.1:11435", model="private-model-name")
            runtime = build_runtime(root / "state", FIXTURES, knowledge_reader=reader, model=model)
            status, health = dispatch(runtime, "GET", "/health")
            self.assertEqual(status, 200)
            self.assertEqual(health["mode"], "local_adapters")
            self.assertEqual(health["configured_adapters"], {
                "knowledge": "catalog", "model": "ollama", "weather": "fixture", "output": "dry_run",
            })
            serialized = json.dumps(health)
            for private_value in (directory, "private-model-name", "11435"):
                self.assertNotIn(private_value, serialized)

    def test_health_labels_injected_unknown_adapters_as_custom(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            runtime = build_runtime(Path(directory), FIXTURES, model=object(), knowledge_reader=object())
            _, health = dispatch(runtime, "GET", "/health")
            self.assertEqual(health["mode"], "local_adapters")
            self.assertEqual(health["configured_adapters"]["model"], "custom")
            self.assertEqual(health["configured_adapters"]["knowledge"], "custom")
