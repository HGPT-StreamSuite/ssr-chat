from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_classification import NaturalQuestionRouter
from ssr_chat.modular_entry import COMMANDS, EntryGateway
from ssr_chat.modular_retrieval import DOMAINS, ModularRetrievalPipeline, PipelineError


class NaturalRoutingTests(unittest.TestCase):
    def setUp(self):
        self.trace, self.providers, self.gateways = [], {}, {}
        self.retrieval, self.answers = {}, {}
        self.questions = {"/game": "請查詢 gameplay records", "/channel": "本頻道公告 launch 幾點？",
                          "/history": "我剛剛在聊天頻道打了什麼訊息？", "/transcript": "查詢我的逐字稿"}
        targets = {}
        for command, domain in COMMANDS.items():
            gateway, provider = Mock(), Mock()
            operation = "retrieve" if command == "/game" else "answer"
            gateway.profile = {"domain": domain, "layer": "raw", "public_output_enabled": False,
                               "data_mode": "declared_synthetic" if operation == "retrieve" else "synthetic_only", "citations_required": True}
            if operation == "retrieve": provider = gateway
            else: gateway.retrieval = provider
            provider.classification_plan.side_effect = lambda payload, own=domain: self.probe(payload, own)
            def respond(payload, op=operation):
                self.assertEqual(self.trace[-1]["phase"], "selected")
                result = {"request_id": payload["request_id"], "public_output": "not_attempted"}
                if op == "retrieve": result.update(status="complete", collection={"partial": False, "evidence": [{"text": "synthetic"}]}, answer_generation="not_run")
                else: result.update(status="answered", answer="synthetic [E1]", model_invoked=True)
                return result
            getattr(gateway, operation).side_effect = respond
            route = ("/retrieve/" if operation == "retrieve" else "/answer/") + domain.replace("_", "-")
            (self.retrieval if operation == "retrieve" else self.answers)[route] = gateway
            targets[command] = route
            self.providers[command], self.gateways[command] = provider, gateway
        self.config = {"schema_version": "1.0.1", "routing": "explicit_or_planner_v1", "targets": targets}
        self.entry = EntryGateway(self.config, self.retrieval, self.answers, self.trace)
        self.runtime = Mock(); self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}

    def probe(self, payload, own, selected=None, *, missing=False, exit_code=None):
        selected = selected or next((domain for command, domain in COMMANDS.items() if payload["text"] == self.questions[command]), None)
        if selected is None and payload["text"].startswith("Bob"): selected = "chat_history"
        tasks, skipped = [], []
        for domain in sorted(DOMAINS):
            if domain == selected and own == selected and not missing:
                tasks.append({"task_id": domain + "-1", "domain": domain, "depends_on": [], "query": {
                    "schema_version": "1.0.1", "request_id": payload["request_id"], "text": payload["text"],
                    "scope": {"domain": domain}, "actor_account_id": "BOUND_ACTOR", "access_scope": "PRIVATE_SCOPE"}})
            else:
                skipped.append({"domain": domain, "reason": "missing_input:required_context" if domain == selected else "no_routing_signal"})
        plan = {"schema_version": "1.0.1", "request_id": payload["request_id"], "tasks": tasks, "skipped_domains": skipped,
                "missing_inputs": ["synthetic_context"] if selected and (own != selected or missing) else []}
        code = exit_code if exit_code is not None else (2 if plan["missing_inputs"] else 0)
        return {"plan": plan, "stages": [{"stage": "planner", "exit_code": code}]}

    def ask(self, text, entry=None): return (entry or self.entry).query({"request_id": "synthetic", "text": text})

    def no_targets(self):
        for gateway in self.gateways.values(): gateway.retrieve.assert_not_called(); gateway.answer.assert_not_called()

    def test_each_unambiguous_question_reaches_only_its_original_gateway(self):
        for command, question in self.questions.items():
            result = self.ask(question)
            self.assertEqual(result["command"], command)
            self.assertEqual(result["classification"]["status"], "selected")
            operation = "retrieve" if command == "/game" else "answer"
            getattr(self.gateways[command], operation).assert_called_once_with({"request_id": "synthetic", "text": question})
            self.assertEqual(self.trace[-2]["forwarded_request"]["text"], question)
            for other, gateway in self.gateways.items():
                if other != command: gateway.retrieve.assert_not_called(); gateway.answer.assert_not_called()
            self.gateways[command].reset_mock()

    def test_explicit_commands_do_not_invoke_classifier(self):
        for command in COMMANDS:
            self.ask(command + " " + self.questions[command])
        for provider in self.providers.values(): provider.classification_plan.assert_not_called()

    def test_legacy_mode_keeps_natural_text_closed(self):
        config = deepcopy(self.config); config["routing"] = "explicit_command_v1"
        result = self.ask(self.questions["/transcript"], EntryGateway(config, self.retrieval, self.answers, self.trace))
        self.assertEqual(result["reason_code"], "entry_command_required")
        for provider in self.providers.values(): provider.classification_plan.assert_not_called()
        self.no_targets()

    def test_unknown_or_malformed_slash_commands_never_use_natural_fallback(self):
        for question in ("/unknown question", "/game", " /game question", "查詢我的逐字稿 /history question"):
            self.assertEqual(self.ask(question)["status"], "held")
        for provider in self.providers.values(): provider.classification_plan.assert_not_called()
        self.no_targets()

    def test_unknown_single_question_never_defaults_to_game(self):
        result = self.ask("Hello there")
        self.assertEqual(result["reason_code"], "entry_classification_unknown")
        self.assertEqual(result["classification"]["candidate_domains"], [])
        self.no_targets()

    def test_compound_questions_stop_before_plan_probes(self):
        for question in ("本頻道公告幾點？也查詢我的逐字稿。", "查詢我的逐字稿\n查詢聊天歷史", "gameplay and channel rules?"):
            self.assertEqual(self.ask(question)["reason_code"], "entry_compound_question")
        for provider in self.providers.values(): provider.classification_plan.assert_not_called()
        self.no_targets()

    def test_multiple_domain_signals_never_choose_the_remaining_allowed_domain(self):
        for command in ("/game", "/channel"):
            domain = COMMANDS[command]
            self.providers[command].classification_plan.side_effect = lambda payload, own=domain: self.probe(payload, own, own)
        result = self.ask("gameplay channel rules?")
        self.assertEqual(result["reason_code"], "entry_classification_ambiguous")
        self.no_targets()

    def test_missing_or_denied_planning_context_holds_before_domain_execution(self):
        provider = self.providers["/channel"]
        for code in (2, 3):
            provider.classification_plan.side_effect = lambda payload, code=code: self.probe(payload, "channel_knowledge", "channel_knowledge", missing=True, exit_code=code)
            self.assertEqual(self.ask(self.questions["/channel"])["reason_code"], "entry_classification_incomplete")
        self.no_targets()

    def test_third_person_does_not_become_host_actor_even_if_planner_says_eligible(self):
        result = self.ask("Bob 剛剛在聊天頻道打了什麼訊息？")
        self.assertEqual(result["reason_code"], "entry_personal_question_unresolved")
        self.assertTrue(self.trace[0]["classification"]["probes"]["/history"]["eligible"])
        self.no_targets()

    def test_one_classifier_failure_prevents_all_fallbacks(self):
        for error in (PipelineError("component_timeout", "planner"), RuntimeError("PRIVATE_ERROR")):
            self.providers["/game"].classification_plan.side_effect = error
            result = self.ask(self.questions["/channel"])
            self.assertEqual(result["reason_code"], "entry_classifier_failed")
            self.assertNotIn("PRIVATE", json.dumps(result))
        self.no_targets()

    def test_malformed_plan_identity_and_domain_inventory_fail_closed(self):
        original = self.providers["/channel"].classification_plan.side_effect
        for case in ("request", "text", "version", "inventory", "exit", "dependency"):
            def invalid(payload, case=case):
                probe = original(payload)
                if case == "request": probe["plan"]["request_id"] = "other"
                elif case == "text": probe["plan"]["tasks"][0]["query"]["text"] = "changed"
                elif case == "version": probe["plan"]["schema_version"] = "1.0"
                elif case == "inventory": probe["plan"]["skipped_domains"].pop()
                elif case == "exit": probe["stages"][0]["exit_code"] = True
                else: probe["plan"]["tasks"][0]["depends_on"] = ["identity-needed"]
                return probe
            self.providers["/channel"].classification_plan.side_effect = invalid
            expected = "entry_classification_incomplete" if case == "dependency" else "entry_classifier_failed"
            self.assertEqual(self.ask(self.questions["/channel"])["reason_code"], expected)
        self.no_targets()

    def test_downstream_ai_denial_remains_after_successful_classification(self):
        target = self.gateways["/channel"]; target.answer.side_effect = None
        target.answer.return_value = {"request_id": "synthetic", "status": "held", "answer": None, "model_invoked": False,
                                      "reason_code": "ai_input_not_allowed", "public_output": "not_attempted"}
        result = self.ask(self.questions["/channel"])
        self.assertEqual(result["classification"]["status"], "selected")
        self.assertEqual(result["reason_code"], "ai_input_not_allowed")
        self.assertFalse(result["model_invoked"])

    def test_http_classification_summary_omits_other_route_contexts(self):
        result = self.ask("Bob 剛剛在聊天頻道打了什麼訊息？")
        self.assertNotIn("BOUND_ACTOR", json.dumps(result))
        self.assertNotIn("PRIVATE_SCOPE", json.dumps(result))
        self.assertIn("BOUND_ACTOR", json.dumps(self.trace))

    def test_classified_audit_failure_prevents_target_calls(self):
        entry = EntryGateway(self.config, self.retrieval, self.answers, Mock())
        entry.trace.append.side_effect = OSError("PRIVATE_DISK_ERROR")
        code, body = dispatch(self.runtime, "POST", "/query", json.dumps({"request_id": "synthetic", "text": self.questions["/channel"]}).encode(), entry_gateway=entry)
        self.assertEqual((code, body["reason_code"]), (500, "modular_entry_failed"))
        self.no_targets(); self.runtime.handle.assert_not_called()

    def test_gateway_removes_only_domain_hint_and_never_mutates_authority(self):
        template = {"synthetic": True, "request": {"schema_version": "1.0.1", "scope": {"domain": "chat_history", "session_id": "host-session"}, "layer": "raw", "limit": 1},
                    "trusted_context": {"synthetic": True, "actor_account_id": "host-account", "access_scope": {"read_scopes": [{"domain": "chat_history"}], "write_scopes": []}}}
        original = deepcopy(template); pipeline = Mock()
        gateway = ModularRetrievalGateway(pipeline, template, expected_domain="chat_history")
        payload = {"request_id": "synthetic", "text": self.questions["/history"]}
        gateway.classification_plan(payload)
        request = pipeline.plan_only.call_args.args[0]
        self.assertIsNone(request["request"]["scope"]["domain"])
        self.assertEqual(request["request"]["scope"]["session_id"], "host-session")
        self.assertEqual(request["trusted_context"], original["trusted_context"])
        request["trusted_context"]["access_scope"]["read_scopes"].clear()
        gateway.retrieve(payload)
        self.assertEqual(pipeline.run.call_args.args[0]["trusted_context"], original["trusted_context"])
        self.assertEqual(template, original)

    def test_pipeline_plan_only_invokes_no_dispatcher_or_collector(self):
        config = Mock(); config.planner_repo = Path("trusted-planner")
        pipeline = ModularRetrievalPipeline(config)
        request = {"request": {"schema_version": "1.0.1", "request_id": "synthetic"}, "trusted_context": {}}
        plan = {"schema_version": "1.0.1", "request_id": "synthetic", "tasks": []}
        with patch.object(pipeline, "_invoke", return_value=plan) as invoke:
            result = pipeline.plan_only(request)
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.args[0], "planner")
        self.assertEqual(invoke.call_args.args[1], config.planner_repo)
        self.assertIs(result["plan"], plan)


if __name__ == "__main__": unittest.main()
