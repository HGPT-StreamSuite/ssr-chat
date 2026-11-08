from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from ssr_chat.api import dispatch
from ssr_chat.models import AnswerPrompt
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_answer import CONTENT_FIELDS, EvidenceAnswerGateway, PurposePolicy, content_digest, encoded
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.ollama_model import OllamaModelClient


class ChatAnswerTests(unittest.TestCase):
    def setUp(self):
        self.scope = {"domain": "chat_history", "platform": "synthetic", "channel_id": "room", "game_id": None,
                      "session_id": "session", "content_version": None}
        self.event = {"record_id": "record", "source_id": "source", "source_event_id": "previous", "revision": 2,
            "event_time": "2026-10-30T10:19:59Z", "scope": self.scope, "layer": "raw", "text": "合成原句\\n保留跳脫。",
            "actor_account_id": "alice", "quality_flags": ["synthetic"], "source_kind": "synthetic", "source_refs": [
                {"source_id": "source", "record_id": "record", "revision": 2, "locator": "synthetic://record/2", "transformation_version": None}]}
        self.template = {"synthetic": True, "request": {"schema_version": "1.0.1", "scope": self.scope, "layer": "raw", "limit": 1},
            "trusted_context": {"synthetic": True, "actor_account_id": "alice", "current_event": {
                "source_event_id": "current", "event_time": "2026-10-30T10:20:00Z"},
                "access_scope": {"read_scopes": [{"domain": "chat_history"}], "write_scopes": []}}}
        self.query = {"scope": deepcopy(self.scope), "actor_account_id": "alice", "intent": "recent_utterance", "limit": 1,
                      "layer": "raw", "exclude_event_ids": ["current"], "conditions": {"before_event_time": "2026-10-30T10:20:00Z"}}
        self.report = {"status": "complete", "request_id": "request", "plan": {"tasks": [{"domain": "chat_history", "query": self.query}]},
                       "collection": {"contract_version": "1.0.1", "partial": False, "evidence": [deepcopy(self.event)]}}
        self.pipeline = Mock()
        self.pipeline.run.return_value = deepcopy(self.report)
        self.retrieval = ModularRetrievalGateway(self.pipeline, self.template, expected_domain="chat_history")
        self.model = Mock()
        self.model.generate.return_value = json.dumps({"quote": self.event["text"], "evidence_id": "E1"}, ensure_ascii=False)
        self.trace = []

    def policy(self, event=None):
        event = event or self.event
        grant = {**{key: deepcopy(event[key]) for key in ("scope", "source_id", "record_id", "revision", "layer", "source_kind", "actor_account_id")},
                 "content_sha256": content_digest(event), "ai_input": {"status": "allowed", "basis": "Authored synthetic own-chat test"}}
        return {"schema_version": "1.0.1", "policy_id": "synthetic-chat", "grants": [grant]}

    def gateway(self, policy=None):
        return EvidenceAnswerGateway(self.retrieval, PurposePolicy(policy if policy is not None else self.policy()), self.model, self.trace)

    def answer(self, gateway=None):
        return (gateway or self.gateway()).answer({"request_id": "request", "text": "我剛剛在聊天頻道打了什麼訊息？"})

    def test_allowed_own_recent_quote_preserves_actor_source_and_literal_text(self):
        result = self.answer()
        self.assertEqual(result["status"], "answered")
        self.assertIn(self.event["text"], result["answer"])
        prompt = self.model.generate.call_args.args[0]
        material = prompt.query_results[0]["materials"][0]
        self.assertEqual(material["actor_account_id"], "alice")
        self.assertEqual(material["source_refs"], self.event["source_refs"])
        self.assertEqual(material["text"], self.event["text"])
        self.assertEqual(prompt.analysis["intents"], ["chat_history"])
        self.assertEqual(result["public_output"], "not_attempted")

    def test_actor_is_bound_in_receipt_identity_and_content_hash(self):
        bob = deepcopy(self.event); bob["actor_account_id"] = "bob"
        self.assertNotEqual(content_digest(bob), content_digest(self.event))
        self.assertEqual(PurposePolicy(self.policy()).check(bob), "ai_input_not_allowed")

    def test_approved_other_actor_cannot_escape_host_binding(self):
        bob = deepcopy(self.event); bob["actor_account_id"] = "bob"
        self.pipeline.run.return_value["collection"]["evidence"] = [bob]
        held = self.answer(self.gateway(self.policy(bob)))
        self.assertEqual(held["reason_code"], "answer_actor_scope_mismatch")
        self.model.generate.assert_not_called()

    def test_approved_other_session_cannot_escape_host_binding(self):
        other = deepcopy(self.event); other["scope"]["session_id"] = "other-session"
        self.pipeline.run.return_value["collection"]["evidence"] = [other]
        self.assertEqual(self.answer(self.gateway(self.policy(other)))["reason_code"], "answer_actor_scope_mismatch")
        self.model.generate.assert_not_called()

    def test_missing_or_widened_boundary_holds_before_model(self):
        for case in ("missing_before", "widened_before", "missing_exclusion", "multi_session", "limit"):
            report = deepcopy(self.report); query = report["plan"]["tasks"][0]["query"]
            if case == "missing_before": query["conditions"] = {}
            elif case == "widened_before": query["conditions"]["before_event_time"] = "2026-10-30T10:21:00Z"
            elif case == "missing_exclusion": query["exclude_event_ids"] = []
            elif case == "multi_session": query["conditions"]["session_ids"] = ["other"]
            else: query["limit"] = 2
            self.pipeline.run.return_value = report
            self.assertEqual(self.answer()["reason_code"], "chat_recent_boundary_missing")
        self.model.generate.assert_not_called()

    def test_future_or_current_event_is_held_even_with_matching_ai_receipt(self):
        for field, value in (("event_time", "2026-10-30T10:20:00Z"), ("source_event_id", "current")):
            event = deepcopy(self.event); event[field] = value
            self.pipeline.run.return_value["collection"]["evidence"] = [event]
            self.assertEqual(self.answer(self.gateway(self.policy(event)))["reason_code"], "chat_recent_boundary_missing")
        self.model.generate.assert_not_called()

    def test_unknown_or_prohibited_purpose_is_held(self):
        for status in ("unknown", "prohibited"):
            policy = self.policy(); policy["grants"][0]["ai_input"]["status"] = status
            self.assertEqual(self.answer(self.gateway(policy))["reason_code"], "ai_input_not_allowed")
        self.model.generate.assert_not_called()

    def test_cited_paraphrase_is_held_instead_of_claimed_verbatim(self):
        self.model.generate.return_value = json.dumps({"quote": "這是原句的摘要", "evidence_id": "E1"}, ensure_ascii=False)
        result = self.answer()
        self.assertTrue(result["model_invoked"])
        self.assertEqual(result["reason_code"], "answer_quote_not_preserved")
        self.assertIsNone(result["answer"])

    def test_invalid_quote_json_or_changed_escape_value_is_held(self):
        for reply, reason in (("plain text [E1]", "answer_format_invalid"),
                              ('{"quote":"a","quote":"b","evidence_id":"E1"}', "answer_format_invalid"),
                              (json.dumps({"quote": self.event["text"].replace("\\n", "\n"), "evidence_id": "E1"}), "answer_quote_not_preserved")):
            self.model.generate.return_value = reply
            result = self.answer()
            self.assertEqual(result["reason_code"], reason)
            self.assertIsNone(result["answer"])

    def test_chat_policy_requires_actor_and_concrete_session(self):
        for field in ("actor_account_id", "session"):
            policy = self.policy()
            if field == "actor_account_id": policy["grants"][0].pop("actor_account_id")
            else: policy["grants"][0]["scope"]["session_id"] = None
            with self.assertRaises(PipelineError): PurposePolicy(policy)

    def test_chat_route_requires_stable_host_actor_and_current_event(self):
        for key in ("actor_account_id", "current_event"):
            template = deepcopy(self.template); template["trusted_context"].pop(key)
            retrieval = ModularRetrievalGateway(self.pipeline, template, expected_domain="chat_history")
            with self.assertRaises(PipelineError): EvidenceAnswerGateway(retrieval, PurposePolicy(self.policy()), self.model, self.trace)

    def test_backend_context_copy_is_not_public_or_mutable(self):
        context = self.retrieval.trusted_answer_context
        context["scope"]["session_id"] = "changed"
        context["current_event"]["source_event_id"] = "changed"
        self.assertEqual(self.retrieval.trusted_answer_context["scope"]["session_id"], "session")
        runtime = Mock(); runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}
        _, body = dispatch(runtime, "GET", "/health", answer_routes={"/answer/chat-history": self.gateway()})
        self.assertNotIn("alice", json.dumps(body))
        self.assertNotIn("current_event", json.dumps(body))

    def test_http_cannot_replace_actor_scope_event_or_purpose(self):
        runtime = Mock()
        for field in ("actor_account_id", "scope", "current_event", "ai_input", "source_refs"):
            code, _ = dispatch(runtime, "POST", "/answer/chat-history", json.dumps({"request_id": "request", "text": "question", field: "all"}).encode(),
                               answer_routes={"/answer/chat-history": self.gateway()})
            self.assertEqual(code, 400)
        self.pipeline.run.assert_not_called(); self.model.generate.assert_not_called()

    def test_fixed_quote_instruction_only_applies_to_opt_in_chat_prompt(self):
        client = OllamaModelClient("http://127.0.0.1:11435", "synthetic-installed")
        chat = AnswerPrompt("question", "snapshot", (), {"intents": ["chat_history"]}, (), evidence_citations=True)
        self.assertIn("Quote its complete text verbatim", client.request_body(chat)["messages"][0]["content"])
        ordinary = AnswerPrompt("question", "snapshot", (), {"intents": ["chat_history"]}, ())
        channel = AnswerPrompt("question", "snapshot", (), {"intents": ["channel_knowledge"]}, (), evidence_citations=True)
        self.assertNotIn("Quote its complete text verbatim", client.request_body(ordinary)["messages"][0]["content"])
        self.assertNotIn("Quote its complete text verbatim", client.request_body(channel)["messages"][0]["content"])


if __name__ == "__main__": unittest.main()
