"""Pure validator/fault injection tests; subprocesses are never run here.

The separate r2 native matrix exercises the fixed provider CLI. These unit
tests only read the registered synthetic reference and use new owned evidence
folders for injected process/source failures; they never touch the source DB.
"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from ssr_chat import chat_store_binding as b


class ChatStoreBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = b.bundle(b.HOST_PROFILE)
        cls.base_query = b.read(b.provider_file("interfaces/record-get-query.synthetic.json"))

    def setUp(self):
        self.material = deepcopy(self.base)
        self.query = deepcopy(self.base_query)
        self.receipt = deepcopy(self.material["reference"])

    def rejected(self, code, change):
        change()
        with self.assertRaises(b.BindingError) as raised:
            b.validate_receipt(self.receipt, self.query, self.material, code)
        return raised.exception.code

    def test_registered_reference_and_canonical_hashes(self):
        b.validate_query(self.query, self.material)
        b.validate_receipt(self.receipt, self.query, self.material, 0)
        self.assertEqual(b.digest(self.receipt["records"][0]), self.material["profile"]["record_sha256"])

    def test_duplicate_json_and_nonfinite_are_rejected(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                b.load(raw)

    def test_public_authority_field_is_not_a_query_input(self):
        self.query["policy"] = {"allow": True}
        with self.assertRaises(b.BindingError):
            b.validate_query(self.query, self.material)

    def test_scope_limit_and_selector_fail_closed(self):
        for change in (lambda q: q.update(limit=2), lambda q: q.update(limit=True),
                       lambda q: q["scope"].update(channel_id="ungranted"),
                       lambda q: q.update(actor_account_id="ungranted"), lambda q: q.update(person_id="unresolved")):
            query = deepcopy(self.query)
            change(query)
            with self.subTest(query=query), self.assertRaises(b.BindingError):
                b.validate_query(query, self.material)

    def test_query_cannot_add_a_write_grant(self):
        self.query["access_scope"]["write_scopes"] = deepcopy(self.query["access_scope"]["read_scopes"])
        with self.assertRaises(b.BindingError):
            b.validate_query(self.query, self.material)

    def test_policy_only_accepts_narrowing(self):
        policy = deepcopy(self.material["policy"])
        policy["access_scope"]["read_scopes"] = []
        self.assertTrue(b.policy_narrows(policy, self.material["base_policy"]))
        for mutate in (lambda p: p["operations"].append("ingest"),
                       lambda p: p["sources"].update({self.material["binding"]["source_id"]: "real"}),
                       lambda p: p["access_scope"].update(write_scopes=deepcopy(p["access_scope"]["read_scopes"]))):
            policy = deepcopy(self.material["policy"])
            mutate(policy)
            self.assertFalse(b.policy_narrows(policy, self.material["base_policy"]))

    def test_receipt_cannot_create_a_grant(self):
        self.assertEqual(self.rejected(0, lambda: self.receipt.update(grants_created=["ai_input"])), "receipt_created_grants")

    def test_receipt_rejects_changed_raw_text(self):
        self.rejected(0, lambda: self.receipt["records"][0].update(text="changed"))

    def test_receipt_rejects_changed_selector(self):
        self.rejected(0, lambda: self.receipt["binding"].update(revision=999))

    def test_receipt_rejects_wrong_exit(self):
        self.rejected(3, lambda: None)

    def test_receipt_rejects_wrong_policy_hash(self):
        self.rejected(0, lambda: self.receipt.update(trusted_policy_sha256="0" * 64))

    def test_receipt_rejects_real_or_published_evidence(self):
        for field, value in (("source_kind", "real"), ("layer", "published"), ("rank", 1)):
            self.receipt = deepcopy(self.material["reference"])
            with self.subTest(field=field):
                self.rejected(0, lambda: self.receipt["native_result"]["evidence"][0].update({field: value}))

    def test_receipt_rejects_malformed_native_trace(self):
        for mutate in (lambda n: n.pop("execution"), lambda n: n.update(source_kind=["real"]),
                       lambda n: n["execution"]["attempted_sources"][0].update(source_id="unrelated-source"),
                       lambda n: n["evidence"][0].update(match_reasons=[False])):
            self.receipt = deepcopy(self.material["reference"])
            self.rejected(0, lambda: mutate(self.receipt["native_result"]))

    def test_success_requires_independent_read_intersection(self):
        for authority in ("caller", "query", "source", "operation"):
            self.setUp()
            if authority == "caller":
                self.material["policy"]["access_scope"]["read_scopes"] = []
            elif authority == "query":
                self.query["access_scope"]["read_scopes"] = []
            elif authority == "source":
                source = self.material["binding"]["source_id"]
                self.material["policy"]["source_access_scopes"][source]["read_scopes"] = []
                self.receipt["source_authorization"][0]["current_source_access_scope"] = deepcopy(self.material["policy"]["source_access_scopes"][source])
            else:
                self.material["policy"]["operations"] = []
            self.receipt["query_sha256"] = b.digest(self.query)
            self.receipt["trusted_policy_sha256"] = b.digest(b.policy_snapshot(self.material["policy"]))
            with self.subTest(authority=authority):
                self.rejected(0, lambda: None)

    def test_record_acl_does_not_replace_source_authority(self):
        # A pure verifier fixture models a later raw ACL differing from the
        # independent source ACL. It is not a claimed provider execution.
        record = self.receipt["records"][0]
        record["access_scope"]["read_scopes"] = []
        fingerprint = b.digest(record)
        self.receipt["record_sha256"] = [fingerprint]
        self.material["reference"]["records"][0] = deepcopy(record)
        self.material["profile"]["record_sha256"] = fingerprint
        b.validate_receipt(self.receipt, self.query, self.material, 0)
        self.assertEqual(self.receipt["records"][0]["access_scope"]["read_scopes"], [])

    def test_first_and_current_source_acl_are_independently_checked(self):
        self.rejected(0, lambda: self.receipt["source_authorization"][0]["first_persisted_source_access_scope"].update(read_scopes=[]))

    def empty_receipt(self):
        self.receipt.update(status="no_match", reason_code="bound_record_not_found", records=[], record_sha256=[], source_authorization=[])
        self.receipt["native_result"].update(status="no_match", reason_code="no_records", evidence=[])
        self.receipt["native_result"]["execution"]["attempted_sources"][0].update(status="no_match", reason_code="no_records")

    def test_empty_keeps_distinct_outer_and_native_reasons(self):
        self.empty_receipt()
        b.validate_receipt(self.receipt, self.query, self.material, 0)

    def test_empty_cannot_disguise_a_denial_reason(self):
        self.empty_receipt()
        self.rejected(0, lambda: self.receipt["native_result"].update(reason_code="access_denied"))

    def test_failure_cannot_leak_a_record(self):
        self.empty_receipt()
        self.rejected(0, lambda: self.receipt.update(records=[self.material["reference"]["records"][0]]))

    def execute_injected(self, response=None, source_maps=None, side_effect=None):
        folder = b.WORKSPACE / "evidence/development/20261106-chat-store-caller-r2" / ("unit-" + uuid4().hex)
        folder.mkdir(parents=True)
        query_path = folder / "query.json"
        b.write(query_path, self.query)
        response = response if response is not None else self.receipt
        def fake_run(command, **kwargs):
            self.assertEqual(command[1:6], ["-B", "-X", "utf8", "-m", "source.hgpt_ssr.record_reader"])
            self.assertEqual(kwargs["cwd"], b.PROVIDER)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            if side_effect:
                side_effect(query_path)
            return subprocess.CompletedProcess(command, 0, b.canonical(response), b"private stderr fixture")
        maps = source_maps if source_maps is not None else [{"fixed": "same"}, {"fixed": "same"}]
        with patch.object(b, "subprocess") as process, patch.object(b, "source_files", side_effect=maps):
            process.DEVNULL = subprocess.DEVNULL
            process.run.side_effect = fake_run
            process.TimeoutExpired = subprocess.TimeoutExpired
            return b.execute(b.HOST_PROFILE, query_path, folder / "run"), folder

    def test_fixed_command_and_reader_held(self):
        (report, code), _ = self.execute_injected()
        self.assertEqual(code, 0)
        self.assertFalse(report["reader_invoked"])
        self.assertEqual(report["reader_status"], "held")
        self.assertEqual(report["grants_created"], [])
        self.assertNotIn("private stderr fixture", json.dumps(report))

    def test_wrong_receipt_is_held_without_returned_record(self):
        self.receipt["native_result"]["evidence"][0]["source_kind"] = "real"
        (report, code), folder = self.execute_injected()
        self.assertEqual(code, 4)
        self.assertEqual(report["records"], [])
        self.assertNotIn("provider_receipt", report)
        self.assertTrue((folder / "run/receipt.json").is_file())

    def test_source_change_is_held_without_returned_record(self):
        (report, code), _ = self.execute_injected(source_maps=[{"fixed": "before"}, {"fixed": "after"}])
        self.assertEqual(code, 4)
        self.assertEqual(report["records"], [])
        self.assertNotIn("provider_receipt", report)
        self.assertFalse(report["source_unchanged"])

    def test_source_disappearance_still_writes_held_receipt(self):
        (report, code), folder = self.execute_injected(source_maps=[{"fixed": "same"}, OSError("pure injected source disappearance")])
        self.assertEqual(code, 4)
        self.assertEqual(report["records"], [])
        self.assertTrue((folder / "run/receipt.json").is_file())

    def test_owned_query_change_is_held_without_touching_provider(self):
        def change(path):
            path.write_bytes(b.canonical({**self.query, "request_id": "changed-after-read"}))
        (report, code), _ = self.execute_injected(side_effect=change)
        self.assertEqual(code, 4)
        self.assertEqual(report["records"], [])

    def test_oserror_retains_bounded_target_without_retry(self):
        folder = b.WORKSPACE / "evidence/development/20261106-chat-store-caller-r2" / ("unit-oserror-" + uuid4().hex)
        folder.mkdir(parents=True)
        query = folder / "query.json"
        b.write(query, self.query)
        target = str(folder / "synthetic-fixed-target.exe")
        error = PermissionError(13, "pure injected failure text must not be exposed", target)
        with patch.object(b, "source_files", return_value={"fixed": "same"}), patch.object(b.subprocess, "run", side_effect=error) as run:
            report, code = b.execute(b.HOST_PROFILE, query, folder / "run")
        self.assertEqual(code, 4)
        self.assertFalse(report["provider_invoked"])
        self.assertEqual(report["records"], [])
        self.assertEqual(run.call_count, 1)
        call = b.read(folder / "run/provider-execution.json")
        self.assertEqual(call["failure"], {"type": "PermissionError", "errno": 13, "winerror": None, "filename": target})
        self.assertNotIn("pure injected failure text", json.dumps(call))


if __name__ == "__main__":
    unittest.main()
