from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.cli import main
from ssr_chat.modular_api import ModularRetrievalGateway, RetrievalInputError
from ssr_chat.modular_retrieval import PipelineError


class ModularApiTests(unittest.TestCase):
    def setUp(self):
        self.template = {
            "synthetic": True,
            "request": {"schema_version": "1.0.1", "request_id": "server-placeholder", "text": "server-placeholder",
                        "scope": {"domain": "game_knowledge", "game_id": "synthetic-game"}, "layer": "raw", "limit": 5},
            "trusted_context": {"synthetic": True, "access_scope": {"read_scopes": [{"domain": "game_knowledge"}], "write_scopes": []}},
        }
        self.report = {"status": "complete", "collection": {"partial": False, "evidence": []},
                       "answer_generation": "not_run", "public_output": "not_attempted"}
        self.pipeline = Mock()
        self.pipeline.run.return_value = self.report
        self.gateway = ModularRetrievalGateway(self.pipeline, self.template)
        self.runtime = Mock()
        self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}

    def post(self, value, gateway=True):
        return dispatch(self.runtime, "POST", "/retrieve", json.dumps(value).encode("utf-8"),
                        retrieval_gateway=self.gateway if gateway else None)

    def test_server_context_scope_layer_and_limit_are_fixed_and_copy_is_per_request(self):
        original = deepcopy(self.template)
        self.gateway.retrieve({"request_id": "client-1", "text": "  原問題  "})
        request = self.pipeline.run.call_args.args[0]
        self.assertEqual(request["request"]["request_id"], "client-1")
        self.assertEqual(request["request"]["text"], "  原問題  ")
        for key in ("scope", "layer", "limit"):
            self.assertEqual(request["request"][key], original["request"][key])
        self.assertEqual(request["trusted_context"], original["trusted_context"])
        request["trusted_context"]["access_scope"]["read_scopes"].clear()
        self.template["request"]["layer"] = "published"
        self.gateway.retrieve({"request_id": "client-2", "text": "第二個問題"})
        second = self.pipeline.run.call_args.args[0]
        self.assertEqual(second["request"]["layer"], "raw")
        self.assertEqual(second["trusted_context"], original["trusted_context"])

    def test_http_cannot_supply_authority_paths_or_configuration(self):
        for field in ("trusted_context", "scope", "access_scope", "layer", "limit", "pipeline_config", "command", "source"):
            with self.subTest(field=field):
                status, body = self.post({"request_id": "client", "text": "question", field: {"grant": "all"}})
                self.assertEqual(status, 400)
                self.assertEqual(body["reason_code"], "only_request_id_and_text_allowed")
        self.pipeline.run.assert_not_called()
        self.runtime.handle.assert_not_called()

    def test_invalid_request_identity_and_text_are_rejected_without_pipeline(self):
        cases = [None, [], {}, {"request_id": "x", "text": ""}, {"request_id": "", "text": "x"},
                 {"request_id": 3, "text": "x"}, {"request_id": "x", "text": True},
                 {"request_id": "x" * 201, "text": "x"}, {"request_id": "x", "text": "x" * 16385}]
        for value in cases:
            with self.subTest(value=str(value)[:40]), self.assertRaises(RetrievalInputError): self.gateway.retrieve(value)
        self.pipeline.run.assert_not_called()

    def test_unconfigured_endpoint_does_not_fall_back_to_chat_or_fixture(self):
        status, body = self.post({"request_id": "x", "text": "question"}, gateway=False)
        self.assertEqual((status, body["status"]), (503, "not_configured"))
        self.pipeline.run.assert_not_called()
        self.runtime.handle.assert_not_called()

    def test_successful_retrieval_does_not_call_answer_runtime(self):
        status, body = self.post({"request_id": "x", "text": "question"})
        self.assertEqual(status, 200)
        self.assertIs(body, self.report)
        self.assertEqual(body["answer_generation"], "not_run")
        self.assertEqual(body["public_output"], "not_attempted")
        self.runtime.handle.assert_not_called()

    def test_partial_remains_incomplete_in_http_200_body(self):
        self.pipeline.run.return_value = {"status": "incomplete", "collection": {"partial": True, "reason_code": "partial_results"}}
        status, body = self.post({"request_id": "x", "text": "question"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "incomplete")
        self.assertTrue(body["collection"]["partial"])

    def test_component_failure_has_no_fallback_or_private_exception_details(self):
        self.pipeline.run.side_effect = PipelineError("component_failed", "collector")
        status, body = self.post({"request_id": "x", "text": "question"})
        self.assertEqual(status, 502)
        self.assertEqual(body, {"status": "error", "stage": "collector", "reason_code": "component_failed"})
        self.runtime.handle.assert_not_called()

    def test_unexpected_component_failure_is_bounded(self):
        self.pipeline.run.side_effect = RuntimeError("PRIVATE_SOURCE_CONTENT")
        status, body = self.post({"request_id": "x", "text": "question"})
        self.assertEqual(status, 500)
        self.assertNotIn("PRIVATE", json.dumps(body))
        self.runtime.handle.assert_not_called()

    def test_duplicate_keys_nonfinite_and_nonobjects_are_rejected_before_execution(self):
        for raw in (b'{"request_id":"x","text":"a","text":"b"}',
                    b'{"request_id":"x","text":NaN}', b'[]', b'{invalid'):
            with self.subTest(raw=raw):
                status, _ = dispatch(self.runtime, "POST", "/retrieve", raw, retrieval_gateway=self.gateway)
                self.assertEqual(status, 400)
        self.pipeline.run.assert_not_called()

    def test_health_describes_server_configuration_without_template_or_paths(self):
        status, body = dispatch(self.runtime, "GET", "/health", retrieval_gateway=self.gateway)
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "local_adapters")
        self.assertEqual(body["modular_retrieval"], {"configured": True, "context_source": "server_configuration",
                         "data_mode": "declared_synthetic", "answer_generation_enabled": False, "public_output_enabled": False})
        self.assertNotIn("synthetic-game", json.dumps(body))
        self.assertNotIn("read_scopes", json.dumps(body))

    def test_existing_chat_route_is_not_redirected_to_modular_retrieval(self):
        self.runtime.handle.return_value = {"status": "held"}
        event = {"message_id": "synthetic", "text": "no trigger"}
        status, body = dispatch(self.runtime, "POST", "/chat", json.dumps(event).encode(), retrieval_gateway=self.gateway)
        self.assertEqual((status, body), (200, {"status": "held"}))
        self.runtime.handle.assert_called_once_with(event)
        self.pipeline.run.assert_not_called()

    def test_serve_requires_both_trusted_retrieval_files_before_starting(self):
        with redirect_stderr(StringIO()), patch("ssr_chat.cli.serve") as serve:
            code = main(["serve", "--retrieval-pipeline-config", "missing"])
        self.assertEqual(code, 2)
        serve.assert_not_called()

    def test_serve_passes_explicit_gateway_to_http_server(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(ModularRetrievalGateway, "from_files", return_value=self.gateway) as configure, patch("ssr_chat.cli.serve") as serve:
                code = main(["serve", "--port", "8776", "--state-dir", directory,
                             "--retrieval-pipeline-config", "trusted-pipeline", "--retrieval-template", "trusted-template"])
            self.assertEqual(code, 0)
            configure.assert_called_once_with("trusted-pipeline", "trusted-template")
            self.assertIs(serve.call_args.kwargs["retrieval_gateway"], self.gateway)

    def test_template_version_and_missing_context_are_rejected(self):
        for template in ({"request": {"schema_version": "1.0.1"}},
                         {"request": {"schema_version": "1.0"}, "trusted_context": {}}):
            with self.subTest(template=template), self.assertRaises(PipelineError): ModularRetrievalGateway(self.pipeline, template)


if __name__ == "__main__":
    unittest.main()
