from contextlib import redirect_stderr
from copy import deepcopy
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.cli import main
from ssr_chat.models import AnswerPrompt
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_answer import EvidenceAnswerGateway, PurposePolicy, content_digest, load_answer_routes
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.ollama_model import OllamaModelClient


class ModularAnswerTests(unittest.TestCase):
    def setUp(self):
        self.scope = {"domain": "channel_knowledge", "platform": "synthetic", "channel_id": "room", "game_id": None,
                      "session_id": "session", "content_version": "v1"}
        self.event = {"record_id": "record", "source_id": "source", "source_event_id": "event", "revision": 2,
            "event_time": "2026-11-01T10:00:00Z", "scope": self.scope, "layer": "raw", "text": "Synthetic launch is 10:00.",
            "quality_flags": ["synthetic"], "source_kind": "synthetic", "source_refs": [{"source_id": "source", "record_id": "record",
                "revision": 2, "locator": "synthetic://record/2", "transformation_version": None}]}
        self.policy = {"schema_version": "1.0.1", "policy_id": "synthetic-policy", "grants": [self.grant(self.event)]}
        self.pipeline = Mock()
        self.pipeline.run.return_value = {"status": "complete", "request_id": "request", "collection": {
            "contract_version": "1.0.1", "partial": False, "evidence": [deepcopy(self.event)]}}
        self.retrieval = ModularRetrievalGateway(self.pipeline, {"synthetic": True, "request": {"schema_version": "1.0.1",
            "scope": self.scope, "layer": "raw"}, "trusted_context": {"synthetic": True, "access_scope": {
                "read_scopes": [{"domain": "channel_knowledge"}], "write_scopes": []}}}, expected_domain="channel_knowledge")
        self.model = Mock()
        self.model.generate.return_value = "10:00 [E1]"
        self.trace = []
        self.runtime = Mock()
        self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}

    def grant(self, event):
        return {**{key: deepcopy(event[key]) for key in ("scope", "source_id", "record_id", "revision", "layer", "source_kind")},
                "content_sha256": content_digest(event), "ai_input": {"status": "allowed", "basis": "Authored synthetic unit input"}}

    def gateway(self, policy=None, trace=None):
        return EvidenceAnswerGateway(self.retrieval, PurposePolicy(policy if policy is not None else self.policy), self.model,
                                     trace if trace is not None else self.trace)

    def answer(self, gateway=None):
        return (gateway or self.gateway()).answer({"request_id": "request", "text": "launch time?"})

    def test_allowed_record_preserves_quote_and_provenance_in_prompt_and_answer(self):
        result = self.answer()
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "10:00 [E1]")
        self.assertEqual(result["public_output"], "not_attempted")
        self.assertEqual(result["cited_evidence_ids"], ["E1"])
        prompt = self.model.generate.call_args.args[0]
        self.assertEqual(prompt.user_text, "launch time?")
        self.assertTrue(prompt.evidence_citations)
        self.assertEqual(prompt.query_results[0]["materials"][0]["text"], self.event["text"])
        self.assertNotIn("source_refs", prompt.query_results[0]["materials"][0])
        self.assertEqual(result["provided_evidence"][0]["source_refs"], self.event["source_refs"])
        self.assertEqual([item["phase"] for item in self.trace], ["prepared", "finished"])

    def test_prompt_materials_are_compact_and_map_to_full_provided_evidence(self):
        result = self.answer()
        prompt = self.model.generate.call_args.args[0].as_dict()
        materials = prompt["query_results"][0]["materials"]
        self.assertEqual(prompt["query_results"][0]["sources"], [])
        provided = {row["evidence_id"]: row for row in result["provided_evidence"]}
        for material in materials:
            self.assertEqual(set(material), {"evidence_id", "event_time", "text", "quality_flags"})
            full = provided[material["evidence_id"]]
            for key in material:
                self.assertEqual(material[key], full[key])
            self.assertEqual(full["content_sha256"], content_digest(self.event))

    def test_readable_synthetic_record_does_not_implicitly_grant_ai_use(self):
        for status in ("unknown", "prohibited", "missing"):
            policy = deepcopy(self.policy)
            if status == "missing":
                policy["grants"] = []
            else:
                policy["grants"][0]["ai_input"]["status"] = status
            result = self.answer(self.gateway(policy))
            self.assertEqual(result["reason_code"], "ai_input_not_allowed")
            self.assertFalse(result["model_invoked"])
        self.model.generate.assert_not_called()

    def test_revision_scope_and_content_are_bound_to_exact_receipt(self):
        for field in ("revision", "scope", "text"):
            result = deepcopy(self.pipeline.run.return_value)
            event = result["collection"]["evidence"][0]
            if field == "revision": event["revision"] = 3
            elif field == "scope": event["scope"]["channel_id"] = "other-room"
            else: event["text"] = "Different data at the same revision"
            self.pipeline.run.return_value = result
            held = self.answer()
            self.assertIn(held["reason_code"], ("ai_input_not_allowed", "approved_content_mismatch"))
            self.assertFalse(held["model_invoked"])
            self.pipeline.run.return_value["collection"]["evidence"] = [deepcopy(self.event)]
        self.model.generate.assert_not_called()

    def test_foreign_reference_is_not_assumed_to_share_parent_scope(self):
        event = deepcopy(self.event)
        event["source_refs"][0]["record_id"] = "unresolved-foreign-record"
        self.pipeline.run.return_value["collection"]["evidence"] = [event]
        policy = {**self.policy, "grants": [self.grant(event)]}
        self.assertEqual(self.answer(self.gateway(policy))["reason_code"], "unverified_source_reference")
        self.model.generate.assert_not_called()

    def test_real_or_unlabelled_evidence_stops_before_model(self):
        self.pipeline.run.return_value["collection"]["evidence"][0]["source_kind"] = "local_data"
        self.assertEqual(self.answer()["reason_code"], "unsupported_answer_source")
        self.model.generate.assert_not_called()

    def test_incomplete_and_empty_retrieval_do_not_invoke_model(self):
        self.pipeline.run.return_value["status"] = "incomplete"
        self.assertEqual(self.answer()["reason_code"], "retrieval_incomplete")
        self.pipeline.run.return_value["status"] = "complete"
        self.pipeline.run.return_value["collection"]["evidence"] = []
        self.assertEqual(self.answer()["reason_code"], "no_answer_evidence")
        self.model.generate.assert_not_called()

    def test_missing_or_unknown_citation_holds_generated_answer(self):
        for answer in ("10:00", "10:00 [E999]", "10:00 [E1] [E0]"):
            self.model.generate.return_value = answer
            result = self.answer()
            self.assertTrue(result["model_invoked"])
            self.assertEqual(result["reason_code"], "answer_citations_invalid")
            self.assertIsNone(result["answer"])

    def test_model_failure_is_bounded_and_never_public(self):
        self.model.generate.side_effect = ValueError("PRIVATE_ERROR_PAYLOAD")
        result = self.answer()
        self.assertEqual(result["status"], "model_error")
        self.assertIsNone(result["answer"])
        self.assertNotIn("PRIVATE_ERROR_PAYLOAD", json.dumps(self.trace))
        self.assertEqual(result["public_output"], "not_attempted")

    def test_trace_failure_before_model_prevents_invocation(self):
        trace = Mock()
        trace.append.side_effect = OSError("private path")
        with self.assertRaises(OSError): self.answer(self.gateway(trace=trace))
        self.model.generate.assert_not_called()

    def test_large_approved_prompt_is_held_before_model(self):
        event = deepcopy(self.event); event["text"] = "synthetic " * 3000
        self.pipeline.run.return_value["collection"]["evidence"] = [event]
        self.assertEqual(self.answer(self.gateway({**self.policy, "grants": [self.grant(event)]}))["reason_code"], "answer_prompt_limit")
        self.model.generate.assert_not_called()

    def test_duplicate_or_non_synthetic_policy_records_are_rejected(self):
        duplicate = deepcopy(self.policy); duplicate["grants"] *= 2
        with self.assertRaises(PipelineError): PurposePolicy(duplicate)
        real = deepcopy(self.policy); real["grants"][0]["source_kind"] = "real"
        with self.assertRaises(PipelineError): PurposePolicy(real)

    def test_http_cannot_supply_ai_policy_or_model_configuration(self):
        for field in ("ai_input", "purpose_policy", "scope", "model", "endpoint", "prompt"):
            payload = {"request_id": "request", "text": "question", field: "allow-all"}
            code, body = dispatch(self.runtime, "POST", "/answer/channel-knowledge", json.dumps(payload).encode(),
                                  answer_routes={"/answer/channel-knowledge": self.gateway()})
            self.assertEqual(code, 400)
        self.model.generate.assert_not_called()
        self.pipeline.run.assert_not_called()
        self.runtime.handle.assert_not_called()

    def test_unknown_answer_endpoint_has_no_chat_or_fixture_fallback(self):
        code, body = dispatch(self.runtime, "POST", "/answer/unknown", b'{}')
        self.assertEqual((code, body["status"]), (503, "not_configured"))
        self.runtime.handle.assert_not_called()

    def test_answer_route_requires_synthetic_raw_channel_profile(self):
        for profile in ({"domain": "stt_transcript", "layer": "raw", "data_mode": "declared_synthetic"},
                        {"domain": "channel_knowledge", "layer": "published", "data_mode": "declared_synthetic"},
                        {"domain": "channel_knowledge", "layer": "raw", "data_mode": "operator_configured"}):
            with self.assertRaises(PipelineError): EvidenceAnswerGateway(Mock(profile=profile), PurposePolicy(self.policy), self.model, self.trace)

    def test_loader_uses_known_server_retrieval_route_and_local_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "policy.json").write_text(json.dumps(self.policy))
            config = {"schema_version": "1.0.1", "routes": {"/answer/channel-knowledge": {
                "retrieval_route": "/retrieve/channel-knowledge", "purpose_policy": "policy.json"}}}
            (root / "routes.json").write_text(json.dumps(config))
            routes = load_answer_routes(root / "routes.json", {"/retrieve/channel-knowledge": self.retrieval}, self.model, self.trace)
            self.assertEqual(routes["/answer/channel-knowledge"].profile["purpose_gate"], "exact_record_revision_content")
            with self.assertRaises(PipelineError): load_answer_routes(root / "routes.json", {}, self.model, self.trace)

    def test_cli_requires_explicit_model_and_wires_answer_routes(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stderr(StringIO()), patch("ssr_chat.cli.serve") as serve:
            self.assertEqual(main(["serve", "--state-dir", directory, "--answer-routes-config", "missing"]), 2)
            serve.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, patch("ssr_chat.modular_answer.load_answer_routes", return_value={"answer": self.gateway()}) as load, patch("ssr_chat.cli.serve") as serve:
            self.assertEqual(main(["serve", "--state-dir", directory, "--answer-routes-config", "trusted", "--model-adapter", "ollama",
                                   "--ollama-endpoint", "http://127.0.0.1:11435", "--ollama-model", "synthetic-installed"]), 0)
            self.assertEqual(serve.call_args.kwargs["answer_routes"].keys(), {"answer"})
            self.assertEqual(load.call_args.args[0], "trusted")

    def test_citation_request_is_opt_in_and_generate_uses_rendered_body(self):
        client = OllamaModelClient("http://127.0.0.1:11435", "synthetic-installed")
        ordinary = AnswerPrompt("question", "snapshot", (), {}, ())
        cited = replace(ordinary, evidence_citations=True)
        self.assertNotIn("evidence_citations", ordinary.as_dict())
        self.assertNotIn("Cite the provided", client.request_body(ordinary)["messages"][0]["content"])
        body = client.request_body(cited)
        self.assertIn("Cite the provided", body["messages"][0]["content"])
        self.assertFalse(body["think"])
        with patch.object(client, "_request_json", return_value={"message": {"content": "10:00 [E1]"}}) as request:
            self.assertEqual(client.generate(cited), "10:00 [E1]")
            request.assert_called_once_with("/api/chat", body)


if __name__ == "__main__": unittest.main()
