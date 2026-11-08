from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from ssr_chat.api import dispatch
from ssr_chat.models import AnswerPrompt
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_answer import EvidenceAnswerGateway, PurposePolicy, content_digest
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.ollama_model import OllamaModelClient


class SttAnswerTests(unittest.TestCase):
    def setUp(self):
        self.scope = {"domain": "stt_transcript", "platform": "synthetic", "channel_id": "room", "game_id": None,
                      "session_id": "session", "content_version": "v1"}
        self.event = {"record_id": "record", "source_id": "source", "source_event_id": "segment", "revision": 2,
            "event_time": "2026-10-30T12:00:00Z", "scope": self.scope, "layer": "raw", "text": "合成辨識原句。\n保留換行。",
            "actor_account_id": "synthetic-speaker", "quality_flags": ["synthetic", "unreviewed", "final"], "source_kind": "synthetic",
            "source_refs": [{"source_id": "source", "record_id": "record", "revision": 2, "locator": "synthetic://stt/2", "transformation_version": None}],
            "match_reasons": ["revision_view:current", "revision_effective:true"],
            "extensions": {"stt": {"segment_id": "segment", "segment_revision": 2, "state": "final", "speaker_id": "opaque-speaker-a",
                "started_at": "2026-10-30T12:00:00Z", "ended_at": "2026-10-30T12:00:03Z", "source_confidence": 0.92}}}
        self.template = {"synthetic": True, "request": {"schema_version": "1.0.1", "scope": self.scope, "layer": "raw", "limit": 1,
            "time_from": "2026-10-30T12:00:00Z", "time_to": "2026-10-30T12:00:10Z"},
            "trusted_context": {"synthetic": True, "actor_account_id": "synthetic-speaker", "access_scope": {
                "read_scopes": [{"domain": "stt_transcript"}], "write_scopes": []}}}
        query = {**deepcopy(self.template["request"]), "actor_account_id": "synthetic-speaker",
                 "intent": "stt_lookup", "conditions": {"revision_view": "current"}}
        self.report = {"status": "complete", "request_id": "request", "plan": {"tasks": [{"domain": "stt_transcript", "query": query}]},
                       "collection": {"contract_version": "1.0.1", "partial": False, "evidence": [deepcopy(self.event)]}}
        self.pipeline = Mock(); self.pipeline.run.return_value = deepcopy(self.report)
        self.retrieval = ModularRetrievalGateway(self.pipeline, self.template, expected_domain="stt_transcript")
        self.model = Mock(); self.model.generate.return_value = json.dumps({"quote": self.event["text"], "evidence_id": "E1"}, ensure_ascii=False)
        self.trace = []

    def policy(self, event=None):
        event = event or self.event
        grant = {**{key: deepcopy(event[key]) for key in ("scope", "source_id", "record_id", "revision", "layer", "source_kind", "actor_account_id")},
                 "content_sha256": content_digest(event), "ai_input": {"status": "allowed", "basis": "Authored synthetic STT test"}}
        return {"schema_version": "1.0.1", "policy_id": "synthetic-stt", "grants": [grant]}

    def gateway(self, policy=None):
        return EvidenceAnswerGateway(self.retrieval, PurposePolicy(policy if policy is not None else self.policy()), self.model, self.trace)

    def answer(self, gateway=None):
        return (gateway or self.gateway()).answer({"request_id": "request", "text": "查詢我的逐字稿"})

    def test_final_quote_preserves_recognition_metadata_and_caveat(self):
        result = self.answer()
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["quoted_evidence"]["quote"], self.event["text"])
        self.assertTrue(result["answer"].startswith("逐字稿辨識文字："))
        self.assertEqual(result["evidence_type"], "stt_recognition")
        self.assertEqual(result["recognition"]["source_confidence"], 0.92)
        self.assertIn("unreviewed", result["recognition"]["quality_flags"])
        self.assertTrue(result["recognition"]["not_validated_against_audio"])
        material = self.model.generate.call_args.args[0].query_results[0]["materials"][0]
        self.assertEqual(material["extensions"], self.event["extensions"])
        self.assertEqual(material["actor_account_id"], "synthetic-speaker")

    def test_segment_confidence_is_bound_in_content_hash(self):
        changed = deepcopy(self.event); changed["extensions"]["stt"]["source_confidence"] = 0.25
        self.assertNotEqual(content_digest(changed), content_digest(self.event))
        self.pipeline.run.return_value["collection"]["evidence"] = [changed]
        self.assertEqual(self.answer()["reason_code"], "approved_content_mismatch")
        self.model.generate.assert_not_called()

    def test_allowed_partial_recognition_is_still_held(self):
        partial = deepcopy(self.event); partial["extensions"]["stt"]["state"] = "partial"
        self.pipeline.run.return_value["collection"]["evidence"] = [partial]
        self.assertEqual(self.answer(self.gateway(self.policy(partial)))["reason_code"], "stt_final_segment_required")
        self.model.generate.assert_not_called()

    def test_entire_segment_must_fit_window_even_with_matching_receipt(self):
        crossed = deepcopy(self.event); crossed["extensions"]["stt"]["ended_at"] = "2026-10-30T12:00:11Z"
        self.pipeline.run.return_value["collection"]["evidence"] = [crossed]
        self.assertEqual(self.answer(self.gateway(self.policy(crossed)))["reason_code"], "stt_window_mismatch")
        self.model.generate.assert_not_called()

    def test_other_actor_or_session_cannot_escape_host_binding(self):
        for field in ("actor", "session"):
            event = deepcopy(self.event)
            if field == "actor": event["actor_account_id"] = "other"
            else: event["scope"]["session_id"] = "other"
            self.pipeline.run.return_value["collection"]["evidence"] = [event]
            self.assertEqual(self.answer(self.gateway(self.policy(event)))["reason_code"], "answer_actor_scope_mismatch")
        self.model.generate.assert_not_called()

    def test_query_cannot_widen_window_add_predicates_or_increase_limit(self):
        for case in ("window", "conditions", "limit", "intent"):
            report = deepcopy(self.report); query = report["plan"]["tasks"][0]["query"]
            if case == "window": query["time_to"] = "2026-10-30T12:01:00Z"
            elif case == "conditions": query["conditions"] = {"session_ids": ["other"]}
            elif case == "intent": query["intent"] = "stt_history"
            else: query["limit"] = 10
            self.pipeline.run.return_value = report
            self.assertEqual(self.answer()["reason_code"], "stt_window_mismatch")
        self.model.generate.assert_not_called()

    def test_history_or_missing_revision_view_never_reaches_model(self):
        for conditions in ({}, {"revision_view": "history"}, {"revision_view": "current", "unexpected": True}):
            report = deepcopy(self.report)
            report["plan"]["tasks"][0]["query"]["conditions"] = conditions
            self.pipeline.run.return_value = report
            self.assertEqual(self.answer()["reason_code"], "stt_window_mismatch")
        self.model.generate.assert_not_called()

    def test_invalid_revision_segment_alignment_and_missing_speaker_are_held(self):
        for case in ("invalid", "segment", "speaker"):
            event = deepcopy(self.event)
            if case == "invalid": event["quality_flags"].append("invalid_revision")
            elif case == "segment": event["extensions"]["stt"]["segment_revision"] = 3
            else: event["extensions"]["stt"]["speaker_id"] = None
            self.pipeline.run.return_value["collection"]["evidence"] = [event]
            self.assertEqual(self.answer(self.gateway(self.policy(event)))["reason_code"], "stt_segment_metadata_invalid")
        self.model.generate.assert_not_called()

    def test_stt_route_requires_fixed_window_actor_and_limit(self):
        for field in ("time_from", "time_to", "limit", "actor"):
            template = deepcopy(self.template)
            if field == "actor": template["trusted_context"]["actor_account_id"] = None
            else: template["request"].pop(field)
            retrieval = ModularRetrievalGateway(self.pipeline, template, expected_domain="stt_transcript")
            with self.assertRaises(PipelineError): EvidenceAnswerGateway(retrieval, PurposePolicy(self.policy()), self.model, self.trace)

    def test_unknown_purpose_and_incomplete_retrieval_do_not_invoke_model(self):
        policy = self.policy(); policy["grants"][0]["ai_input"]["status"] = "unknown"
        self.assertEqual(self.answer(self.gateway(policy))["reason_code"], "ai_input_not_allowed")
        self.pipeline.run.return_value["status"] = "incomplete"
        self.pipeline.run.return_value["collection"]["partial"] = True
        self.assertEqual(self.answer()["reason_code"], "retrieval_incomplete")
        self.model.generate.assert_not_called()

    def test_unknown_confidence_is_preserved(self):
        event = deepcopy(self.event); event["extensions"]["stt"]["source_confidence"] = None
        self.pipeline.run.return_value["collection"]["evidence"] = [event]
        result = self.answer(self.gateway(self.policy(event)))
        self.assertEqual(result["status"], "answered")
        self.assertIsNone(result["recognition"]["source_confidence"])

    def test_model_cannot_add_audio_verification_claims(self):
        self.model.generate.return_value = json.dumps({"quote": self.event["text"], "evidence_id": "E1", "verified_audio": True})
        result = self.answer()
        self.assertEqual(result["reason_code"], "answer_format_invalid")
        self.assertIsNone(result["answer"])

    def test_http_cannot_supply_window_actor_or_recognition_metadata(self):
        runtime = Mock()
        for field in ("time_from", "time_to", "actor_account_id", "source_confidence", "segment_revision", "allow_partial"):
            code, _ = dispatch(runtime, "POST", "/answer/stt-transcript", json.dumps({"request_id": "request", "text": "question", field: "all"}).encode(),
                               answer_routes={"/answer/stt-transcript": self.gateway()})
            self.assertEqual(code, 400)
        self.pipeline.run.assert_not_called(); self.model.generate.assert_not_called()

    def test_stt_system_labels_recognition_without_claiming_verified_audio(self):
        client = OllamaModelClient("http://127.0.0.1:11435", "synthetic-installed")
        prompt = AnswerPrompt("question", "snapshot", (), {"intents": ["stt_transcript"]}, (), evidence_citations=True)
        system = client.request_body(prompt)["messages"][0]["content"]
        for phrase in ("machine-recognition", "not independently verified audio", "source_confidence", "Quote its complete text verbatim"):
            self.assertIn(phrase, system)


if __name__ == "__main__": unittest.main()
