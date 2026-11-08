"""Pure mocked pipeline checks; owned evidence fixtures, no TEMP or cleanup.

Fake registrations and child results exercise orchestration guards only. They
are not native execution or authoritative source acceptance evidence.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from ssr_chat import game_name_pipeline as pipeline


class GameNamePipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owned = (pipeline.WORKSPACE / "evidence" / "development" /
                     "20261106-game-name-mocked-unit" / uuid4().hex)
        cls.owned.mkdir(parents=True, exist_ok=False)

    def setUp(self):
        self.case = self.owned / uuid4().hex
        self.case.mkdir(exist_ok=False)
        self.pins = {"ssr-chat/ssr_chat/game_name_pipeline.py": "a" * 64}
        self.scope = {"domain": "game_knowledge", "platform": "pc", "channel_id": None,
                      "game_id": "synthetic-unit-arena", "session_id": None, "content_version": "v1"}
        self.access = {"read_scopes": [{"domain": "game_knowledge", **{
            key: {"mode": "exact", "value": value}
            for key, value in self.scope.items() if key != "domain"
        }}], "write_scopes": []}
        self.payload = {"request_id": "synthetic-unit-request", "text": "Unregistered Name"}
        self.profile = {"source_id": "synthetic-unit-source", "scope": deepcopy(self.scope)}
        self.config = {
            "schema_version": "1.0.1", "profile": pipeline.PROFILE, "synthetic": True,
            "source_id": self.profile["source_id"], "source_files_sha256": deepcopy(self.pins),
            "template": {"synthetic": True, "request": {"conditions": {}},
                         "trusted_context": {"synthetic": True}},
            **{name: str(self.case / (name + ".input")) for name in pipeline.DATA_KEYS},
        }

    def save_bytes(self, name, raw):
        path = self.case / name
        with path.open("xb") as stream:
            stream.write(raw)
        return path

    def execute(self, *, config=None, payload=None, after=None, children=None):
        config = self.config if config is None else config
        payload = self.payload if payload is None else payload
        config_path = self.save_bytes("config.json", pipeline.encoded(config))
        input_path = self.save_bytes("request.json", pipeline.encoded(payload))
        child_behavior = children if children is not None else AssertionError("native process forbidden")
        with patch.object(pipeline, "source_files", side_effect=[self.pins, self.pins if after is None else after]) as sources, \
                patch.object(pipeline, "_validate_data", return_value=(self.profile, {}, {})) as preflight, \
                patch.object(pipeline.subprocess, "run", side_effect=child_behavior) as run:
            report, code = pipeline.execute(config_path, input_path, self.case / "execution")
        self.assertEqual(sources.call_count, 2)
        return report, code, run, preflight

    def native_replies(self):
        query = {"schema_version": "1.0.1", **self.payload, "intent": "game_lookup",
                 "scope": deepcopy(self.scope), "actor_account_id": None, "person_id": None,
                 "time_from": None, "time_to": None, "exclude_event_ids": [], "limit": 5,
                 "layer": "raw", "conditions": {}, "access_scope": deepcopy(self.access)}
        plan = {"schema_version": "1.0.1", "request_id": self.payload["request_id"],
                "tasks": [{"task_id": "game-1", "domain": "game_knowledge", "query": query, "depends_on": []}],
                "missing_inputs": [], "skipped_domains": []}
        result = {"schema_version": "1.0.1", "request_id": self.payload["request_id"],
                  "domain": "game_knowledge", "status": "no_match", "evidence": [],
                  "reason_code": "no_eligible_match", "source_kind": ["synthetic"],
                  "missing_fields": [], "blocked_by": [], "execution": {"mode": "independent",
                  "elapsed_ms": None, "attempted_sources": [{"source_id": self.profile["source_id"],
                  "source_kind": "synthetic", "status": "no_match", "reason_code": "no_eligible_match"}], "errors": []}}
        return [subprocess.CompletedProcess([], 0, pipeline.encoded(value), b"") for value in (plan, result)]

    def test_source_change_after_mocked_reader_is_held_without_fallback(self):
        changed = {**self.pins, "planner/source/changed.py": "b" * 64}
        report, code, run, _ = self.execute(after=changed, children=self.native_replies())
        self.assertEqual((code, report["status"], report["reason_code"]),
                         (3, "held", "source_changed_during_execution"))
        self.assertFalse(report["source_unchanged"])
        self.assertTrue(report["reader_invoked"])
        self.assertEqual([call["stage"] for call in report["calls"]], ["planner", "reader"])
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("input", call.kwargs)
        self.assertEqual(report["result"]["status"], "no_match")
        self.assertFalse(report["model_invoked"])
        self.assertEqual(report["public_output"], "not_attempted")

    def test_source_pin_mismatch_stops_before_preflight_or_child(self):
        config = deepcopy(self.config)
        config["source_files_sha256"] = {"stale": "b" * 64}
        report, code, run, preflight = self.execute(config=config)
        self.assertEqual((code, report["reason_code"]), (3, "source_pin_mismatch"))
        self.assertFalse(report["reader_invoked"])
        run.assert_not_called()
        preflight.assert_not_called()

    def test_configuration_rejects_arbitrary_command_profile_and_nonsynthetic(self):
        for changes in ({"command": ["arbitrary"]}, {"profile": "automatic"}, {"synthetic": False}):
            with self.subTest(changes=changes):
                self.case = self.owned / uuid4().hex
                self.case.mkdir(exist_ok=False)
                config = {**deepcopy(self.config), **changes}
                report, code, run, preflight = self.execute(config=config)
                self.assertEqual((code, report["reason_code"]), (3, "game_name_configuration_invalid"))
                run.assert_not_called()
                preflight.assert_not_called()

    def test_public_payload_rejects_commands_acl_and_extra_fields_without_child(self):
        for extra in ({"command": "arbitrary"}, {"access_scope": self.access}, {"profile": pipeline.PROFILE}):
            with self.subTest(extra=extra):
                self.case = self.owned / uuid4().hex
                self.case.mkdir(exist_ok=False)
                report, code, run, _ = self.execute(payload={**self.payload, **extra})
                self.assertEqual((code, report["reason_code"]), (3, "only_request_id_and_text_allowed"))
                run.assert_not_called()

    def test_numeric_text_and_template_filters_do_not_spawn_children(self):
        report, code, run, _ = self.execute(payload={**self.payload, "text": "Integration Blade power > 90"})
        self.assertEqual((code, report["reason_code"]), (3, "numeric_text_unsupported"))
        run.assert_not_called()
        self.case = self.owned / uuid4().hex
        self.case.mkdir(exist_ok=False)
        config = deepcopy(self.config)
        config["template"]["request"]["conditions"] = {"filters": []}
        report, code, run, _ = self.execute(config=config)
        self.assertEqual((code, report["reason_code"]), (3, "numeric_filters_not_supported"))
        run.assert_not_called()

    def data_fixture(self, *, variant=None):
        """Mock registration pins isolate semantic preflight from hash guards."""
        record = {"schema_version": "1.0.1", "source_id": self.profile["source_id"],
                  "source_event_id": "synthetic-event", "record_id": "synthetic-record", "revision": 1,
                  "scope": deepcopy(self.scope), "record_kind": "raw_event", "access_scope": deepcopy(self.access),
                  "event_time": "2026-10-01T00:00:00Z", "received_at": "2026-10-01T00:01:00Z",
                  "actor_account_id": "synthetic-author", "text": "Authored synthetic unit data.",
                  "quality_flags": ["synthetic"], "source_refs": [{"source_id": self.profile["source_id"],
                  "record_id": "synthetic-record", "revision": 1, "locator": "synthetic://unit/record",
                  "transformation_version": None}]}
        if variant == "stored_acl_denied":
            record["access_scope"] = {"read_scopes": [], "write_scopes": []}
        exported = [deepcopy(record)]
        rows = [{"name": "Integration Blade", "fields": {"power": 90}, "record": deepcopy(record)}]
        metadata = [{"name": rows[0]["name"], "fields": deepcopy(rows[0]["fields"]),
                     "record": deepcopy(record), "record_sha256": pipeline.sha(pipeline.encoded(record))}]
        handoffs = [{"record": deepcopy(record), "acquisition": {"policy": {"uses": {
            "search_index": {"status": "allowed", "basis": "Authored synthetic unit fixture only"}}}}}]
        manifest = {"contract_version": "1.0.1", "applicable_dimensions": ["domain", "platform", "game_id", "content_version"],
                    "field_types": {"power": "number"}, "source_policies": {self.profile["source_id"]: {
                    "source_kind": "synthetic", "scopes": [deepcopy(self.scope)], "allowed_layers": ["raw"],
                    "query_allowed": True, "collected": True, "reason_code": None, "access_scope": deepcopy(self.access)}}}
        base_context = {"access_scope": deepcopy(self.access), "allowed_layers": ["raw"]}
        context = deepcopy(base_context)
        if variant == "missing_snapshot":
            rows = []
        elif variant == "extra_snapshot":
            extra = deepcopy(rows[0])
            extra["record"].update(record_id="foreign-record", source_event_id="foreign-event")
            rows.append(extra)
        elif variant == "changed_snapshot_acl":
            rows[0]["record"]["access_scope"] = {"read_scopes": [], "write_scopes": []}
        elif variant == "evidence_export":
            for field in ("schema_version", "record_kind", "access_scope"):
                exported[0].pop(field)
        elif variant == "context_narrow":
            context["access_scope"]["read_scopes"] = []
            context["allowed_layers"] = []
        elif variant == "context_expand":
            foreign = deepcopy(self.access["read_scopes"][0])
            foreign["game_id"]["value"] = "unregistered-game"
            context["access_scope"]["read_scopes"].append(foreign)
        jsonl = lambda values: b"".join(pipeline.encoded(value) + b"\n" for value in values)
        blobs = {"source_records": jsonl(exported), "snapshot": jsonl(rows), "projection_registry": pipeline.encoded(metadata),
                 "handoffs": jsonl(handoffs), "manifest": pipeline.encoded(manifest), "context": pipeline.encoded(context)}
        config = deepcopy(self.config)
        for name, raw in blobs.items():
            config[name] = str(self.save_bytes(name + ".data", raw))
        profile = {**deepcopy(self.profile), "files": {name: {"sha256": pipeline.sha(raw)} for name, raw in blobs.items()}}
        authority = {**blobs, "context": pipeline.encoded(base_context)}
        return config, profile, authority, record

    def validate_fixture(self, variant=None):
        config, profile, authority, record = self.data_fixture(variant=variant)
        with patch.object(pipeline, "registered_source", return_value=(profile, authority)), \
                patch.object(pipeline.subprocess, "run", side_effect=AssertionError("preflight must not spawn")):
            result = pipeline._validate_data(config, self.case / "config.json")
        return result, record

    def test_preflight_matches_complete_original_export_and_registered_projection(self):
        (_, records, hashes), original = self.validate_fixture()
        self.assertEqual(list(records.values()), [original])
        self.assertEqual(len(hashes), len(pipeline.DATA_KEYS))

    def test_preflight_preserves_stored_acl_instead_of_borrowing_caller_grant(self):
        (_, records, _), original = self.validate_fixture("stored_acl_denied")
        self.assertEqual(list(records.values()), [original])
        self.assertEqual(original["access_scope"]["read_scopes"], [])
        self.assertTrue(self.access["read_scopes"])

    def test_preflight_rejects_missing_and_extra_snapshot_records(self):
        for variant, reason in (("missing_snapshot", "snapshot_export_incomplete"),
                                ("extra_snapshot", "snapshot_export_identity_mismatch")):
            with self.subTest(variant=variant):
                self.case = self.owned / uuid4().hex
                self.case.mkdir(exist_ok=False)
                with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
                    self.validate_fixture(variant)

    def test_preflight_rejects_changed_record_acl_and_evidence_only_export(self):
        for variant, reason in (("changed_snapshot_acl", "original_record_or_projection_changed"),
                                ("evidence_export", "full_original_record_required")):
            with self.subTest(variant=variant):
                self.case = self.owned / uuid4().hex
                self.case.mkdir(exist_ok=False)
                with self.assertRaisesRegex(ValueError, "^" + reason + "$"):
                    self.validate_fixture(variant)

    def test_preflight_context_can_narrow_but_cannot_expand(self):
        self.validate_fixture("context_narrow")
        self.case = self.owned / uuid4().hex
        self.case.mkdir(exist_ok=False)
        with self.assertRaisesRegex(ValueError, "^context_profile_mismatch$"):
            self.validate_fixture("context_expand")


if __name__ == "__main__":
    unittest.main()
