"""Synthetic-only review UI/API tests; no external providers or existing state."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ssr_chat.review_web import MAX_BODY, REPO, ReviewError, ReviewServer, ReviewWorkspace, criteria, examples, handler_for


class SyntheticWorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.parent = (REPO / ".test-temp" / "review-web-tests").resolve()
        self.parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.parent)
        self.app = ReviewWorkspace(Path(self.temp.name))
        self.event = deepcopy(examples()[0]["event"])

    def tearDown(self):
        self.assertEqual(Path(self.temp.name).resolve().parent, self.parent)
        self.temp.cleanup()

    def payload(self, event=None, request_id="review-operation"):
        return {"synthetic": True, "workspace_id": self.app.workspace_id, "request_id": request_id,
                "event": deepcopy(self.event if event is None else event)}


class WorkspaceTests(SyntheticWorkspaceCase):
    def test_real_fixture_runtime_returns_answer_prompt_trace_and_never_sends(self):
        response = self.app.run(self.payload())
        self.assertEqual(response["result"]["status"], "answered")
        self.assertTrue(response["result"]["answer"])
        self.assertEqual(response["result"]["send_result"]["status"], "dry_run_not_sent")
        self.assertFalse(response["result"]["send_result"]["attempted"])
        self.assertFalse(response["result"]["send_result"]["public_reply"])
        self.assertEqual(response["execution_mode"], "executed_fixture_runtime")
        self.assertEqual(response["adapters"], {"model": "fixture", "knowledge": "fixture", "weather": "fixture", "output": "dry_run"})
        self.assertEqual(response["evidence"]["input"], self.event)
        self.assertEqual(len(response["evidence"]["trace"]), 1)
        self.assertEqual(response["evidence"]["prompt"]["context_snapshot_id"], response["result"]["context_snapshot_id"])

    def test_followup_context_is_same_room_only(self):
        self.app.run(self.payload())
        event = {**self.event, "message_id": "review-followup", "text": "!ask 那款遊戲還有哪些特色？"}
        response = self.app.run(self.payload(event, "review-followup-operation"))
        self.assertEqual(response["evidence"]["prompt"]["context_messages"][0]["message_id"], self.event["message_id"])
        other = {**event, "message_id": "review-other", "conversation_id": "review-other-room"}
        response = self.app.run(self.payload(other, "review-other-operation"))
        self.assertEqual(response["evidence"]["prompt"]["context_messages"], ())

    def test_missing_untriggered_and_self_use_real_sop_without_model(self):
        cases = [("missing", "missing_message_id"), ("untriggered", "not_triggered"), ("self", "self_event_loop_guard")]
        for case_id, reason in cases:
            event = next(item["event"] for item in examples() if item["id"] == case_id)
            event = {**event, "message_id": "review-" + case_id} if "message_id" in event else event
            response = self.app.run(self.payload(event, "review-" + case_id))
            self.assertEqual((response["result"]["status"], response["result"]["reason"]), ("held", reason))
            self.assertIsNone(response["evidence"]["prompt"])
            self.assertEqual(response["evidence"]["sop"][0]["reason"], reason)
        self.assertEqual(self.app.model.calls, 0)

    def test_duplicate_event_is_held_but_same_operation_replays_receipt(self):
        first = self.app.run(self.payload())
        replay = self.app.run(self.payload())
        self.assertTrue(replay.pop("replayed"))
        self.assertEqual(replay, {k: v for k, v in first.items() if k != "replayed"})
        duplicate = self.app.run(self.payload(request_id="review-duplicate"))
        self.assertEqual(duplicate["result"]["reason"], "duplicate_message_id")
        self.assertIsNone(duplicate["evidence"]["prompt"])
        self.assertEqual(self.app.model.calls, 1)

    def test_concurrent_identical_operations_execute_once(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: self.app.run(self.payload()), range(4)))
        self.assertEqual(sum(not item["replayed"] for item in results), 1)
        self.assertEqual(self.app.model.calls, 1)

    def test_request_id_conflict_preserves_original_receipt(self):
        self.app.run(self.payload())
        with self.assertRaisesRegex(ReviewError, "request_id_conflict") as caught:
            self.app.run(self.payload({**self.event, "text": "changed"}))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.app.model.calls, 1)

    def test_untrusted_fields_types_and_real_sources_never_reach_runtime(self):
        events = [{**self.event, "source": "twitch-eventsub"}, {**self.event, "path": "C:/private"},
                  {**self.event, "conversation_id": []}, {**self.event, "conversation_id": "real-room"},
                  {**self.event, "author_id": "real-account"}, {**self.event, "message_id": {}},
                  {**self.event, "is_self": "false"}, {**self.event, "text": "x" * 2001}, []]
        with patch.object(self.app.runtime, "handle") as handle:
            for event in events:
                with self.subTest(event=event), self.assertRaises(ReviewError):
                    self.app.run(self.payload(event))
            for changes in ({"synthetic": False}, {"request_id": []}, {"path": "C:/private"}):
                with self.subTest(changes=changes), self.assertRaises(ReviewError):
                    self.app.run({**self.payload(), **changes})
            handle.assert_not_called()

    def test_reset_does_not_reuse_or_delete_previous_workspace(self):
        old = self.payload()
        self.app.run(old)
        old_state = self.app.state
        reset = self.app.reset()
        self.assertNotEqual(old["workspace_id"], reset["workspace_id"])
        self.assertTrue(old_state.exists())
        with self.assertRaisesRegex(ReviewError, "workspace_changed"):
            self.app.run(old)
        self.assertEqual(self.app.run(self.payload())["result"]["status"], "answered")

    def test_model_failure_retains_runtime_error_and_no_delivery(self):
        with patch.object(self.app.model, "generate", side_effect=RuntimeError("C:/private/no-leak")):
            response = self.app.run(self.payload())
        self.assertEqual(response["result"]["status"], "failed")
        self.assertEqual(response["result"]["reason"], "model_error")
        self.assertEqual(response["result"]["send_result"]["status"], "not_attempted")
        self.assertNotIn("C:/private", json.dumps(response))

    def test_operation_limit_rejects_new_work_without_model(self):
        self.app.receipts = {"unused-" + str(i): ("hash", {}) for i in range(100)}
        with self.assertRaisesRegex(ReviewError, "operation_limit"):
            self.app.run(self.payload())
        self.assertEqual(self.app.model.calls, 0)

    def test_metadata_is_sanitized_and_criteria_have_twenty_two_specific_cases(self):
        body = self.app.meta()
        self.assertNotIn(str(REPO), json.dumps(body))
        self.assertEqual(len(body["version"]["fingerprint"]), 64)
        self.assertEqual([row["id"] for row in criteria()], [f"WEB{i:02d}" for i in range(1, 23)])


class InMemoryHandlerTests(SyntheticWorkspaceCase):
    """Exercise the actual handlers without socket creation or network I/O."""
    def request(self, method, path, raw=None, **changes):
        raw = raw or b"{}"
        handler = object.__new__(handler_for(self.app))
        handler.server = SimpleNamespace(server_port=8826)
        handler.path = path
        handler.headers = {"Host": "127.0.0.1:8826", "Content-Type": "application/json",
                           "Content-Length": str(len(raw)), "Origin": "http://127.0.0.1:8826",
                           "X-SSR-Review-Token": self.app.token, **changes}
        handler.rfile, handler.wfile = BytesIO(raw), BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
        getattr(handler, "do_" + method)()
        headers = dict(call.args for call in handler.send_header.call_args_list)
        output = handler.wfile.getvalue()
        data = json.loads(output) if headers["Content-Type"].startswith("application/json") else output.decode()
        return handler.send_response.call_args.args[0], data, headers

    def test_handler_page_and_security_headers_without_socket(self):
        for path in ("/", "/app.js", "/styles.css", "/api/meta", "/api/examples", "/api/criteria"):
            code, data, headers = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(data)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            if path == "/api/criteria":
                self.assertNotIn(str(REPO.parents[1]), data["standard"])

    def test_handler_actual_runtime_response_without_http_transport(self):
        code, response, _ = self.request("POST", "/api/run", json.dumps(self.payload()).encode())
        self.assertEqual(code, 200)
        self.assertEqual(response["result"]["status"], "answered")
        self.assertEqual(response["result"]["send_result"]["status"], "dry_run_not_sent")

    def test_handler_origin_token_and_host_boundaries(self):
        with patch.object(self.app.runtime, "handle") as handle:
            for changes in ({"Origin": "https://untrusted.invalid"}, {"X-SSR-Review-Token": "wrong"},
                            {"X-SSR-Review-Token": "é"}, {"Host": "untrusted.invalid"}):
                self.assertEqual(self.request("POST", "/api/run", json.dumps(self.payload()).encode(), **changes)[0], 403)
            handle.assert_not_called()
        self.assertEqual(self.request("GET", "/api/meta", Host="untrusted.invalid")[0], 403)

    def test_handler_json_size_and_type_errors(self):
        for raw in (b'{', b'[]', b'{"x":1,"x":2}', b'{"x":NaN}'):
            self.assertEqual(self.request("POST", "/api/run", raw)[0], 400)
        self.assertEqual(self.request("POST", "/api/run", b"x" * (MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/run", b"{}", **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/run", b"{}", **{"Content-Length": "bad"})[0], 400)
        self.assertEqual(self.app.model.calls, 0)

    def test_handler_reset_and_receipt_conflict(self):
        original = self.payload()
        self.assertEqual(self.request("POST", "/api/run", json.dumps(original).encode())[0], 200)
        changed = {**original, "event": {**self.event, "text": "changed"}}
        self.assertEqual(self.request("POST", "/api/run", json.dumps(changed).encode())[0], 409)
        code, reset, _ = self.request("POST", "/api/reset", json.dumps({"workspace_id": self.app.workspace_id}).encode())
        self.assertEqual((code, reset["status"]), (200, "new_synthetic_workspace"))
        self.assertEqual(self.request("POST", "/api/run", json.dumps(original).encode())[0], 409)

    def test_handler_readonly_gap_and_no_file_browsing(self):
        for path in ("/../README.md", "/api/twitch/login", "/api/ollama", "/api/files"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/unknown", b"{}")[0], 404)
        code, body, _ = self.request("GET", "/api/evidence")
        self.assertEqual((code, body["g01"]["status"]), (200, "blocked"))
        self.assertEqual(body["g01"]["mode"], "read_only_record")


@unittest.skipUnless(os.environ.get("SSR_CHAT_REVIEW_HTTP_TESTS") == "1",
                     "real loopback tests blocked by WinError 10013; require fresh authorization before opt-in")
class HttpReviewTests(unittest.TestCase):
    def setUp(self):
        self.parent = (REPO / ".test-temp" / "review-web-http-tests").resolve()
        self.parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.parent)
        self.app = ReviewWorkspace(Path(self.temp.name))
        self.server = ReviewServer(("127.0.0.1", 0), handler_for(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(Path(self.temp.name).resolve().parent, self.parent)
        self.temp.cleanup()

    def request(self, method, path, raw=None, **changes):
        headers = {"Content-Type": "application/json", "Origin": self.origin, "X-SSR-Review-Token": self.app.token, **changes}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
            content = json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data.decode()
            return response.status, content, dict(response.getheaders())
        finally:
            connection.close()

    def run_body(self):
        return json.dumps({"synthetic": True, "workspace_id": self.app.workspace_id, "request_id": "review-http", "event": examples()[0]["event"]}).encode()

    def test_page_assets_and_actual_http_input_output(self):
        for path in ("/", "/app.js", "/styles.css", "/api/meta", "/api/examples", "/api/criteria"):
            code, value, headers = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertTrue(value)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        code, response, _ = self.request("POST", "/api/run", self.run_body())
        self.assertEqual(code, 200)
        self.assertEqual(response["result"]["status"], "answered")
        self.assertFalse(response["result"]["send_result"]["public_reply"])

    def test_cross_origin_token_and_host_rejections_do_not_execute(self):
        cases = [{"Origin": "https://untrusted.invalid"}, {"X-SSR-Review-Token": "wrong"},
                 {"Host": "untrusted.invalid"}, {"X-SSR-Review-Token": "é"}]
        with patch.object(self.app.runtime, "handle") as handle:
            for headers in cases:
                with self.subTest(headers=headers):
                    code, body, _ = self.request("POST", "/api/run", self.run_body(), **headers)
                    self.assertEqual(code, 403)
                    self.assertEqual(body["reason_code"], "local_page_token_required")
            handle.assert_not_called()
        self.assertEqual(self.request("GET", "/api/meta", Host="untrusted.invalid")[0], 403)

    def test_invalid_json_duplicates_nonfinite_content_type_and_size(self):
        for raw in (b'{', b'[]', b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/api/run", raw)[0], 400)
        self.assertEqual(self.request("POST", "/api/run", b"x" * (MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/run", b"{}", **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/run", b"", **{"Content-Length": "bad"})[0], 400)
        self.assertEqual(self.app.model.calls, 0)

    def test_reset_and_stale_workspace(self):
        old = self.run_body()
        code, reset, _ = self.request("POST", "/api/reset", json.dumps({"workspace_id": self.app.workspace_id}).encode())
        self.assertEqual(code, 200)
        self.assertEqual(reset["status"], "new_synthetic_workspace")
        self.assertEqual(self.request("POST", "/api/run", old)[0], 409)

    def test_no_file_browsing_or_external_adapter_endpoints(self):
        for path in ("/../README.md", "/api/twitch/login", "/api/ollama", "/api/files"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/unknown", b"{}")[0], 404)

    def test_evidence_labels_history_and_unresolved_g01(self):
        code, body, _ = self.request("GET", "/api/evidence")
        self.assertEqual(code, 200)
        self.assertEqual(body["g01"]["status"], "blocked")
        self.assertEqual(body["g01"]["mode"], "read_only_record")
        self.assertIn("改動前", body["historical"])


if __name__ == "__main__":
    unittest.main()
