"""Pure orchestration checks: native stages are spies, never real processes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from ssr_chat.chat_history_binding import PROFILE
from ssr_chat.modular_retrieval import (
    CHAT_RECENT_ADAPTER, ModularRetrievalPipeline, PipelineConfig, PipelineError,
)


class ChatHistoryRoutingTests(unittest.TestCase):
    def setUp(self):
        # These paths only label spy calls; no component is read or executed.
        self.root = Path(__file__).resolve().parents[1]
        self.config = PipelineConfig(Path(sys.executable), self.root, self.root,
                                     self.root, self.root / "dispatcher.json")
        self.enabled = replace(self.config, chat_history_intent_adapter=PROFILE)
        self.request = {
            "synthetic": True,
            "request": {"schema_version": "1.0.1", "request_id": "routing-1", "text": "synthetic history"},
            "trusted_context": {"synthetic": True},
        }
        scope = {"domain": "chat_history", "platform": "synthetic-platform",
                 "channel_id": "synthetic-room", "game_id": None,
                 "session_id": "synthetic-session", "content_version": None}
        self.query = {
            "schema_version": "1.0.1", "request_id": "routing-1", "text": "synthetic history",
            "intent": "chat_history", "scope": scope, "actor_account_id": "synthetic-alice",
            "person_id": None, "time_from": "2026-10-01T00:00:00Z",
            "time_to": "2026-11-01T00:00:00Z", "exclude_event_ids": ["synthetic-current"],
            "limit": 7, "layer": "raw", "conditions": {"before_event_time": "2026-10-31T10:00:00Z"},
            "access_scope": {"read_scopes": [{"domain": "chat_history", **{
                key: {"mode": "exact", "value": value} for key, value in scope.items() if key != "domain"
            }}], "write_scopes": []},
        }
        self.plan = {"schema_version": "1.0.1", "request_id": "routing-1", "tasks": [{
            "task_id": "chat-1", "domain": "chat_history", "query": self.query, "depends_on": [],
        }], "skipped_domains": [], "missing_inputs": []}
        guard = patch("ssr_chat.modular_retrieval.subprocess.run",
                      side_effect=AssertionError("native subprocess forbidden in routing unit tests"))
        self.native = guard.start()
        self.addCleanup(guard.stop)

    def run_spies(self, plan=None, *, config=None, request=None, expected_domain="chat_history"):
        plan = self.plan if plan is None else plan
        pipeline = ModularRetrievalPipeline(self.enabled if config is None else config)
        result = {"domain": expected_domain, "status": "ok", "evidence": [{
            "source_id": "synthetic-chat", "record_id": "synthetic-record", "revision": 3,
            "source_refs": [{"source_id": "synthetic-chat", "record_id": "synthetic-record",
                             "revision": 3, "locator": "synthetic://routing/record", "transformation_version": None}],
        }]}
        dispatch = {"schema_version": "1.0.1", "request_id": "routing-1", "tasks": [
            {"task_id": task["task_id"], "result": deepcopy(result)} for task in plan["tasks"]
        ]}
        collection = {"contract_version": "1.0.1", "request_id": "routing-1", "status": "ok", "partial": False}
        replies = {"planner": plan, "dispatcher": dispatch, "collector": collection}

        def invoke(stage, repo, arguments, payload, exits, trace):
            trace.append({"stage": stage, "exit_code": 0})
            return replies[stage]

        with patch.object(pipeline, "_invoke", side_effect=invoke) as spy:
            report = pipeline.run(self.request if request is None else request, expected_domain=expected_domain)
        self.native.assert_not_called()
        return report, spy, dispatch

    def test_opt_in_changes_only_intent_and_preserves_question_only_three_states(self):
        for state in ("absent", False, True):
            with self.subTest(question_only=state):
                plan = deepcopy(self.plan)
                if state != "absent":
                    plan["tasks"][0]["query"]["conditions"]["question_only"] = state
                before, request_before = deepcopy(plan), deepcopy(self.request)
                report, spy, dispatch = self.run_spies(plan)
                expected = deepcopy(before)
                expected["tasks"][0]["query"]["intent"] = "history"
                self.assertEqual([call.args[0] for call in spy.call_args_list], ["planner", "dispatcher", "collector"])
                self.assertEqual(spy.call_args_list[1].args[3], expected)
                self.assertIs(spy.call_args_list[2].args[3]["query_plan"], plan)
                self.assertEqual(spy.call_args_list[2].args[3]["domain_results"]["chat-1"], dispatch["tasks"][0]["result"])
                self.assertEqual(plan, before)
                self.assertEqual(self.request, request_before)
                self.assertIs(report["plan"], plan)
                self.assertEqual(report["dispatch_plan"], expected)
                self.assertEqual(report["query_adaptations"], [{
                    "adapter": PROFILE, "task_id": "chat-1", "domain": "chat_history",
                    "schema_version": "1.0.1", "from_intent": "chat_history", "to_intent": "history",
                    "changed_json_pointers": ["/intent"],
                }])
                self.assertEqual(report["status"], "complete")
                self.assertEqual(report["answer_generation"], "not_run")
                self.assertEqual(report["public_output"], "not_attempted")

    def test_person_identity_is_preserved_without_inventing_actor(self):
        plan = deepcopy(self.plan)
        plan["tasks"][0]["query"].update(actor_account_id=None, person_id="synthetic-person")
        report, spy, _ = self.run_spies(plan)
        mapped = spy.call_args_list[1].args[3]["tasks"][0]["query"]
        self.assertIsNone(mapped["actor_account_id"])
        self.assertEqual(mapped["person_id"], "synthetic-person")
        self.assertIsNone(report["plan"]["tasks"][0]["query"]["actor_account_id"])

    def test_profile_is_off_by_default_and_does_not_require_synthetic_markers(self):
        request = deepcopy(self.request)
        request.pop("synthetic")
        request["trusted_context"].pop("synthetic")
        report, spy, _ = self.run_spies(config=self.config, request=request)
        self.assertIs(spy.call_args_list[1].args[3], self.plan)
        self.assertNotIn("dispatch_plan", report)
        self.assertNotIn("query_adaptations", report)
        self.assertIsNone(self.config.chat_history_intent_adapter)

    def test_inconsistent_or_nonboolean_markers_fail_before_any_stage(self):
        for where in ("top", "trusted"):
            for marker in ("missing", False, 1, "true"):
                request = deepcopy(self.request)
                target = request if where == "top" else request["trusted_context"]
                if marker == "missing":
                    target.pop("synthetic")
                else:
                    target["synthetic"] = marker
                pipeline = ModularRetrievalPipeline(self.enabled)
                with self.subTest(where=where, marker=marker), patch.object(pipeline, "_invoke") as spy:
                    with self.assertRaises(PipelineError) as caught:
                        pipeline.run(request)
                    self.assertEqual(caught.exception.code, "chat_history_profile_required")
                    spy.assert_not_called()

    def assert_binding_rejection(self, plan, code):
        pipeline = ModularRetrievalPipeline(self.enabled)
        before = deepcopy(plan)
        with patch.object(pipeline, "_invoke", return_value=plan) as spy:
            with self.assertRaises(PipelineError) as caught:
                pipeline.run(self.request)
        self.assertEqual((caught.exception.code, caught.exception.stage), (code, "binding"))
        self.assertEqual([call.args[0] for call in spy.call_args_list], ["planner"])
        self.assertEqual(plan, before)
        self.native.assert_not_called()

    def test_missing_or_invalid_identity_stops_before_dispatch(self):
        for actor, person, code in ((None, None, "query_identity_required"),
                                    (" ", None, "query_identity_invalid"),
                                    (None, False, "query_identity_invalid")):
            plan = deepcopy(self.plan)
            plan["tasks"][0]["query"].update(actor_account_id=actor, person_id=person)
            with self.subTest(actor=actor, person=person):
                self.assert_binding_rejection(plan, code)

    def test_unknown_query_version_and_unapproved_intents_are_binding_failures(self):
        for field, value, code in (("schema_version", "1.0", "query_version_unsupported"),
                                   ("intent", "recent_utterance", "query_intent_unsupported"),
                                   ("intent", "LT", "query_intent_unsupported"),
                                   ("intent", "question_history", "query_intent_unsupported")):
            plan = deepcopy(self.plan)
            plan["tasks"][0]["query"][field] = value
            with self.subTest(field=field, value=value):
                self.assert_binding_rejection(plan, code)

    def test_empty_or_mixed_domain_plans_are_not_partially_dispatched(self):
        empty = deepcopy(self.plan)
        empty["tasks"] = []
        self.assert_binding_rejection(empty, "chat_history_task_required")
        mixed = deepcopy(self.plan)
        other = deepcopy(mixed["tasks"][0])
        other.update(task_id="channel-1", domain="channel_knowledge")
        other["query"]["scope"]["domain"] = "channel_knowledge"
        mixed["tasks"].append(other)
        self.assert_binding_rejection(mixed, "task_domain_unsupported")

    def test_default_profile_leaves_channel_and_game_plans_unchanged(self):
        for domain in ("channel_knowledge", "game_knowledge"):
            plan = deepcopy(self.plan)
            plan["tasks"][0]["domain"] = domain
            plan["tasks"][0]["query"]["scope"]["domain"] = domain
            before = deepcopy(plan)
            with self.subTest(domain=domain):
                report, spy, _ = self.run_spies(plan, config=self.config, expected_domain=domain)
                self.assertIs(spy.call_args_list[1].args[3], plan)
                self.assertEqual(plan, before)
                self.assertNotIn("query_adaptations", report)

    def read_config(self, options):
        value = {"schema_version": "1.0.1", "python": str(Path(sys.executable)),
                 "planner_repo": ".", "dispatcher_repo": ".", "collector_repo": ".",
                 "dispatcher_config": "dispatcher.json", **options}
        dispatch = {"schema_version": "1.0.1", "adapters": {
            "chat_history": {"kind": "cli", "timeout_seconds": 5}}}
        paths = [Path(sys.executable), self.root, self.root, self.root, self.root / "dispatcher.json"]
        with patch.object(Path, "read_bytes", side_effect=[json.dumps(value).encode(), json.dumps(dispatch).encode()]), \
                patch.object(Path, "is_file", return_value=True), \
                patch("ssr_chat.modular_retrieval._local_path", side_effect=paths):
            return PipelineConfig.read(self.root / "operator.json")

    def test_config_read_accepts_explicit_profile_and_preserves_default(self):
        self.assertEqual(self.read_config({"chat_history_intent_adapter": PROFILE}).chat_history_intent_adapter, PROFILE)
        self.assertIsNone(self.read_config({}).chat_history_intent_adapter)

    def test_config_rejects_unknown_profile_before_component_checks(self):
        for value in ("automatic", True, [], {}):
            raw = {"schema_version": "1.0.1", "python": "missing", "planner_repo": ".",
                   "dispatcher_repo": ".", "collector_repo": ".", "dispatcher_config": "missing",
                   "chat_history_intent_adapter": value}
            with self.subTest(profile=value), patch.object(Path, "read_bytes", return_value=json.dumps(raw).encode()), \
                    patch("ssr_chat.modular_retrieval._local_path") as resolve:
                with self.assertRaises(PipelineError) as caught:
                    PipelineConfig.read(self.root / "operator.json")
                self.assertEqual(caught.exception.code, "chat_history_intent_adapter_invalid")
                resolve.assert_not_called()

    def test_recent_and_history_profiles_are_mutually_exclusive_before_any_stage(self):
        pipeline = ModularRetrievalPipeline(replace(self.enabled, chat_recent_intent_adapter=CHAT_RECENT_ADAPTER))
        with patch.object(pipeline, "_invoke") as spy:
            with self.assertRaises(PipelineError) as caught:
                pipeline.run(self.request)
            self.assertEqual(caught.exception.code, "chat_intent_profiles_mutually_exclusive")
            spy.assert_not_called()
        raw = {"schema_version": "1.0.1", "python": "missing", "planner_repo": ".",
               "dispatcher_repo": ".", "collector_repo": ".", "dispatcher_config": "missing",
               "chat_history_intent_adapter": PROFILE, "chat_recent_intent_adapter": CHAT_RECENT_ADAPTER}
        with patch.object(Path, "read_bytes", return_value=json.dumps(raw).encode()), \
                patch("ssr_chat.modular_retrieval._local_path") as resolve:
            with self.assertRaises(PipelineError) as caught:
                PipelineConfig.read(self.root / "operator.json")
            self.assertEqual(caught.exception.code, "chat_intent_profiles_mutually_exclusive")
            resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
