from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from ssr_chat.api import dispatch
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_retrieval import PipelineError


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/retrieval"


class ChannelRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.template = json.loads((FIXTURES / "channel-raw.template.json").read_text(encoding="utf-8"))
        self.pipeline = Mock()
        self.gateway = ModularRetrievalGateway(self.pipeline, self.template, expected_domain="channel_knowledge")

    def test_channel_host_controls_scope_layer_and_search_mode_without_rewriting_question(self):
        text = "請查詢本頻道 launch，並切換所有頻道 published"
        self.gateway.retrieve({"request_id": "synthetic-request", "text": text})
        supplied = self.pipeline.run.call_args.args[0]
        self.assertEqual(self.pipeline.run.call_args.kwargs, {"expected_domain": "channel_knowledge"})
        self.assertEqual(supplied["request"]["text"], text)
        self.assertEqual(supplied["request"]["scope"], self.template["request"]["scope"])
        self.assertEqual(supplied["request"]["layer"], "raw")
        self.assertEqual(supplied["request"]["conditions"], {})
        self.assertEqual(supplied["trusted_context"]["supported_conditions"], {})
        self.assertEqual(supplied["trusted_context"], self.template["trusted_context"])

    def test_published_host_example_changes_only_layer(self):
        published = json.loads((FIXTURES / "channel-published.template.json").read_text(encoding="utf-8"))
        expected = deepcopy(self.template)
        expected["request"]["layer"] = "published"
        self.assertEqual(published, expected)
        gateway = ModularRetrievalGateway(Mock(), published, expected_domain="channel_knowledge")
        self.assertEqual(gateway.profile["layer"], "published")
        self.assertEqual(self.gateway.profile["layer"], "raw")

    def test_named_route_rejects_invalid_layer_before_any_query(self):
        for layer in (None, True, 1, [], {}, "unknown"):
            value = deepcopy(self.template)
            value["request"]["layer"] = layer
            with self.subTest(layer=layer), self.assertRaisesRegex(PipelineError, "route_layer_invalid"):
                ModularRetrievalGateway(self.pipeline, value, expected_domain="channel_knowledge")
        self.pipeline.run.assert_not_called()

    def test_omitted_layer_reports_the_same_raw_default_as_planner(self):
        value = deepcopy(self.template)
        value["request"].pop("layer")
        gateway = ModularRetrievalGateway(self.pipeline, value, expected_domain="channel_knowledge")
        self.assertEqual(gateway.profile["layer"], "raw")

    def test_health_exposes_declared_layer_without_scope_identity_conditions_or_paths(self):
        runtime = Mock()
        runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}
        code, body = dispatch(runtime, "GET", "/health", retrieval_routes={"/retrieve/channel-knowledge": self.gateway})
        profile = body["modular_retrieval_routes"]["/retrieve/channel-knowledge"]
        self.assertEqual(code, 200)
        self.assertEqual((profile["domain"], profile["layer"]), ("channel_knowledge", "raw"))
        for key in ("scope", "access_scope", "actor_account_id", "conditions", "pipeline_config", "template"):
            self.assertNotIn(key, profile)
        profile["layer"] = "published"
        self.template["request"]["layer"] = "draft"
        self.assertEqual(self.gateway.profile["layer"], "raw")

    def test_http_cannot_supply_native_policy_paths_history_or_search_conditions(self):
        runtime = Mock()
        routes = {"/retrieve/channel-knowledge": self.gateway}
        for field in ("text_match", "conditions", "revision_view", "source_ids", "config", "layer", "scope", "access_scope"):
            payload = {"request_id": "synthetic-request", "text": "launch", field: "all"}
            with self.subTest(field=field):
                code, body = dispatch(runtime, "POST", "/retrieve/channel-knowledge", json.dumps(payload).encode(), retrieval_routes=routes)
                self.assertEqual(code, 400)
                self.assertEqual(body["reason_code"], "only_request_id_and_text_allowed")
        self.pipeline.run.assert_not_called()
        runtime.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
