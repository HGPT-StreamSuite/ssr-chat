"""Prepared prompts retain the normal evidence gates without invoking a model."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_answer import (
    model_material,
    EvidenceAnswerGateway, PurposePolicy, content_digest, encoded, load_answer_routes,
)
from ssr_chat.modular_entry import EntryGateway
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.ollama_model import OllamaModelClient


class PrepareAnswerTests(unittest.TestCase):
    def setUp(self):
        scope = {"domain": "channel_knowledge", "platform": "synthetic-chat", "channel_id": "channel-a",
                 "game_id": "synthetic-game", "session_id": "session001", "content_version": "v1"}
        self.event = {
            "record_id": "record-a", "source_id": "source-a", "source_event_id": "event-a", "revision": 1,
            "event_time": "2026-11-01T10:00:00Z", "scope": scope, "layer": "raw",
            "text": "Authored synthetic channel information.", "quality_flags": ["synthetic"],
            "source_kind": "synthetic", "source_refs": [{"source_id": "source-a", "record_id": "record-a",
                "revision": 1, "locator": "synthetic://record-a/1", "transformation_version": None}],
        }
        self.pipeline = Mock()
        self.pipeline.run.return_value = {"status": "complete", "request_id": "request-a", "collection": {
            "contract_version": "1.0.1", "partial": False, "evidence": [deepcopy(self.event)]}}
        self.retrieval = ModularRetrievalGateway(self.pipeline, {"synthetic": True, "request": {
            "schema_version": "1.0.1", "scope": scope, "layer": "raw"}, "trusted_context": {
            "synthetic": True, "access_scope": {"read_scopes": [{"domain": "channel_knowledge"}],
                                                "write_scopes": []}}}, expected_domain="channel_knowledge")
        self.policy_value = self.policy_for(self.event)
        self.trace = []
        self.model = Mock()
        self.model.generate.return_value = "Synthetic information [E1]"
        self.payload = {"request_id": "request-a", "text": "channel information?"}

    @staticmethod
    def policy_for(event):
        grant = {key: deepcopy(event[key]) for key in (
            "scope", "source_id", "record_id", "revision", "layer", "source_kind")}
        grant.update(content_sha256=content_digest(event), ai_input={
            "status": "allowed", "basis": "Authored synthetic unit input"})
        return {"schema_version": "1.0.1", "policy_id": "prepare-synthetic-policy", "grants": [grant]}

    def gateway(self, *, model=None, policy=None, prepare_only=True, trace=None):
        return EvidenceAnswerGateway(self.retrieval, PurposePolicy(policy or self.policy_value), model,
                                     self.trace if trace is None else trace, prepare_only=prepare_only)

    def test_prepares_exact_permitted_prompt_without_a_model(self):
        gateway = self.gateway()
        result = gateway.answer(self.payload)
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["reason_code"], "answer_generation_not_enabled")
        self.assertFalse(result["model_invoked"])
        self.assertIsNone(result["answer"])
        self.assertEqual(result["public_output"], "not_attempted")
        self.assertEqual(result["cited_evidence_ids"], [])
        self.assertEqual([row["phase"] for row in self.trace], ["prepared", "finished"])
        prepared = self.trace[0]
        prompt = prepared["prompt"]
        self.assertEqual(prompt["user_text"], self.payload["text"])
        self.assertEqual(prompt["query_results"][0]["materials"],
                         [model_material(row, row["scope"]["domain"]) for row in result["provided_evidence"]])
        self.assertEqual(result["provided_evidence"][0]["text"], self.event["text"])
        self.assertEqual(result["provided_evidence"][0]["source_refs"], self.event["source_refs"])
        self.assertEqual(result["prompt_sha256"], hashlib.sha256(encoded(prompt)).hexdigest())
        self.assertEqual(prepared["prompt_sha256"], result["prompt_sha256"])
        self.assertFalse(prepared["model_invoked"])
        self.assertNotIn("model_request", prepared)
        self.assertNotIn("sent_messages", json.dumps(self.trace))
        self.assertEqual(gateway.profile["model"], "not_run")
        self.assertFalse(gateway.profile["answer_generation_enabled"])
        self.assertTrue(gateway.profile["citations_required"])

    def test_prepared_mode_does_not_render_or_call_an_injected_model(self):
        # No constructor or endpoint is used; the adapter exists only as a spy.
        model = object.__new__(OllamaModelClient)
        model.request_body = Mock(side_effect=AssertionError("must not render a model request"))
        model.generate = Mock(side_effect=AssertionError("must not invoke a model"))
        result = self.gateway(model=model).answer(self.payload)
        self.assertEqual(result["reason_code"], "answer_generation_not_enabled")
        model.request_body.assert_not_called()
        model.generate.assert_not_called()

    def test_prepared_mode_does_not_bypass_purpose_or_content_checks(self):
        for change, reason in (("permission", "ai_input_not_allowed"),
                               ("text", "approved_content_mismatch"),
                               ("source", "unsupported_answer_source")):
            with self.subTest(change=change):
                self.trace.clear()
                self.pipeline.run.return_value["collection"]["evidence"] = [deepcopy(self.event)]
                policy = deepcopy(self.policy_value)
                if change == "permission":
                    policy["grants"][0]["ai_input"]["status"] = "unknown"
                elif change == "text":
                    self.pipeline.run.return_value["collection"]["evidence"][0]["text"] = "Changed content"
                else:
                    self.pipeline.run.return_value["collection"]["evidence"][0]["source_kind"] = "local_data"
                result = self.gateway(model=self.model, policy=policy).answer(self.payload)
                self.assertEqual(result["reason_code"], reason)
                self.assertEqual([row["phase"] for row in self.trace], ["finished"])
                self.assertEqual(result["provided_evidence"], [])
                self.assertNotIn("prompt_sha256", result)
        self.model.generate.assert_not_called()

    def test_oversize_approved_prompt_is_not_prepared(self):
        event = deepcopy(self.event)
        event["text"] = "synthetic " * 3000
        self.pipeline.run.return_value["collection"]["evidence"] = [event]
        result = self.gateway(model=self.model, policy=self.policy_for(event)).answer(self.payload)
        self.assertEqual(result["reason_code"], "answer_prompt_limit")
        self.assertEqual([row["phase"] for row in self.trace], ["finished"])
        self.assertNotIn("prompt_sha256", result)
        self.model.generate.assert_not_called()

    def test_incomplete_retrieval_does_not_prepare(self):
        self.pipeline.run.return_value["collection"]["partial"] = True
        result = self.gateway().answer(self.payload)
        self.assertEqual(result["reason_code"], "retrieval_incomplete")
        self.assertEqual([row["phase"] for row in self.trace], ["finished"])

    def test_model_is_required_unless_explicitly_preparing(self):
        with self.assertRaisesRegex(PipelineError, "answer_model_required"):
            self.gateway(prepare_only=False)
        with self.assertRaisesRegex(PipelineError, "answer_prepare_only_invalid"):
            self.gateway(prepare_only="true")

    def test_default_generation_still_invokes_and_validates_model(self):
        gateway = EvidenceAnswerGateway(self.retrieval, PurposePolicy(self.policy_value), self.model, self.trace)
        result = gateway.answer(self.payload)
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["model_invoked"])
        self.assertEqual(result["answer"], "Synthetic information [E1]")
        self.model.generate.assert_called_once()
        self.assertTrue(gateway.profile["answer_generation_enabled"])
        self.assertEqual(gateway.profile["model"], "custom")

    def test_loader_and_entry_preserve_prepared_only_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "policy.json").write_text(json.dumps(self.policy_value), encoding="utf-8")
            config = {"schema_version": "1.0.1", "routes": {"/answer/channel-knowledge": {
                "retrieval_route": "/retrieve/channel-knowledge", "purpose_policy": "policy.json"}}}
            (root / "routes.json").write_text(json.dumps(config), encoding="utf-8")
            routes = load_answer_routes(root / "routes.json", {"/retrieve/channel-knowledge": self.retrieval},
                                        None, self.trace, prepare_only=True)
        entry = EntryGateway({"schema_version": "1.0.1", "routing": "explicit_command_v1", "targets": {
            "/channel": "/answer/channel-knowledge"}}, {}, routes, [])
        result = entry.query({**self.payload, "text": "/channel channel information?"})
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["reason_code"], "answer_generation_not_enabled")
        self.assertFalse(result["model_invoked"])
        self.assertEqual([row["phase"] for row in self.trace], ["prepared", "finished"])

    def test_prepared_trace_failure_propagates_without_model_invocation(self):
        trace = Mock()
        trace.append.side_effect = OSError("trace unavailable")
        with self.assertRaises(OSError):
            self.gateway(model=self.model, trace=trace).answer(self.payload)
        self.model.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
