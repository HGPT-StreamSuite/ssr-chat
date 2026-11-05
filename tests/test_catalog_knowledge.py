from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ssr_chat.application import build_runtime
from ssr_chat.catalog_knowledge import CatalogKnowledgeReader, CatalogReaderError, catalog_query_text
from ssr_chat.cli import main


def published_result():
    usage = {"ai_input": {"status": "allowed"}}
    return {"scope": "published", "query": "合成月光繩", "names_only": True,
            "status": "ok", "truncated": False, "total_matches": 1, "matches": [{
                "status": "published", "stale": False, "revision_id": 7,
                "match_type": "exact_name", "payload_sha256": "fixture-sha",
                "entry": {"entry_id": "fixture-rope", "name_zh": "合成月光繩",
                          "summary_zh": "虛構繩索形成藍色平台。", "limitations": "合成測試，持續時間未知。",
                          "editor": "PRIVATE_EDITOR", "citations": [{"snapshot_id": "s1", "locator": "fixture:1"}],
                          "knowledge_details": {"fields": {"duration": {"value": "unknown"}}, "gaps": {}}},
                "sources": [{"source_id": "fixture-source", "snapshot_id": "s1",
                             "source": {"url": "https://example.test/fixture", "title": "Fixture", "policy": {}},
                             "metadata": {"private": "PRIVATE_METADATA"},
                             "provenance": {"current_usage": copy.deepcopy(usage), "snapshot_usage": copy.deepcopy(usage)}}]}]}


class CatalogKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "ssr_knowledge").mkdir()
        (self.root / "ssr_knowledge/__main__.py").write_text("", encoding="utf-8")
        (self.root / "knowledge.sqlite3").write_bytes(b"fixture placeholder; never opened")
        self.reader = CatalogKnowledgeReader(self.root, sys.executable, self.root)

    def query(self, value):
        with patch.object(self.reader, "_search", return_value=value):
            return self.reader.query("game_item", "「合成月光繩」怎麼用？", ())

    def test_projects_published_material_with_unknowns_and_omits_private_metadata(self):
        result = self.query(published_result())
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.materials[0]["revision_id"], 7)
        self.assertEqual(result.materials[0]["knowledge_details"]["fields"]["duration"]["value"], "unknown")
        self.assertNotIn("PRIVATE_", json.dumps(result.public_dict()))
        self.assertEqual(result.sources[0]["snapshot_id"], "s1")

    def test_no_data_stays_no_data(self):
        value = published_result(); value.update(status="no_data", matches=[], total_matches=0)
        self.assertEqual(self.query(value).status, "no_data")

    def test_preview_pending_stale_and_inexact_matches_are_rejected(self):
        for field, invalid in (("scope", "review_preview"), ("names_only", False)):
            value = published_result(); value[field] = invalid
            with self.subTest(field=field), self.assertRaises(CatalogReaderError): self.query(value)
        for field, invalid in (("status", "pending_review"), ("status", "approved"), ("stale", True), ("match_type", "full_text")):
            value = published_result(); value["matches"][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(CatalogReaderError): self.query(value)

    def test_unknown_or_revoked_ai_permission_excludes_whole_mixed_entry(self):
        for scope in ("current_usage", "snapshot_usage"):
            for status in ("unknown", "prohibited", None):
                value = published_result()
                source = copy.deepcopy(value["matches"][0]["sources"][0])
                source["provenance"][scope]["ai_input"]["status"] = status
                value["matches"][0]["sources"].append(source)
                with self.subTest(scope=scope, status=status):
                    result = self.query(value)
                    self.assertEqual((result.status, result.diagnostic), ("tool_error", "catalog_ai_input_not_allowed"))
                    self.assertFalse(result.materials)

    def test_ambiguity_and_oversized_material_fail_without_partial_facts(self):
        value = published_result(); value["matches"] *= 2
        self.assertEqual(self.query(value).diagnostic, "catalog_ambiguous_name")
        value = published_result(); value["matches"][0]["entry"]["summary_zh"] = "x" * 17000
        self.assertEqual(self.query(value).diagnostic, "catalog_material_too_large")

    def test_name_selection_and_single_hop_followup(self):
        context = ({"text": "!ask 「合成月光繩」怎麼用？"},)
        self.assertEqual(catalog_query_text("「合成月光繩」怎麼用？", ()), "合成月光繩")
        self.assertEqual(catalog_query_text("它的持續時間呢？", context), "合成月光繩")
        self.assertEqual(catalog_query_text("新怪物呢？", context), "新怪物呢")
        self.assertEqual(catalog_query_text("它的用途？", ()), "它的用途")
        self.assertEqual(catalog_query_text("它的用途？", context + ({"text": "!ask 未知新物品"},)), "它的用途")
        with self.assertRaises(CatalogReaderError): catalog_query_text("「A」與「B」", ())

    def test_unquoted_effect_questions_select_one_explicit_name(self):
        for name in ("合成月光繩", "Fixture Moon Rope"):
            for suffix in ("是什麼效果？", "有什麼效果?", "的效果是什麼？"):
                with self.subTest(name=name, suffix=suffix):
                    self.assertEqual(catalog_query_text(name + " " + suffix, ()), name)
        self.assertEqual(catalog_query_text("「合成是什麼效果」是什麼效果？", ()), "合成是什麼效果")

    def test_effect_question_extraction_does_not_guess_or_reuse_an_old_subject(self):
        context = ({"text": "!ask 「舊合成道具」怎麼用？"},)
        for name in ("新合成道具", "那迦合成繩", "這件合成繩"):
            with self.subTest(name=name):
                self.assertEqual(catalog_query_text(name + "有什麼效果？", context), name)
        self.assertEqual(catalog_query_text("它有什麼效果？", context), "舊合成道具")
        for question in ("它是什麼效果？", "是什麼效果？", "這個有什麼效果？",
                         "合成甲與合成乙是什麼效果？", "合成甲、合成乙是什麼效果？",
                         "合成甲；合成乙是什麼效果？", "合成道具好用嗎？"):
            with self.subTest(question=question):
                self.assertEqual(catalog_query_text(question, ()), question.rstrip("？"))
        with self.assertRaises(CatalogReaderError):
            catalog_query_text("合" * 201 + "是什麼效果？", ())

    def test_runtime_full_effect_question_reaches_catalog_and_preserves_the_prompt(self):
        class Model:
            def generate(self, prompt):
                self.prompt = prompt
                return "合成回答"
        model = Model()
        runtime = build_runtime(self.root / "effect-state", knowledge_reader=self.reader, model=model)
        question = "!ask 合成月光繩是什麼效果？"
        with patch.object(self.reader, "_search", return_value=published_result()) as search:
            result = runtime.handle({"message_id": "effect-1", "conversation_id": "effect-room",
                                     "author_id": "synthetic", "source": "local-fixture", "text": question})
        search.assert_called_once_with("合成月光繩")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["analysis"]["intents"], ["game_item"])
        self.assertEqual(model.prompt.user_text, "合成月光繩是什麼效果？")
        self.assertEqual(model.prompt.analysis["query_text"], "合成月光繩是什麼效果？")
        self.assertEqual(model.prompt.query_results[0]["materials"][0]["entry_id"], "fixture-rope")
        self.assertEqual(result["send_result"]["status"], "dry_run_not_sent")
        self.assertFalse(result["send_result"]["public_reply"])

    def test_unquoted_effect_question_keeps_ai_input_permission_gate(self):
        value = published_result()
        value["matches"][0]["sources"][0]["provenance"]["snapshot_usage"]["ai_input"]["status"] = "unknown"
        with patch.object(self.reader, "_search", return_value=value) as search:
            result = self.reader.query("game_item", "合成月光繩是什麼效果？", ())
        search.assert_called_once_with("合成月光繩")
        self.assertEqual((result.status, result.diagnostic), ("tool_error", "catalog_ai_input_not_allowed"))
        self.assertFalse(result.materials)
        self.assertFalse(result.sources)

    def test_subprocess_uses_read_only_cli_safe_arguments_and_utf8(self):
        value = published_result()
        result = subprocess.CompletedProcess([], 0, json.dumps(value, ensure_ascii=False).encode("utf-8"), b"")
        with patch("ssr_chat.catalog_knowledge.subprocess.run", return_value=result) as run:
            self.assertEqual(self.reader._search("--preview"), value)
        args, kwargs = run.call_args
        self.assertIn("--text=--preview", args[0]); self.assertNotIn("--preview", args[0])
        self.assertIn("--names-only", args[0]); self.assertIn("-B", args[0])
        self.assertNotIn("shell", kwargs); self.assertEqual(kwargs["timeout"], 15.0)
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_transport_failure_and_invalid_output_have_bounded_diagnostics(self):
        cases = [subprocess.TimeoutExpired("private command", 1), OSError("private path")]
        for exc in cases:
            with patch("ssr_chat.catalog_knowledge.subprocess.run", side_effect=exc):
                with self.assertRaises(CatalogReaderError) as caught: self.reader._search("x")
                self.assertNotIn("private", str(caught.exception))
        for code, stdout in [(1, b"private stderr"), (0, b"not json"), (0, b"[]")]:
            with patch("ssr_chat.catalog_knowledge.subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout, b"private")):
                with self.assertRaises(CatalogReaderError): self.reader._search("x")

    def test_runtime_uses_injected_reader_and_preserves_tool_error_behavior(self):
        class Model:
            def generate(self, prompt):
                self.prompt = prompt
                return "合成回答"
        model = Model()
        runtime = build_runtime(self.root / "state", knowledge_reader=self.reader, model=model)
        value = published_result()
        with patch.object(self.reader, "_search", return_value=value):
            result = runtime.handle({"message_id": "synthetic-1", "conversation_id": "synthetic-room", "author_id": "synthetic",
                                     "source": "local-fixture", "text": "!ask 「合成月光繩」怎麼用？"})
        self.assertEqual(result["status"], "answered")
        self.assertEqual(model.prompt.query_results[0]["materials"][0]["entry_id"], "fixture-rope")
        with patch.object(self.reader, "_search", side_effect=CatalogReaderError("catalog_failed")):
            result = runtime.handle({"message_id": "synthetic-2", "conversation_id": "synthetic-other", "author_id": "synthetic",
                                     "source": "local-fixture", "text": "!ask 「合成月光繩」怎麼用？"})
        self.assertEqual(result["status"], "answered")
        self.assertEqual(model.prompt.query_results[0]["status"], "tool_error")
        self.assertFalse(model.prompt.query_results[0]["materials"])

    def test_cli_requires_explicit_existing_catalog_configuration(self):
        from contextlib import redirect_stderr
        from io import StringIO
        for args in (["--knowledge-adapter", "catalog"], ["--knowledge-data-dir", str(self.root)]):
            with redirect_stderr(StringIO()):
                self.assertEqual(main(["chat", "--input", "-", *args]), 2)
        for timeout in (0, float("inf"), float("nan")):
            with self.assertRaises(ValueError): CatalogKnowledgeReader(self.root, sys.executable, self.root, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
