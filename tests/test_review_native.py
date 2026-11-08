"""Real native CLI integration through the page handler; no HTTP/socket/model."""
from copy import deepcopy
from io import BytesIO
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from ssr_chat.review_native import INTERFACE_VERSION
from ssr_chat.review_web import REPO, ReviewWorkspace, handler_for


class NativeReviewHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = REPO.parents[1] / "evidence" / "development" / "native-review-web-tests" / uuid4().hex
        cls.artifacts.mkdir(parents=True, exist_ok=False)
        cls.app = ReviewWorkspace(cls.artifacts)
        cls.outputs = {}
        # One real initial call. Stop the class on any failure, not repeated attempts.
        code, response = cls.send("prepare", "channel-prepare", "/channel launch")
        if code != 200 or response.get("pipeline_exit_code") != 3:
            raise RuntimeError("native_prepare_smoke_failed; evidence=" + str(cls.artifacts))
        cls.prepared = response

    @classmethod
    def payload(cls, name, tool, text):
        return {"synthetic": True, "workspace_id": cls.app.workspace_id,
                "request_id": "review-native-" + name, "request": {"tool": tool, "text": text}}

    @classmethod
    def send(cls, name, tool=None, text=None, *, body=None):
        body = body if body is not None else cls.payload(name, tool, text)
        raw = json.dumps(body, ensure_ascii=False).encode()
        handler = object.__new__(handler_for(cls.app))
        handler.server = SimpleNamespace(server_port=8826)
        handler.path = "/api/native/run"
        handler.headers = {"Host": "127.0.0.1:8826", "Content-Type": "application/json", "Content-Length": str(len(raw)),
                           "Origin": "http://127.0.0.1:8826", "X-SSR-Review-Token": cls.app.token}
        handler.rfile, handler.wfile = BytesIO(raw), BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        handler.do_POST()
        response = json.loads(handler.wfile.getvalue())
        status = handler.send_response.call_args.args[0]
        for suffix, value in (("input", body), ("response", {"handler_status": status, "body": response})):
            with (cls.artifacts / f"{name}.{suffix}.json").open("x", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, indent=2)
        cls.outputs[name] = {"handler_status": status, "body": response}
        return status, response

    def assert_native(self, response, *, readers=1):
        self.assertEqual(response["execution_mode"], "actual_native_pipeline")
        self.assertFalse(response["model_invoked"])
        self.assertIsNone(response["generated_answer"])
        self.assertEqual(response["public_output"], "not_attempted")
        self.assertTrue(response["source_unchanged"])
        self.assertEqual(len(response["evidence"]["native_reader_receipts"]), readers)
        for receipt in response["evidence"]["native_reader_receipts"]:
            self.assertEqual(receipt["kind"], "actual_native_channel_query")
            self.assertEqual(receipt["request_id"], response["operation_id"])
            self.assertEqual(receipt["exit_code"], 0)
            self.assertFalse(receipt["wire_conversion"])
        self.assertEqual(self.app.model.calls, 0)

    def test_prepare_uses_actual_receipts_and_same_prompt_without_generation(self):
        response = self.prepared
        self.assert_native(response)
        self.assertEqual(response["interface_version"], INTERFACE_VERSION)
        self.assertEqual(response["pipeline_exit_code"], 3)
        self.assertEqual(response["result"]["reason_code"], "answer_generation_not_enabled")
        self.assertEqual(response["prompt_status"], "prepared_not_sent")
        self.assertEqual(len(response["evidence"]["records"]), 2)
        self.assertEqual(response["evidence"]["input"]["text"], "/channel launch")
        self.assertEqual(response["evidence"]["prompt"]["user_text"], "launch")
        self.assertEqual({s["stage"] for s in response["evidence"]["trace"]}, {"planner", "dispatcher", "collector"})
        self.assertTrue(all(s["exit_code"] == 0 for s in response["evidence"]["trace"]))

    def test_evidence_and_identical_operation_replay_do_not_invoke_twice(self):
        status, response = self.send("evidence", "channel-evidence", "/channel launch")
        self.assertEqual(status, 200)
        self.assert_native(response)
        self.assertEqual((response["pipeline_exit_code"], response["result"]["status"]), (0, "evidence_ready"))
        self.assertEqual(len(response["evidence"]["records"]), 2)
        self.assertIsNone(response["evidence"]["prompt"])
        before = len(list(self.app.native.reader_io.iterdir()))
        with patch.object(self.app.native, "run", side_effect=AssertionError("receipt replay must not execute")):
            code, replay = self.send("evidence-replay", body=self.payload("evidence", "channel-evidence", "/channel launch"))
        self.assertEqual(code, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(list(self.app.native.reader_io.iterdir())), before)

    def test_purpose_denial_is_actual_and_never_prepares_prompt(self):
        code, response = self.send("purpose-denied", "channel-purpose-denied", "/channel launch")
        self.assertEqual(code, 200)
        self.assert_native(response)
        self.assertEqual((response["pipeline_exit_code"], response["result"]["reason_code"]), (1, "ai_input_not_allowed"))
        self.assertEqual(len(response["evidence"]["records"]), 2)
        self.assertIsNone(response["evidence"]["prompt"])
        self.assertEqual(response["prompt_status"], "not_prepared")

    def test_no_match_is_native_zero_evidence_without_fixture_fallback(self):
        code, response = self.send("no-match", "channel-evidence", "/channel absent-synthetic-term-92837")
        self.assertEqual(code, 200)
        self.assert_native(response)
        self.assertEqual((response["pipeline_exit_code"], response["result"]["status"]), (0, "no_match"))
        self.assertEqual(response["evidence"]["records"], [])

    def test_illegal_and_nested_commands_stop_in_real_entry_before_reader(self):
        for name, text, reason in (("illegal", "/unknown launch", "entry_command_unsupported"),
                                   ("nested", "/channel launch /history anything", "entry_multiple_commands")):
            with self.subTest(name=name):
                code, response = self.send(name, "channel-evidence", text)
                self.assertEqual(code, 200)
                self.assert_native(response, readers=0)
                self.assertEqual((response["pipeline_exit_code"], response["result"]["reason_code"]), (1, reason))
                self.assertEqual(response["evidence"]["trace"], [])

    def test_acl_paths_and_unknown_tool_are_rejected_before_native_execution(self):
        before = len(list(self.app.native.root.glob("operation-*")))
        for name, update in (("acl", {"access_scope": {"read_scopes": ["all"]}}),
                             ("command", {"command": ["arbitrary.exe"]}), ("python", {"python": "C:/other/python.exe"}),
                             ("repo", {"repo": "C:/other"}), ("policy", {"policy": "allow-all"})):
            body = self.payload("inject-" + name, "channel-prepare", "/channel launch")
            body["request"].update(update)
            code, response = self.send("inject-" + name, body=body)
            self.assertEqual((code, response["reason_code"]), (400, "native_request_fields_invalid"))
        body = self.payload("inject-outer", "channel-prepare", "/channel launch")
        body["access_scope"] = {"write_scopes": ["all"]}
        self.assertEqual(self.send("inject-outer", body=body)[0], 400)
        self.assertEqual(self.send("unknown-tool", "unregistered", "/channel launch")[0], 400)
        self.assertEqual(len(list(self.app.native.root.glob("operation-*"))), before)

    def test_changed_operation_id_content_is_conflict_and_preserves_receipt(self):
        body = self.payload("prepare", "channel-prepare", "/channel different")
        code, response = self.send("conflict", body=body)
        self.assertEqual((code, response["reason_code"]), (409, "request_id_conflict"))
        self.assertEqual(self.app.native_receipts[body["request_id"]][1]["evidence"]["input"]["text"], "/channel launch")

    def test_browser_projection_does_not_expose_private_machine_paths(self):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
        for value in strings(self.prepared):
            self.assertNotIn(str(REPO), value)
            self.assertNotIn(str(self.artifacts), value)
            self.assertNotIn("python.exe", value)

    @classmethod
    def tearDownClass(cls):
        final = json.loads((REPO.parents[1]/"evidence/development/20261105-offline-runtime/FINAL.json").read_text(encoding="utf-8-sig"))
        preserved = {name: hashlib.sha256((REPO/name).read_bytes()).hexdigest() == value for name,value in final["changed_files_sha256"].items()}
        summary = {"mode": "real_native_cli_through_in_process_handler_no_socket", "artifacts": str(cls.artifacts),
                   "coordinator_baseline_file_matches": preserved,
                   "intentional_additions_since_baseline": ["local_pipeline library mode guard", "boundary test and documentation"],
                   "model_calls": cls.app.model.calls,
                   "browser_gate": "blocked_not_retried", "cases": {name: {"handler_status": row["handler_status"],
                       "pipeline_exit_code": row["body"].get("pipeline_exit_code"), "status": row["body"].get("result", row["body"]).get("status"),
                       "reason": row["body"].get("result", row["body"]).get("reason_code"), "snapshot": row["body"].get("source_snapshot_sha256")}
                       for name,row in cls.outputs.items()}}
        snapshots = {row["snapshot"] for row in summary["cases"].values() if row["snapshot"]}
        summary["all_cases_same_source_snapshot"] = len(snapshots) == 1
        with (cls.artifacts/"summary.json").open("x",encoding="utf-8") as output:json.dump(summary,output,ensure_ascii=False,indent=2)
        print("NATIVE_WEB_EVIDENCE=" + str(cls.artifacts), flush=True)
        if len(snapshots) != 1:
            raise AssertionError("dependency source changed between cases; no same-version acceptance")


if __name__ == "__main__":
    unittest.main()
