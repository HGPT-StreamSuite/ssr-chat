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
from ssr_chat.modular_api import RetrievalInputError
from ssr_chat.modular_entry import COMMANDS, EntryGateway
from ssr_chat.modular_retrieval import PipelineError


class EntryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.trace = []
        self.retrieval, self.answers, self.targets = {}, {}, {}
        for command, domain in COMMANDS.items():
            gateway = Mock()
            operation = "retrieve" if command == "/game" else "answer"
            route = ("/retrieve/" if operation == "retrieve" else "/answer/") + domain.replace("_", "-")
            gateway.profile = {"domain": domain, "layer": "raw", "public_output_enabled": False,
                "data_mode": "declared_synthetic" if operation == "retrieve" else "synthetic_only", "citations_required": True,
                "private_binding": "PRIVATE_ACTOR_AND_PATH"}
            def respond(payload, domain=domain, operation=operation):
                self.assertEqual(self.trace[-1]["phase"], "selected")
                result = {"request_id": payload["request_id"], "public_output": "not_attempted"}
                if operation == "retrieve":
                    result.update(status="complete", collection={"partial": False, "evidence": [{"text": "synthetic"}]}, answer_generation="not_run")
                else:
                    result.update(status="answered", model_invoked=True, answer=domain + " [E1]", cited_evidence_ids=["E1"])
                return result
            getattr(gateway, operation).side_effect = respond
            (self.retrieval if operation == "retrieve" else self.answers)[route] = gateway
            self.targets[command] = route
        self.config = {"schema_version": "1.0.1", "routing": "explicit_command_v1", "targets": self.targets}
        self.entry = EntryGateway(self.config, self.retrieval, self.answers, self.trace)
        self.runtime = Mock(); self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}

    def query(self, text, entry=None):
        return (entry or self.entry).query({"request_id": "synthetic-request", "text": text})

    def assert_no_targets(self):
        for target in [*self.retrieval.values(), *self.answers.values()]:
            target.retrieve.assert_not_called(); target.answer.assert_not_called()

    def test_each_command_calls_only_its_bound_target_and_keeps_question(self):
        for command, domain in COMMANDS.items():
            question = "合成問句：\n保留原始換行？"
            result = self.query(command + " " + question)
            route = self.targets[command]
            target = (self.retrieval if command == "/game" else self.answers)[route]
            operation = "retrieve" if command == "/game" else "answer"
            getattr(target, operation).assert_called_once_with({"request_id": "synthetic-request", "text": question})
            self.assertEqual((result["domain"], result["operation"]), (domain, operation))
            self.assertEqual(result["status"], "evidence_ready" if command == "/game" else "answered")
            self.assertEqual(self.trace[-2]["original_text"], command + " " + question)
            for other in [*self.retrieval.values(), *self.answers.values()]:
                if other is not target: other.retrieve.assert_not_called(); other.answer.assert_not_called()
            target.reset_mock()

    def test_bare_unknown_empty_and_chained_commands_hold_before_execution(self):
        for text in ("查詢我的逐字稿", "/unknown question", "/game", "/game  ", "/channel q\n/transcript q", "/game q /history q"):
            self.assertEqual(self.query(text)["status"], "held")
        self.assert_no_targets()

    def test_unconfigured_command_does_not_try_another_domain(self):
        config = deepcopy(self.config); config["targets"] = {"/game": self.targets["/game"]}
        entry = EntryGateway(config, self.retrieval, self.answers, self.trace)
        self.assertEqual(self.query("/history question", entry)["reason_code"], "entry_target_not_configured")
        self.assert_no_targets()

    def test_invalid_identity_text_and_extra_authority_are_rejected(self):
        values = [None, [], {}, {"request_id": "", "text": "/game q"}, {"request_id": True, "text": "/game q"},
                  {"request_id": "x" * 201, "text": "/game q"}, {"request_id": "x", "text": " "},
                  {"request_id": "x", "text": True}, {"request_id": "x", "text": "x" * 16385}]
        for value in values:
            with self.assertRaises(RetrievalInputError): self.entry.query(value)
        for field in ("domain", "route", "mode", "target_route", "scope", "actor_account_id", "access_scope", "ai_input", "layer", "time_from"):
            code, body = dispatch(self.runtime, "POST", "/query", json.dumps({"request_id": "x", "text": "/game q", field: "all"}).encode(), entry_gateway=self.entry)
            self.assertEqual((code, body["reason_code"]), (400, "only_request_id_and_text_allowed"))
        self.assert_no_targets(); self.runtime.handle.assert_not_called()

    def test_unconfigured_http_entry_has_no_legacy_fallback(self):
        code, body = dispatch(self.runtime, "POST", "/query", b'{"request_id":"x","text":"/game q"}')
        self.assertEqual((code, body["reason_code"]), (503, "modular_entry_not_configured"))
        self.runtime.handle.assert_not_called(); self.assert_no_targets()

    def test_duplicate_nonfinite_and_nonobject_json_cannot_select_target(self):
        for raw in (b'{"request_id":"x","text":"/game q","text":"/history q"}', b'{"request_id":"x","text":NaN}', b'[]', b'{bad'):
            code, _ = dispatch(self.runtime, "POST", "/query", raw, entry_gateway=self.entry)
            self.assertEqual(code, 400)
        self.assert_no_targets(); self.runtime.handle.assert_not_called()

    def test_downstream_purpose_denial_is_preserved_without_other_target(self):
        target = self.answers[self.targets["/history"]]
        target.answer.side_effect = None
        target.answer.return_value = {"request_id": "synthetic-request", "status": "held", "answer": None,
            "model_invoked": False, "reason_code": "ai_input_not_allowed", "public_output": "not_attempted"}
        result = self.query("/history own question")
        self.assertEqual(result["reason_code"], "ai_input_not_allowed")
        self.assertFalse(result["model_invoked"]); self.assertIs(result["target_result"], target.answer.return_value)
        self.retrieval[self.targets["/game"]].retrieve.assert_not_called()

    def test_partial_and_no_match_retrieval_remain_distinct(self):
        target = self.retrieval[self.targets["/game"]]; target.retrieve.side_effect = None
        base = {"request_id": "synthetic-request", "answer_generation": "not_run", "public_output": "not_attempted"}
        target.retrieve.return_value = {**base, "status": "incomplete", "collection": {"partial": True, "blocked_by": ["synthetic-denial"]}}
        result = self.query("/game question")
        self.assertEqual((result["status"], result["reason_code"]), ("held", "retrieval_incomplete"))
        self.assertEqual(result["target_result"]["collection"]["blocked_by"], ["synthetic-denial"])
        target.retrieve.return_value = {**base, "status": "complete", "collection": {"partial": False, "evidence": []}}
        self.assertEqual(self.query("/game question")["status"], "no_match")
        for target in self.answers.values(): target.answer.assert_not_called()

    def test_target_exception_has_no_retry_and_model_invocation_is_unknown(self):
        target = self.answers[self.targets["/transcript"]]
        for error in (PipelineError("route_domain_mismatch", "planner"), RuntimeError("PRIVATE_ERROR")):
            target.answer.side_effect = error
            result = self.query("/transcript question")
            self.assertEqual(result["reason_code"], "entry_target_failed")
            self.assertIsNone(result["model_invoked"])
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(target.answer.call_count, 2)
        self.runtime.handle.assert_not_called()

    def test_wrong_request_or_public_output_result_is_not_accepted(self):
        target = self.retrieval[self.targets["/game"]]; target.retrieve.side_effect = None
        for invalid in ({"request_id": "other", "public_output": "not_attempted"}, {"request_id": "synthetic-request", "public_output": "attempted"}):
            target.retrieve.return_value = invalid
            self.assertEqual(self.query("/game question")["reason_code"], "entry_target_result_invalid")
        self.runtime.handle.assert_not_called()

    def test_selection_audit_failure_stops_before_retrieval_or_model(self):
        trace = Mock(); trace.append.side_effect = OSError("PRIVATE_DISK_ERROR")
        entry = EntryGateway(self.config, self.retrieval, self.answers, trace)
        code, body = dispatch(self.runtime, "POST", "/query", b'{"request_id":"x","text":"/history q"}', entry_gateway=entry)
        self.assertEqual((code, body["reason_code"]), (500, "modular_entry_failed"))
        self.assert_no_targets(); self.runtime.handle.assert_not_called()

    def test_target_domain_source_mode_citations_and_output_are_validated_at_startup(self):
        target = self.answers[self.targets["/channel"]]
        original = deepcopy(target.profile)
        for field, value in (("domain", "chat_history"), ("data_mode", "operator_configured"), ("public_output_enabled", True),
                             ("citations_required", False), ("layer", "published")):
            target.profile = {**original, field: value}
            with self.assertRaises(PipelineError): EntryGateway(self.config, self.retrieval, self.answers, self.trace)
        self.assert_no_targets()

    def test_unnamed_missing_and_unknown_targets_cannot_be_activated(self):
        for route in ("/retrieve", "/chat", "/answer/missing", 3):
            config = deepcopy(self.config); config["targets"]["/game"] = route
            with self.assertRaises(PipelineError): EntryGateway(config, self.retrieval, self.answers, self.trace)
        config = deepcopy(self.config); config["targets"]["/all"] = self.targets["/game"]
        with self.assertRaises(PipelineError): EntryGateway(config, self.retrieval, self.answers, self.trace)

    def test_configuration_snapshot_cannot_be_mutated_into_another_target(self):
        self.config["targets"]["/game"] = self.targets["/history"]
        self.assertEqual(self.query("/game question")["domain"], "game_knowledge")
        self.answers[self.targets["/history"]].answer.assert_not_called()

    def test_health_reveals_only_safe_command_profiles(self):
        code, body = dispatch(self.runtime, "GET", "/health", entry_gateway=self.entry)
        self.assertEqual(code, 200)
        self.assertEqual(set(body["modular_entry"]["commands"]), set(COMMANDS))
        self.assertNotIn("PRIVATE", json.dumps(body))
        self.assertNotIn("target_route", json.dumps(body))

    def test_existing_chat_protocol_remains_separate(self):
        event = {"message_id": "synthetic", "text": "/history question"}
        dispatch(self.runtime, "POST", "/chat", json.dumps(event).encode(), entry_gateway=self.entry)
        self.runtime.handle.assert_called_once_with(event); self.assert_no_targets()

    def test_invalid_entry_configuration_stops_cli_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "entry.json"; config.write_text(json.dumps(self.config))
            with patch("ssr_chat.cli.serve") as serve, redirect_stderr(StringIO()):
                code = main(["serve", "--state-dir", directory, "--entry-routes-config", str(config)])
            self.assertEqual(code, 2); serve.assert_not_called()


if __name__ == "__main__": unittest.main()
