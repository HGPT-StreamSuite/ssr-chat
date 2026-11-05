from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_retrieval import ModularRetrievalPipeline, PipelineConfig, PipelineError


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/retrieval"


class TranscriptRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((FIXTURES / "transcript-raw.template.json").read_text(encoding="utf-8"))
        self.config = PipelineConfig(Path(sys.executable), FIXTURES, FIXTURES, FIXTURES, FIXTURES / "unused.json")
        self.pipeline = ModularRetrievalPipeline(self.config)
        self.gateway = ModularRetrievalGateway(self.pipeline, self.template, expected_domain="stt_transcript")
        self.query = {"schema_version": "1.0.1", "request_id": "synthetic-request", "intent": "stt_lookup",
                      "scope": deepcopy(self.template["request"]["scope"])}
        self.plan = {"schema_version": "1.0.1", "request_id": "synthetic-request", "tasks": [
            {"task_id": "stt_transcript-1", "domain": "stt_transcript", "query": self.query}],
            "missing_inputs": [], "skipped_domains": []}
        self.result = {"schema_version": "1.0.1", "request_id": "synthetic-request", "domain": "stt_transcript",
                       "status": "ok", "evidence": [], "blocked_by": [{"source_id": "synthetic-private", "reason_code": "source_policy_denied"}]}
        self.dispatched = {"schema_version": "1.0.1", "request_id": "synthetic-request", "tasks": [
            {"task_id": "stt_transcript-1", "result": self.result}]}
        self.collection = {"contract_version": "1.0.1", "request_id": "synthetic-request", "status": "ok", "partial": True}

    def retrieve(self):
        return self.gateway.retrieve({"request_id": "synthetic-request", "text": "查詢我的逐字稿"})

    def test_transcript_host_example_binds_identity_scope_and_layer(self):
        pipeline = Mock()
        gateway = ModularRetrievalGateway(pipeline, self.template, expected_domain="stt_transcript")
        gateway.retrieve({"request_id": "synthetic-request", "text": "查詢我的逐字稿"})
        supplied = pipeline.run.call_args.args[0]
        self.assertEqual(pipeline.run.call_args.kwargs, {"expected_domain": "stt_transcript"})
        self.assertEqual(supplied["trusted_context"], self.template["trusted_context"])
        self.assertEqual(supplied["request"]["scope"], self.template["request"]["scope"])
        self.assertEqual(supplied["request"]["layer"], "raw")
        self.assertTrue(supplied["synthetic"])
        self.assertEqual(supplied["trusted_context"]["current_event"], {})

    def test_foreign_task_or_query_domain_stops_before_dispatcher_or_sources(self):
        for field in ("task", "query", "missing_query"):
            plan = deepcopy(self.plan)
            if field == "task":
                plan["tasks"][0]["domain"] = "chat_history"
            elif field == "query":
                plan["tasks"][0]["query"]["scope"]["domain"] = "channel_knowledge"
            else:
                plan["tasks"][0].pop("query")
            with self.subTest(field=field), patch.object(self.pipeline, "_invoke", return_value=plan) as invoke:
                with self.assertRaisesRegex(PipelineError, "route_plan_domain_mismatch"):
                    self.retrieve()
                self.assertEqual(invoke.call_count, 1)

    def test_foreign_result_domain_is_not_sent_to_collector(self):
        dispatched = deepcopy(self.dispatched)
        dispatched["tasks"][0]["result"]["domain"] = "chat_history"
        with patch.object(self.pipeline, "_invoke", side_effect=[self.plan, dispatched]) as invoke:
            with self.assertRaisesRegex(PipelineError, "route_result_domain_mismatch"):
                self.retrieve()
        self.assertEqual(invoke.call_count, 2)

    def test_partial_transcript_result_and_denial_are_preserved(self):
        with patch.object(self.pipeline, "_invoke", side_effect=[self.plan, self.dispatched, self.collection]):
            report = self.retrieve()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["dispatch"]["tasks"][0]["result"], self.result)
        self.assertTrue(report["collection"]["partial"])
        self.assertEqual(report["answer_generation"], "not_run")
        self.assertEqual(report["public_output"], "not_attempted")

    def test_http_cannot_select_history_policy_source_or_transcript_layer(self):
        runtime = Mock()
        for key in ("revision_view", "conditions", "source_policy", "snapshot", "trusted_context", "layer"):
            payload = {"request_id": "synthetic-request", "text": "查詢我的逐字稿", key: "all"}
            with self.subTest(key=key), patch.object(self.pipeline, "_invoke") as invoke:
                code, body = dispatch(runtime, "POST", "/retrieve/transcripts", json.dumps(payload).encode(),
                                      retrieval_routes={"/retrieve/transcripts": self.gateway})
                self.assertEqual(code, 400)
                self.assertEqual(body["reason_code"], "only_request_id_and_text_allowed")
                invoke.assert_not_called()
        runtime.handle.assert_not_called()

    def test_invalid_bound_domain_stops_before_planner(self):
        for domain in ([], {}, 1, "unknown"):
            with self.subTest(domain=domain), patch.object(self.pipeline, "_invoke") as invoke:
                with self.assertRaisesRegex(PipelineError, "expected_domain_invalid"):
                    self.pipeline.run(self.template, expected_domain=domain)
                invoke.assert_not_called()

    def test_published_example_is_separate_without_authority_expansion(self):
        published = json.loads((FIXTURES / "transcript-published.template.json").read_text(encoding="utf-8"))
        self.assertEqual(published["request"]["layer"], "published")
        raw = deepcopy(self.template)
        raw["request"]["layer"] = "published"
        self.assertEqual(published, raw)


if __name__ == "__main__":
    unittest.main()
