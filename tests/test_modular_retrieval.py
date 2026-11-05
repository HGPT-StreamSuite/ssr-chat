from __future__ import annotations

import copy
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ssr_chat.cli import main
from ssr_chat.modular_retrieval import CHAT_RECENT_ADAPTER, ModularRetrievalPipeline, PipelineConfig, PipelineError, load_object


class ModularRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = PipelineConfig(Path(sys.executable), self.root, self.root, self.root,
                                     self.root / "dispatcher.json")
        self.pipeline = ModularRetrievalPipeline(self.config)
        self.request = {"request": {"schema_version": "1.0.1", "request_id": "synthetic-q", "text": "synthetic"},
                        "trusted_context": {"synthetic": True}}
        self.plan = {"schema_version": "1.0.1", "request_id": "synthetic-q",
                     "tasks": [{"task_id": "game-1"}, {"task_id": "game-2"}], "missing_inputs": [], "skipped_domains": []}
        self.first = {"status": "ok", "evidence": [{"record_id": "synthetic-record-1"}]}
        self.second = {"status": "denied", "evidence": [], "reason_code": "synthetic-denial"}
        self.dispatched = {"schema_version": "1.0.1", "request_id": "synthetic-q", "tasks": [
            {"task_id": "game-2", "result": self.second}, {"task_id": "game-1", "result": self.first}]}
        self.collected = {"contract_version": "1.0.1", "request_id": "synthetic-q", "status": "ok", "partial": True}

    def test_join_uses_task_identity_and_retains_failed_branch_and_original_plan(self):
        original = copy.deepcopy(self.request)
        with patch.object(self.pipeline, "_invoke", side_effect=[self.plan, self.dispatched, self.collected]) as invoke:
            report = self.pipeline.run(self.request)
        collector_input = invoke.call_args_list[2].args[3]
        self.assertIs(collector_input["query_plan"], self.plan)
        self.assertEqual(collector_input["domain_results"], {"game-1": self.first, "game-2": self.second})
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["answer_generation"], "not_run")
        self.assertEqual(report["public_output"], "not_attempted")
        self.assertEqual(self.request, original)

    def test_complete_collection_is_distinct_from_partial(self):
        self.collected["partial"] = False
        with patch.object(self.pipeline, "_invoke", side_effect=[self.plan, self.dispatched, self.collected]):
            self.assertEqual(self.pipeline.run(self.request)["status"], "complete")

    def test_collector_envelope_uses_its_own_contract_version_field(self):
        invalid = {**self.collected, "schema_version": "1.0.1"}
        del invalid["contract_version"]
        with patch.object(self.pipeline, "_invoke", side_effect=[self.plan, self.dispatched, invalid]):
            with self.assertRaisesRegex(PipelineError, "collector_identity_mismatch"):
                self.pipeline.run(self.request)

    def recent_chat_plan(self):
        plan = copy.deepcopy(self.plan)
        plan["tasks"][0].update(domain="chat_history", query={
            "schema_version": "1.0.1", "intent": "recent_utterance", "scope": {"domain": "chat_history"},
            "actor_account_id": "synthetic-alice", "exclude_event_ids": ["current"],
            "conditions": {"before_event_time": "2026-10-30T10:20:00Z"},
            "access_scope": {"read_scopes": [], "write_scopes": []}})
        return plan

    def test_opt_in_chat_adapter_changes_only_intent_and_preserves_original_plan(self):
        plan = self.recent_chat_plan()
        original = copy.deepcopy(plan)
        pipeline = ModularRetrievalPipeline(replace(self.config, chat_recent_intent_adapter=CHAT_RECENT_ADAPTER))
        with patch.object(pipeline, "_invoke", side_effect=[plan, self.dispatched, self.collected]) as invoke:
            report = pipeline.run(self.request)
        expected = copy.deepcopy(original)
        expected["tasks"][0]["query"]["intent"] = "last_said"
        self.assertEqual(invoke.call_args_list[1].args[3], expected)
        self.assertEqual(invoke.call_args_list[2].args[3]["query_plan"], original)
        self.assertEqual(plan, original)
        self.assertEqual(report["plan"], original)
        self.assertEqual(report["dispatch_plan"], expected)
        self.assertEqual(report["query_adaptations"], [{"adapter": CHAT_RECENT_ADAPTER, "task_id": "game-1",
            "domain": "chat_history", "schema_version": "1.0.1", "from_intent": "recent_utterance", "to_intent": "last_said"}])
        self.assertEqual(report["status"], "incomplete")

    def test_chat_adapter_is_off_by_default(self):
        plan = self.recent_chat_plan()
        with patch.object(self.pipeline, "_invoke", side_effect=[plan, self.dispatched, self.collected]) as invoke:
            report = self.pipeline.run(self.request)
        self.assertIs(invoke.call_args_list[1].args[3], plan)
        self.assertNotIn("query_adaptations", report)

    def test_chat_adapter_does_not_rewrite_other_domains_or_intents(self):
        for domain, intent in (("stt_transcript", "recent_utterance"), ("chat_history", "question_history")):
            plan = self.recent_chat_plan()
            plan["tasks"][0]["domain"] = domain
            plan["tasks"][0]["query"]["intent"] = intent
            original = copy.deepcopy(plan)
            pipeline = ModularRetrievalPipeline(replace(self.config, chat_recent_intent_adapter=CHAT_RECENT_ADAPTER))
            with self.subTest(domain=domain, intent=intent), patch.object(pipeline, "_invoke", side_effect=[plan, self.dispatched, self.collected]) as invoke:
                report = pipeline.run(self.request)
            self.assertEqual(invoke.call_args_list[1].args[3], original)
            self.assertEqual(plan, original)
            self.assertNotIn("query_adaptations", report)

    def test_chat_adapter_rejects_mismatched_domain_and_version_before_dispatch(self):
        for replacement in (None, {"schema_version": "1.0.0", "intent": "recent_utterance", "scope": {"domain": "chat_history"}},
                            {"schema_version": "1.0.1", "intent": "recent_utterance", "scope": {"domain": "game_knowledge"}}):
            plan = self.recent_chat_plan()
            plan["tasks"][0]["query"] = replacement
            pipeline = ModularRetrievalPipeline(replace(self.config, chat_recent_intent_adapter=CHAT_RECENT_ADAPTER))
            with self.subTest(query=replacement), patch.object(pipeline, "_invoke", return_value=plan) as invoke:
                with self.assertRaisesRegex(PipelineError, "chat_recent_adapter_query_invalid"):
                    pipeline.run(self.request)
                self.assertEqual(invoke.call_count, 1)

    def test_configuration_rejects_unknown_chat_adapter_before_component_checks(self):
        config = {"schema_version": "1.0.1", "python": "missing", "planner_repo": ".", "dispatcher_repo": ".",
                  "collector_repo": ".", "dispatcher_config": "missing"}
        path = self.root / "pipeline.json"
        for adapter in ("automatic", True, [], {}):
            path.write_text(json.dumps({**config, "chat_recent_intent_adapter": adapter}), encoding="utf-8")
            with self.subTest(adapter=adapter), self.assertRaisesRegex(PipelineError, "chat_recent_intent_adapter_invalid"):
                PipelineConfig.read(path)

    def test_duplicate_plan_task_ids_do_not_run_dispatcher(self):
        self.plan["tasks"][1]["task_id"] = "game-1"
        with patch.object(self.pipeline, "_invoke", return_value=self.plan) as invoke:
            with self.assertRaisesRegex(PipelineError, "plan_task_identity_invalid"):
                self.pipeline.run(self.request)
        self.assertEqual(invoke.call_count, 1)

    def test_missing_duplicate_unknown_and_nonstring_result_ids_do_not_run_collector(self):
        cases = [self.dispatched["tasks"][:1], self.dispatched["tasks"][:1] * 2,
                 [{"task_id": "unknown", "result": self.first}], [{"task_id": [], "result": self.first}]]
        for tasks in cases:
            dispatched = {**self.dispatched, "tasks": tasks}
            with self.subTest(tasks=tasks), patch.object(self.pipeline, "_invoke", side_effect=[self.plan, dispatched]) as invoke:
                with self.assertRaises(PipelineError): self.pipeline.run(self.request)
                self.assertEqual(invoke.call_count, 2)

    def test_request_and_child_identities_are_checked(self):
        with patch.object(self.pipeline, "_invoke") as invoke:
            with self.assertRaises(PipelineError): self.pipeline.run({"request": self.request["request"]})
            invoke.assert_not_called()
        for stage in ("planner", "dispatcher", "collector"):
            values = [copy.deepcopy(self.plan), copy.deepcopy(self.dispatched), copy.deepcopy(self.collected)]
            values[("planner", "dispatcher", "collector").index(stage)]["request_id"] = "other-request"
            with self.subTest(stage=stage), patch.object(self.pipeline, "_invoke", side_effect=values):
                with self.assertRaisesRegex(PipelineError, "identity_mismatch"): self.pipeline.run(self.request)

    def test_transport_uses_fixed_argv_json_stdin_and_omits_secrets(self):
        completed = subprocess.CompletedProcess([], 1, b'{"status":"denied"}', b'PRIVATE_STDERR')
        trace = []
        with patch.dict(os.environ, {"PRIVATE_API_KEY": "secret"}), patch("ssr_chat.modular_retrieval.subprocess.run", return_value=completed) as run:
            result = self.pipeline._invoke("dispatcher", self.root, ["source/run_hgpt_ssr.py", "--plan", "-"],
                                           self.request, {0, 1}, trace)
        self.assertEqual(result["status"], "denied")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertNotIn("PRIVATE_API_KEY", run.call_args.kwargs["env"])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), self.request)
        self.assertEqual(trace[0]["exit_code"], 1)
        self.assertNotIn("PRIVATE_STDERR", json.dumps(trace))

    def test_transport_failures_have_bounded_diagnostics(self):
        for error in (OSError("PRIVATE_PATH"), subprocess.TimeoutExpired("PRIVATE_COMMAND", 1)):
            with patch("ssr_chat.modular_retrieval.subprocess.run", side_effect=error):
                with self.assertRaises(PipelineError) as caught:
                    self.pipeline._invoke("planner", self.root, [], {}, {0}, [])
                self.assertNotIn("PRIVATE", str(caught.exception))
        with patch("ssr_chat.modular_retrieval.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b'not-json', b'PRIVATE')):
            with self.assertRaisesRegex(PipelineError, "component_invalid_json"):
                self.pipeline._invoke("planner", self.root, [], {}, {0}, [])

    def test_strict_json_rejects_duplicate_nonfinite_and_nonobjects(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'[]', b'bad'):
            with self.subTest(raw=raw), self.assertRaises(PipelineError): load_object(raw)

    def test_configuration_rejects_api_adapters_before_execution(self):
        for entry in ("source/hgpt_ssr/__main__.py", "source/run_hgpt_ssr.py", "run.py"):
            path = self.root / entry
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic constructor-only placeholder", encoding="utf-8")
        (self.root / "dispatcher.json").write_text(json.dumps({"schema_version": "1.0.1", "adapters": {
            "game_knowledge": {"kind": "api", "url": "https://example.test", "timeout_seconds": 5}}}), encoding="utf-8")
        config = {"schema_version": "1.0.1", "python": sys.executable, "planner_repo": ".", "dispatcher_repo": ".",
                  "collector_repo": ".", "dispatcher_config": "dispatcher.json"}
        path = self.root / "pipeline.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        with patch("ssr_chat.modular_retrieval.subprocess.run") as run:
            with self.assertRaisesRegex(PipelineError, "only_local_cli"): PipelineConfig.read(path)
            run.assert_not_called()

    def test_existing_output_is_preserved_without_running_components(self):
        output = self.root / "existing.json"
        output.write_text("original", encoding="utf-8")
        with redirect_stderr(StringIO()), patch.object(PipelineConfig, "read") as read:
            code = main(["retrieve", "--request", "missing", "--pipeline-config", "missing", "--output", str(output)])
        self.assertEqual(code, 2)
        read.assert_not_called()
        self.assertEqual(output.read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
