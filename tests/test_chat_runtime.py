from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ssr_chat.analysis import QuestionAnalyzer
from ssr_chat.answer_context import AnswerContextBuilder
from ssr_chat.application import build_runtime
from ssr_chat.model import FixtureModelClient
from ssr_chat.stores import JsonlLog, RunTraceStore


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class CapturingAnalyzer(QuestionAnalyzer):
    pass


class CapturingAnswerContext(AnswerContextBuilder):
    pass


class CapturingModel(FixtureModelClient):
    def __init__(self) -> None:
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return super().generate(prompt)


class FailingWeatherAdapter:
    def fetch(self, question, context_messages):
        raise RuntimeError("fixture timeout diagnostic")


class ChatRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root = Path(__file__).resolve().parents[1] / ".test-temp"
        cls.temp_root.mkdir(exist_ok=True)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=self.temp_root)
        self.state = Path(self.temp.name)
        self.analyzer = CapturingAnalyzer()
        self.answer_context = CapturingAnswerContext()
        self.model = CapturingModel()
        self.runtime = build_runtime(
            self.state,
            FIXTURES,
            analyzer=self.analyzer,
            answer_context=self.answer_context,
            model=self.model,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(self, name: str) -> dict:
        with (FIXTURES / "events" / name).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_general_question_then_followup_uses_same_snapshot(self) -> None:
        first = self.runtime.handle(self.event("general.json"))
        second = self.runtime.handle(self.event("followup.json"))

        self.assertEqual(first["status"], "answered")
        self.assertIn("科幻策略遊戲", first["answer"])
        self.assertEqual(second["status"], "answered")
        self.assertIn("科幻策略遊戲", second["answer"])
        self.assertEqual(second["analysis"]["intents"], ["game_info"])
        self.assertEqual(
            second["context_snapshot_id"], second["analysis"]["snapshot_id"]
        )
        self.assertEqual(
            self.analyzer.snapshots_seen[-1],
            self.answer_context.snapshots_seen[-1],
        )
        self.assertIs(
            self.analyzer.snapshots_seen[-1],
            self.answer_context.snapshots_seen[-1],
        )
        self.assertEqual(
            self.analyzer.snapshots_seen[-1].messages[0]["message_id"],
            "demo-general-001",
        )

        raw_events = JsonlLog(self.state / "conversation_events.jsonl").read_all()
        traces = RunTraceStore(self.state / "run_traces.jsonl").read_all()
        self.assertEqual(len(raw_events), 2)
        self.assertEqual(len(traces), 2)
        self.assertNotIn("answer", raw_events[0])
        self.assertIn("answer", traces[0])
        self.assertIn("queries", traces[0])
        self.assertIn("send_result", traces[0])
        self.assertFalse(traces[0]["send_result"]["public_reply"])

    def test_person_and_guide_queries_read_local_fixtures(self) -> None:
        person = self.runtime.handle(self.event("person.json"))
        guide = self.runtime.handle(self.event("guide.json"))

        self.assertEqual(person["analysis"]["intents"], ["person"])
        self.assertEqual(person["queries"][0]["status"], "ok")
        self.assertEqual(set(person["queries"][0]), {"status", "materials", "sources"})
        self.assertIn("導航員角色", person["answer"])
        self.assertEqual(guide["analysis"]["intents"], ["game_guide"])
        self.assertIn("護盾解除", guide["answer"])

    def test_weather_and_item_are_separate_queries_with_sources(self) -> None:
        result = self.runtime.handle(self.event("multi-weather-item.json"))
        by_purpose = dict(zip(result["analysis"]["intents"], result["queries"]))

        self.assertEqual(set(by_purpose), {"weather", "game_item"})
        self.assertEqual(by_purpose["weather"]["status"], "ok")
        self.assertEqual(by_purpose["game_item"]["status"], "ok")
        self.assertIn("fixture://weather", by_purpose["weather"]["sources"][0]["locator"])
        self.assertIn("回復 30 點能量", result["answer"])
        self.assertIn("虛構 fixture", result["answer"])

    def test_invalid_untriggered_and_own_events_go_to_sop_without_reply(self) -> None:
        invalid = self.runtime.handle(self.event("invalid.json"))
        untriggered = self.runtime.handle(self.event("untriggered.json"))
        own = self.runtime.handle(self.event("self.json"))

        self.assertEqual(invalid["status"], "held")
        self.assertEqual(invalid["reason"], "source_not_allowed")
        self.assertEqual(untriggered["reason"], "not_triggered")
        self.assertEqual(own["reason"], "self_event_loop_guard")
        self.assertFalse(invalid["public_reply"])
        self.assertFalse(untriggered["public_reply"])
        self.assertFalse(own["public_reply"])
        self.assertEqual(len(JsonlLog(self.state / "sop_events.jsonl").read_all()), 3)
        self.assertEqual(len(JsonlLog(self.state / "conversation_events.jsonl").read_all()), 3)
        self.assertFalse((self.state / "run_traces.jsonl").exists())

    def test_missing_knowledge_records_gap_but_tool_error_does_not(self) -> None:
        event = {
            "message_id": "unknown-person-001",
            "conversation_id": "demo-room",
            "author_id": "viewer",
            "source": "local-fixture",
            "text": "!ask 介紹這位人物：失落旅人",
        }
        result = self.runtime.handle(event)
        self.assertEqual(result["queries"][0]["status"], "no_data")
        self.assertIn("知識缺口", result["answer"])
        gaps = JsonlLog(self.state / "knowledge_gaps.jsonl").read_all()
        self.assertGreaterEqual(len(gaps), 1)
        self.assertIn("person", {gap["purpose"] for gap in gaps})
        self.assertTrue(all(gap["reason"] == "knowledge_gap" for gap in gaps))
        self.assertTrue(all(gap["message_id"] == "unknown-person-001" for gap in gaps))

    def test_partial_tool_failure_is_separate_from_prompt_diagnostics(self) -> None:
        self.runtime = build_runtime(
            self.state / "partial",
            FIXTURES,
            weather_adapter=FailingWeatherAdapter(),
            model=self.model,
        )
        event = self.event("multi-weather-item.json")
        result = self.runtime.handle(event)
        by_purpose = dict(zip(result["analysis"]["intents"], result["queries"]))

        self.assertEqual(by_purpose["weather"]["status"], "tool_error")
        self.assertEqual(by_purpose["game_item"]["status"], "ok")
        self.assertIn("回復 30 點能量", result["answer"])
        self.assertNotIn("fixture timeout diagnostic", result["answer"])
        prompt_data = self.model.prompts[-1].as_dict()
        self.assertNotIn("fixture timeout diagnostic", json.dumps(prompt_data))
        trace = RunTraceStore(
            self.state / "partial" / "run_traces.jsonl"
        ).read_all()[0]
        self.assertIn("fixture timeout diagnostic", trace["diagnostics"][0]["detail"])
        self.assertFalse(
            (self.state / "partial" / "knowledge_gaps.jsonl").exists()
        )

    def test_missing_required_input_field_is_recorded_as_invalid(self) -> None:
        result = self.runtime.handle(
            {
                "message_id": "bad-input-001",
                "conversation_id": "room",
                "author_id": "viewer",
                "source": "local-fixture",
            }
        )
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["reason"], "missing_or_invalid_text")
        self.assertFalse(result["public_reply"])
