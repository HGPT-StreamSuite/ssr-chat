from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.cli import main
from ssr_chat.modular_api import ModularRetrievalGateway, load_retrieval_routes
from ssr_chat.modular_retrieval import PipelineError


def template(domain):
    return {"synthetic": True, "request": {"schema_version": "1.0.1", "scope": {"domain": domain}, "layer": "raw"},
            "trusted_context": {"synthetic": True, "actor_account_id": "synthetic-self", "access_scope": {
                "read_scopes": [{"domain": domain}], "write_scopes": []}}}


class RetrievalRouteTests(unittest.TestCase):
    def setUp(self):
        self.chat_pipeline = Mock()
        self.game_pipeline = Mock()
        self.chat_pipeline.run.return_value = {"status": "complete", "source": "synthetic-chat"}
        self.game_pipeline.run.return_value = {"status": "complete", "source": "synthetic-game"}
        self.chat = ModularRetrievalGateway(self.chat_pipeline, template("chat_history"), expected_domain="chat_history")
        self.game = ModularRetrievalGateway(self.game_pipeline, template("game_knowledge"))
        self.routes = {"/retrieve/chat-history": self.chat}
        self.runtime = Mock()
        self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}

    def request(self, path, extra=None):
        payload = {"request_id": "synthetic-request", "text": "我剛剛在聊天頻道打了什麼訊息？", **(extra or {})}
        return dispatch(self.runtime, "POST", path, json.dumps(payload).encode(), retrieval_gateway=self.game, retrieval_routes=self.routes)

    def test_exact_path_selects_its_own_context_without_authority_union(self):
        code, result = self.request("/retrieve/chat-history")
        self.assertEqual((code, result["source"]), (200, "synthetic-chat"))
        chat_request = self.chat_pipeline.run.call_args.args[0]
        self.assertEqual(chat_request["trusted_context"]["access_scope"]["read_scopes"], [{"domain": "chat_history"}])
        self.assertEqual(chat_request["request"]["scope"]["domain"], "chat_history")
        self.game_pipeline.run.assert_not_called()
        code, result = self.request("/retrieve")
        self.assertEqual((code, result["source"]), (200, "synthetic-game"))
        self.assertEqual(self.game_pipeline.run.call_args.args[0]["trusted_context"]["access_scope"]["read_scopes"], [{"domain": "game_knowledge"}])
        self.runtime.handle.assert_not_called()

    def test_unknown_named_route_has_no_legacy_or_chat_fallback(self):
        code, result = self.request("/retrieve/unknown")
        self.assertEqual((code, result["status"]), (503, "not_configured"))
        self.chat_pipeline.run.assert_not_called()
        self.game_pipeline.run.assert_not_called()
        self.runtime.handle.assert_not_called()

    def test_named_route_rejects_client_profile_account_and_scope(self):
        for field in ("profile", "actor_account_id", "session_id", "scope", "access_scope"):
            with self.subTest(field=field):
                code, result = self.request("/retrieve/chat-history", {field: "all"})
                self.assertEqual(code, 400)
                self.assertEqual(result["reason_code"], "only_request_id_and_text_allowed")
        self.chat_pipeline.run.assert_not_called()

    def test_named_route_requires_matching_domain_and_read_only_authority(self):
        cases = []
        wrong_scope = template("game_knowledge"); cases.append(wrong_scope)
        mixed = template("chat_history"); mixed["trusted_context"]["access_scope"]["read_scopes"].append({"domain": "game_knowledge"}); cases.append(mixed)
        writable = template("chat_history"); writable["trusted_context"]["access_scope"]["write_scopes"] = [{"domain": "chat_history"}]; cases.append(writable)
        empty = template("chat_history"); empty["trusted_context"]["access_scope"]["read_scopes"] = []; cases.append(empty)
        malformed = template("chat_history"); malformed["request"]["scope"] = None; cases.append(malformed)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PipelineError): ModularRetrievalGateway(self.chat_pipeline, value, expected_domain="chat_history")

    def test_health_lists_declared_domains_without_identity_or_grants(self):
        _, body = dispatch(self.runtime, "GET", "/health", retrieval_routes=self.routes)
        self.assertEqual(body["modular_retrieval_routes"]["/retrieve/chat-history"]["domain"], "chat_history")
        self.assertFalse(body["modular_retrieval"]["configured"])
        self.assertNotIn("synthetic-self", json.dumps(body))
        self.assertNotIn("read_scopes", json.dumps(body))

    def test_route_loader_resolves_local_paths_and_pins_expected_domain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pipeline.json").write_text("{}")
            (root / "template.json").write_text("{}")
            config = root / "routes.json"
            config.write_text(json.dumps({"schema_version": "1.0.1", "routes": {"/retrieve/chat-history": {
                "domain": "chat_history", "pipeline_config": "pipeline.json", "template": "template.json"}}}))
            with patch.object(ModularRetrievalGateway, "from_files", return_value=self.chat) as configure:
                self.assertEqual(load_retrieval_routes(config), self.routes)
            configure.assert_called_once_with(root / "pipeline.json", root / "template.json", expected_domain="chat_history")

    def test_route_loader_rejects_invalid_routes_versions_and_domain_types(self):
        base = {"schema_version": "1.0.1", "routes": {"/retrieve/chat-history": {
            "domain": "chat_history", "pipeline_config": "unused", "template": "unused"}}}
        cases = [{"schema_version": "1.0.1", "routes": {}}, {**base, "schema_version": "1.0"}]
        for route in ("/retrieve", "/chat", "/retrieve/../chat", "http://example.test"):
            cases.append({"schema_version": "1.0.1", "routes": {route: base["routes"]["/retrieve/chat-history"]}})
        bad_domain = deepcopy(base); bad_domain["routes"]["/retrieve/chat-history"]["domain"] = []; cases.append(bad_domain)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.json"
            for value in cases:
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(PipelineError): load_retrieval_routes(path)

    def test_cli_wires_only_the_explicit_route_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("ssr_chat.modular_api.load_retrieval_routes", return_value=self.routes) as load, patch("ssr_chat.cli.serve") as serve:
                code = main(["serve", "--state-dir", directory, "--retrieval-routes-config", "trusted-routes.json"])
            self.assertEqual(code, 0)
            load.assert_called_once_with("trusted-routes.json")
            self.assertIs(serve.call_args.kwargs["retrieval_routes"], self.routes)
            self.assertIsNone(serve.call_args.kwargs["retrieval_gateway"])


if __name__ == "__main__":
    unittest.main()
