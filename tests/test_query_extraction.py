from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

import test_natural_routing as natural_tests
from ssr_chat.api import dispatch
from ssr_chat.modular_api import ModularRetrievalGateway
from ssr_chat.modular_entry import COMMANDS, EntryGateway
from ssr_chat.modular_extraction import EntityQueryExtractor, validate_extraction
from ssr_chat.modular_retrieval import PipelineError


class QueryExtractionTests(unittest.TestCase):
    def setUp(self):
        self.host = natural_tests.NaturalRoutingTests(); self.host.setUp()
        self.host.questions.update({"/game": "請找出 Aurora Blade gameplay records", "/channel": "請問本頻道公告 launch 幾點？"})
        self.scopes = {}
        for command, domain in COMMANDS.items():
            scope = {"domain": domain, "platform": "synthetic", "channel_id": "room", "game_id": "game" if command == "/game" else None,
                     "session_id": None, "content_version": "v1"}
            self.scopes[command] = scope
            self.host.providers[command].trusted_answer_context = {"scope": scope}
        self.config = deepcopy(self.host.config)
        self.config["query_extraction"] = {"schema_version": "1.0.1", "routes": {
            "/game": {"rule": "registered_gameplay_records_v1", "entities": [{"id": "aurora", "name": "Aurora Blade"}, {"id": "echo", "name": "Echo Blade"}]},
            "/channel": {"rule": "registered_channel_announcement_v1", "entities": [{"id": "launch", "name": "launch"}]}}}
        self.targets = self.host.entry._targets
        self.extractor = EntityQueryExtractor(self.config["query_extraction"], self.targets)

    def test_original_examples_extract_complete_registered_substrings(self):
        for command, expected in (("/game", "Aurora Blade"), ("/channel", "launch")):
            text = self.host.questions[command]
            record, decision = self.extractor.extract(command, text)
            self.assertEqual(decision["status"], "selected")
            self.assertEqual(record.search_text, expected)
            self.assertEqual(text[record.span_start:record.span_end], expected)
            validate_extraction(record, text, self.scopes[command])

    def test_different_entity_is_not_forced_to_previous_subject(self):
        record, _ = self.extractor.extract("/game", "請找出 Echo Blade gameplay records")
        self.assertEqual((record.entity_id, record.search_text), ("echo", "Echo Blade"))
        record, decision = self.extractor.extract("/game", "請找出 Unknown Blade gameplay records")
        self.assertIsNone(record); self.assertEqual(decision["reason_code"], "query_extraction_unresolved")

    def test_unknown_shape_or_multiple_entities_is_never_guessed(self):
        for text in ("Aurora Blade gameplay records", "請找出 Aurora Blade Echo Blade gameplay records", "請找出 Aurora Blade", "請找出 Aurora Blade gameplay records 然後 launch"):
            self.assertIsNone(self.extractor.extract("/game", text)[0])

    def test_actual_topic_can_itself_be_gameplay_or_announcement(self):
        config = deepcopy(self.config["query_extraction"])
        config["routes"]["/game"]["entities"].append({"id": "literal-gameplay", "name": "gameplay"})
        config["routes"]["/channel"]["entities"].append({"id": "literal-announcement", "name": "公告"})
        extractor = EntityQueryExtractor(config, self.targets)
        self.assertEqual(extractor.extract("/game", "請找出 gameplay gameplay records")[0].search_text, "gameplay")
        self.assertEqual(extractor.extract("/channel", "請問本頻道公告 公告 幾點？")[0].search_text, "公告")

    def test_longer_registered_topic_creates_ambiguity_instead_of_word_deletion(self):
        config = deepcopy(self.config["query_extraction"])
        config["routes"]["/game"]["entities"].append({"id": "long-game", "name": "Aurora Blade gameplay"})
        config["routes"]["/channel"]["entities"].append({"id": "long-channel", "name": "公告 launch"})
        extractor = EntityQueryExtractor(config, self.targets)
        for command in ("/game", "/channel"):
            text = self.host.questions[command]
            for question in (text, text.replace(" gameplay", "  gameplay") if command == "/game" else text.replace(" launch", "  launch")):
                record, decision = extractor.extract(command, question)
                self.assertIsNone(record); self.assertEqual(decision["reason_code"], "query_extraction_ambiguous")

    def test_registry_is_snapshot_with_unique_ids_and_names(self):
        self.config["query_extraction"]["routes"]["/game"]["entities"][0]["name"] = "changed"
        self.assertEqual(self.extractor.extract("/game", self.host.questions["/game"])[0].search_text, "Aurora Blade")
        for entity in ({"id": "other", "name": "aurora blade"}, {"id": "aurora", "name": "Other"}, {"id": "bad", "name": ""}):
            config = deepcopy(self.config["query_extraction"])
            config["routes"]["/game"]["entities"] = [{"id": "aurora", "name": "Aurora Blade"}, entity]
            with self.assertRaises(PipelineError): EntityQueryExtractor(config, self.targets)

    def test_wrong_scope_changed_origin_or_forged_span_is_rejected(self):
        text = self.host.questions["/game"]; record, _ = self.extractor.extract("/game", text)
        for invalid in (replace(record, scope_sha256="0" * 64), replace(record, search_text="Echo Blade"), replace(record, original_text="changed"),
                        replace(record, span_start=-1), replace(record, rule="registered_channel_announcement_v1")):
            with self.assertRaises(PipelineError): validate_extraction(invalid, text, self.scopes["/game"])

    def test_entry_preserves_original_payload_and_passes_only_internal_record(self):
        gateway = self.host.gateways["/game"]
        def respond(payload, *, extraction):
            self.assertEqual(payload["text"], self.host.questions["/game"])
            self.assertEqual(extraction.search_text, "Aurora Blade")
            return {"status": "complete", "request_id": payload["request_id"], "public_output": "not_attempted", "answer_generation": "not_run",
                    "collection": {"partial": False, "evidence": [{"text": "synthetic"}]}}
        gateway.retrieve.side_effect = respond
        entry = EntryGateway(self.config, self.host.retrieval, self.host.answers, self.host.trace)
        result = entry.query({"request_id": "synthetic", "text": self.host.questions["/game"]})
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(self.host.trace[-2]["phase"], "selected")
        self.assertEqual(result["query_extraction"]["original_text"], self.host.questions["/game"])

    def test_extraction_ambiguity_stops_before_selected_or_data_gateway(self):
        self.config["query_extraction"]["routes"]["/game"]["entities"].append({"id": "long", "name": "Aurora Blade gameplay"})
        entry = EntryGateway(self.config, self.host.retrieval, self.host.answers, self.host.trace)
        result = entry.query({"request_id": "synthetic", "text": self.host.questions["/game"]})
        self.assertEqual(result["reason_code"], "query_extraction_ambiguous")
        self.assertFalse(any(row["phase"] == "selected" for row in self.host.trace)); self.host.no_targets()

    def test_explicit_command_bypasses_extraction_and_registry(self):
        entry = EntryGateway(self.config, self.host.retrieval, self.host.answers, self.host.trace)
        result = entry.query({"request_id": "synthetic", "text": "/game Aurora Blade"})
        self.assertNotIn("query_extraction", result)
        self.assertEqual(self.host.gateways["/game"].retrieve.call_args.kwargs, {})

    def test_gateway_builds_authentic_search_request_without_changing_authority(self):
        scope = self.scopes["/game"]
        template = {"synthetic": True, "request": {"schema_version": "1.0.1", "scope": scope, "layer": "raw", "limit": 5,
                    "time_from": None, "conditions": {}}, "trusted_context": {"synthetic": True, "actor_account_id": "host",
                    "access_scope": {"read_scopes": [{"domain": "game_knowledge"}], "write_scopes": []}}}
        pipeline = Mock(); report = {"status": "incomplete", "collection": {"partial": True, "reason_code": "source_failed"}}
        pipeline.run.return_value = report
        gateway = ModularRetrievalGateway(pipeline, template, expected_domain="game_knowledge")
        record, _ = self.extractor.extract("/game", self.host.questions["/game"])
        result = gateway.retrieve({"request_id": "synthetic", "text": record.original_text}, extraction=record)
        actual = pipeline.run.call_args.args[0]
        self.assertEqual(actual["request"]["text"], "Aurora Blade")
        self.assertEqual(actual["trusted_context"], template["trusted_context"])
        self.assertEqual({key: value for key, value in actual["request"].items() if key not in ("text", "request_id")}, template["request"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["original_question"], record.original_text)
        self.assertEqual(result["query_extraction"], record.as_dict())

    def test_scope_mismatch_is_rejected_before_any_search_plan(self):
        template = {"request": {"schema_version": "1.0.1", "scope": {**self.scopes["/game"], "game_id": "other"}},
                    "trusted_context": {"access_scope": {"read_scopes": [{"domain": "game_knowledge"}], "write_scopes": []}}}
        pipeline = Mock(); gateway = ModularRetrievalGateway(pipeline, template, expected_domain="game_knowledge")
        record, _ = self.extractor.extract("/game", self.host.questions["/game"])
        with self.assertRaises(PipelineError): gateway.retrieve({"request_id": "synthetic", "text": record.original_text}, extraction=record)
        pipeline.run.assert_not_called()

    def test_client_cannot_supply_search_text_registry_or_extraction(self):
        entry = EntryGateway(self.config, self.host.retrieval, self.host.answers, self.host.trace)
        for field in ("search_text", "query_extraction", "entities", "registry_sha256", "scope_sha256"):
            code, _ = dispatch(self.host.runtime, "POST", "/query", json.dumps({"request_id": "synthetic", "text": self.host.questions["/game"], field: "all"}).encode(), entry_gateway=entry)
            self.assertEqual(code, 400)
        self.host.no_targets()

    def test_extraction_cannot_be_silently_enabled_in_old_command_mode(self):
        self.config["routing"] = "explicit_command_v1"
        with self.assertRaises(PipelineError): EntryGateway(self.config, self.host.retrieval, self.host.answers, self.host.trace)


if __name__ == "__main__": unittest.main()
