from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import LifecycleHTTPServer, dispatch, handler_for, serve
from ssr_chat.api_lifecycle import (
    FileLifecycleControl, RequestLifecycle, main, read_control_status, write_control_command,
)


def temporary_directory():
    root = Path(__file__).resolve().parents[1] / ".test-temp" / "api-lifecycle"
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


class RequestLifecycleTests(unittest.TestCase):
    def test_drain_rejects_new_work_and_stop_waits_for_existing_work(self):
        gate = RequestLifecycle()
        first, second = gate.admit(), gate.admit()
        gate.request_stop()
        self.assertIsNone(gate.admit())
        self.assertFalse(gate.stop_ready)
        with self.assertRaisesRegex(RuntimeError, "requests_not_drained"):
            gate.mark_stopped()
        gate.complete(second, error=True)
        self.assertFalse(gate.stop_ready)
        gate.complete(first)
        self.assertTrue(gate.stop_ready)
        gate.mark_stopped()
        self.assertEqual(gate.snapshot(), {"state": "stopped", "ready": False, "accepted": 2,
                         "completed": 2, "active": 0, "errors": 1, "rejected": 1, "stop_requested": True})

    def test_duplicate_completion_cannot_forge_drained_state(self):
        gate = RequestLifecycle()
        first, second = gate.admit(), gate.admit()
        gate.complete(first)
        for value in (first, 1000):
            with self.assertRaises(ValueError):
                gate.complete(value)
        self.assertEqual(gate.snapshot()["active"], 1)
        gate.complete(second)
        gate.drain()
        gate.drain()
        self.assertFalse(gate.stop_ready)

    def test_concurrent_admissions_settle_after_gate_closes(self):
        gate = RequestLifecycle()
        barrier = threading.Barrier(9)
        release = threading.Event()
        failures = []
        def worker():
            try:
                lease = gate.admit()
                barrier.wait(timeout=3)
                if not release.wait(timeout=3):
                    raise RuntimeError("test_release_timeout")
                gate.complete(lease)
            except BaseException as error:
                failures.append(error)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        try:
            barrier.wait(timeout=3)
            gate.request_stop()
            self.assertEqual(gate.snapshot()["active"], 8)
            self.assertIsNone(gate.admit())
            self.assertFalse(gate.stop_ready)
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=3)
        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(gate.stop_ready)
        self.assertEqual(gate.snapshot()["accepted"], gate.snapshot()["completed"])


class LifecycleHandlerTests(unittest.TestCase):
    def setUp(self):
        self.gate = RequestLifecycle()
        self.runtime = Mock()
        self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture",
                                          "weather": "fixture", "output": "dry_run"}
        self.runtime.handle.return_value = {"status": "answered"}

    def handler(self, path="/chat", payload=b'{}', **routes):
        handler_type = handler_for(self.runtime, lifecycle=self.gate, **routes)
        # Exercise the real HTTP method, body read, dispatch and response boundary
        # without constructing a socket or contacting any existing service.
        handler = object.__new__(handler_type)
        handler.path = path
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = BytesIO(payload)
        handler._respond = Mock()
        return handler

    def test_health_default_unchanged_and_opt_in_reports_readiness_without_binding_secret(self):
        self.assertEqual(dispatch(self.runtime, "GET", "/health")[1]["status"], "ok")
        code, body = dispatch(self.runtime, "GET", "/health", lifecycle=self.gate)
        self.assertEqual((code, body["status"], body["ready"]), (200, "ready", True))
        self.gate.drain()
        code, body = dispatch(self.runtime, "GET", "/health", lifecycle=self.gate)
        self.assertEqual((code, body["status"], body["ready"]), (503, "draining", False))
        self.assertNotIn("generation", json.dumps(body))
        self.assertNotIn("control_token", json.dumps(body))

    def test_all_post_routes_rejected_before_body_read_or_gateway_after_drain(self):
        self.gate.drain()
        for path in ("/chat", "/retrieve", "/retrieve/channel", "/answer/chat", "/query",
                     "/twitch/replay", "/twitch/archive", "/unknown"):
            with self.subTest(path=path):
                gateway = Mock()
                handler = self.handler(path, entry_gateway=gateway, twitch_replay_gateway=gateway,
                                       twitch_archive_gateway=gateway)
                handler.rfile = Mock()
                handler.do_POST()
                handler.rfile.read.assert_not_called()
                self.assertTrue(handler.close_connection)
                handler._respond.assert_called_once_with(503, {"status": "draining", "reason_code": "api_draining"})
                self.assertEqual(gateway.mock_calls, [])
        self.runtime.handle.assert_not_called()
        self.assertEqual(self.gate.snapshot()["rejected"], 8)

    def test_success_and_invalid_json_complete_accounting(self):
        first, invalid = self.handler(), self.handler(payload=b"{")
        first.do_POST()
        invalid.do_POST()
        self.assertEqual(first._respond.call_args.args[0], 200)
        self.assertEqual(invalid._respond.call_args.args[0], 400)
        self.assertEqual(self.gate.snapshot()["accepted"], 2)
        self.assertEqual(self.gate.snapshot()["completed"], 2)
        self.assertEqual(self.gate.snapshot()["errors"], 0)

    def test_unhandled_work_exception_and_response_failure_always_complete_with_error(self):
        self.runtime.handle.side_effect = RuntimeError("synthetic failure")
        handler = self.handler()
        with self.assertRaises(RuntimeError):
            handler.do_POST()
        self.runtime.handle.side_effect = None
        handler = self.handler()
        handler._respond.side_effect = BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            handler.do_POST()
        self.assertEqual(self.gate.snapshot()["active"], 0)
        self.assertEqual(self.gate.snapshot()["completed"], 2)
        self.assertEqual(self.gate.snapshot()["errors"], 2)

    def test_translated_gateway_failure_is_counted(self):
        gateway = Mock()
        gateway.query.side_effect = RuntimeError("synthetic failure")
        handler = self.handler("/query", entry_gateway=gateway)
        handler.do_POST()
        self.assertEqual(handler._respond.call_args.args[0], 500)
        self.assertEqual(self.gate.snapshot()["errors"], 1)

    def test_request_is_active_through_response_delivery(self):
        handler = self.handler()
        def respond(*_):
            self.gate.request_stop()
            self.assertEqual(self.gate.snapshot()["active"], 1)
            self.assertFalse(self.gate.stop_ready)
        handler._respond.side_effect = respond
        handler.do_POST()
        self.assertTrue(self.gate.stop_ready)


class FileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "generation"
        self.gate = RequestLifecycle()
        self.control = FileLifecycleControl(self.gate, self.directory, 8776)

    def command(self, **changes):
        owner = json.loads((self.directory / "owner.json").read_text(encoding="utf-8"))
        command = {"generation": owner["generation"], "control_token": owner["control_token"],
                   "action": "drain", "request_id": "synthetic-request"}
        command.update(changes)
        (self.directory / "command.json").write_text(json.dumps(command), encoding="utf-8")

    def test_exclusive_directory_and_fresh_bindings_prevent_reusing_prior_generation(self):
        with self.assertRaises(FileExistsError):
            FileLifecycleControl(RequestLifecycle(), self.directory, 8776)
        other = FileLifecycleControl(RequestLifecycle(), Path(self.temp.name) / "other", 8776)
        original = json.loads((self.directory / "owner.json").read_bytes())
        second = json.loads((other.directory / "owner.json").read_bytes())
        self.assertNotEqual(original["generation"], second["generation"])
        self.assertNotEqual(original["control_token"], second["control_token"])
        self.assertNotIn("control_token", (self.directory / "status.json").read_text())
        self.command(generation=second["generation"], control_token=second["control_token"])
        self.control.poll()
        self.assertTrue(self.gate.snapshot()["ready"])

    def test_valid_drain_then_stop_waits_for_lease_and_publishes_matching_request(self):
        lease = self.gate.admit()
        request = write_control_command(self.directory, "drain")
        self.control.poll()
        status = read_control_status(self.directory)
        self.assertEqual(status["last_command"]["request_id"], request)
        self.assertEqual(status["state"], "draining")
        self.assertEqual(status["active"], 1)
        self.assertFalse(self.gate.stop_ready)
        request = write_control_command(self.directory, "stop")
        self.control.poll()
        self.assertFalse(self.gate.stop_ready)
        self.gate.complete(lease)
        self.assertTrue(self.gate.stop_ready)
        self.gate.mark_stopped()
        self.control.publish()
        self.assertEqual(read_control_status(self.directory)["state"], "stopped")

    def test_stale_wrong_token_extra_fields_bad_unicode_and_invalid_json_do_not_close_gate(self):
        cases = [{"generation": "old"}, {"control_token": "wrong"}, {"extra": True},
                 {"control_token": "非ASCII"}, {"action": "restart"}, {"request_id": "../escape"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.command(**changes)
                self.control.poll()
                self.assertTrue(self.gate.snapshot()["ready"])
        for raw in (b"{", b"[]", b'{"action":"drain","action":"stop"}', b"x" * 16_385):
            (self.directory / "command.json").write_bytes(raw)
            self.control.poll()
            self.assertTrue(self.gate.snapshot()["ready"])
        self.assertEqual(read_control_status(self.directory)["control_error"], "invalid_lifecycle_command")

    def test_same_command_does_not_reapply_and_same_id_cannot_change_action(self):
        self.command()
        self.control.poll()
        self.control.poll()
        self.command(action="stop")
        self.control.poll()
        self.assertFalse(self.gate.snapshot()["stop_requested"])
        self.assertEqual(read_control_status(self.directory)["control_error"], "invalid_lifecycle_command")

    def test_modified_owner_and_replaced_directory_are_refused(self):
        owner = self.directory / "owner.json"
        original = owner.read_bytes()
        owner.write_bytes(original + b" ")
        with self.assertRaisesRegex(RuntimeError, "owner_binding_changed"):
            self.control.poll()
        owner.write_bytes(original)
        renamed = self.directory.with_name("original")
        self.directory.rename(renamed)
        self.directory.mkdir()
        (self.directory / "owner.json").write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "owner_binding_changed"):
            self.control.poll()

    def test_network_control_paths_and_invalid_port_are_refused(self):
        for path in (r"\\server\share\control", "//server/share/control"):
            with self.assertRaisesRegex(ValueError, "must_be_local"):
                FileLifecycleControl(RequestLifecycle(), path, 8776)
        for port in (0, True, 65536):
            with self.assertRaises(ValueError):
                FileLifecycleControl(RequestLifecycle(), Path(self.temp.name) / "bad", port)

    def test_operator_cli_submits_without_claiming_executed_and_status_does_not_leak_token(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["stop", "--directory", str(self.directory)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "submitted")
        self.assertTrue(self.gate.snapshot()["ready"])
        self.control.poll()
        output = StringIO()
        with redirect_stdout(output):
            code = main(["status", "--directory", str(self.directory)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "draining")
        self.assertNotIn("control_token", output.getvalue())

    def test_atomic_command_write_retries_transient_windows_read_sharing(self):
        replace = os.replace
        calls = []
        def transient(source, target):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("synthetic temporary read sharing")
            return replace(source, target)
        with patch("ssr_chat.api_lifecycle.os.replace", side_effect=transient), \
                patch("ssr_chat.api_lifecycle.time.sleep"):
            request = write_control_command(self.directory, "drain")
        self.assertEqual(len(calls), 3)
        self.control.poll()
        self.assertEqual(read_control_status(self.directory)["last_command"]["request_id"], request)


class LifecycleServerTests(unittest.TestCase):
    def server_without_socket(self, gate, control=None):
        server = object.__new__(LifecycleHTTPServer)
        server.lifecycle, server.control = gate, control or Mock()
        server._shutdown_started, server.control_failed = False, False
        server.shutdown = Mock()
        return server

    def test_shutdown_not_started_until_admitted_work_completes(self):
        gate = RequestLifecycle()
        lease = gate.admit()
        gate.request_stop()
        server = self.server_without_socket(gate)
        with patch("ssr_chat.api.threading.Thread") as thread:
            server.service_actions()
            thread.assert_not_called()
            gate.complete(lease)
            server.service_actions()
            server.service_actions()
            thread.assert_called_once_with(target=server.shutdown, daemon=True)
            thread.return_value.start.assert_called_once()

    def test_control_owner_failure_closes_admission_but_does_not_kill_active_work(self):
        gate = RequestLifecycle()
        lease = gate.admit()
        control = Mock()
        control.poll.side_effect = RuntimeError("owner changed")
        server = self.server_without_socket(gate, control)
        with patch("ssr_chat.api.threading.Thread") as thread:
            server.service_actions()
            self.assertTrue(server.control_failed)
            self.assertIsNone(gate.admit())
            thread.assert_not_called()
            gate.complete(lease)
            server.service_actions()
            thread.assert_called_once()

    def test_default_serve_has_no_control_files_or_managed_server(self):
        server = Mock()
        with patch("ssr_chat.api.ThreadingHTTPServer", return_value=server), \
                patch("ssr_chat.api.FileLifecycleControl") as control, \
                patch("ssr_chat.api.LifecycleHTTPServer") as managed:
            with redirect_stdout(StringIO()):
                serve(Mock(), 8776)
        control.assert_not_called()
        managed.assert_not_called()
        server.serve_forever.assert_called_once_with()
        server.server_close.assert_called_once()

    def test_failed_bind_does_not_leave_ready_control_status(self):
        with temporary_directory() as directory:
            root = Path(directory) / "failed-start"
            with patch("ssr_chat.api.LifecycleHTTPServer", side_effect=OSError("synthetic occupied port")):
                with self.assertRaises(OSError):
                    serve(Mock(), 8776, lifecycle_dir=root)
            status = read_control_status(root)
            self.assertFalse(status["ready"])
            self.assertEqual(status["state"], "stopped")
            self.assertEqual(status["accepted"], 0)

    def test_direct_serve_exit_joins_active_handler_on_success_and_error_without_sockets(self):
        server_type = LifecycleHTTPServer
        for fail_work in (False, True):
            with self.subTest(fail_work=fail_work), temporary_directory() as directory:
                started, release, returned = threading.Event(), threading.Event(), threading.Event()
                captured, serving_errors, worker_errors = {}, [], []
                runtime = Mock()
                def work(_):
                    started.set()
                    if not release.wait(timeout=3):
                        raise RuntimeError("test_release_timeout")
                    if fail_work:
                        raise ValueError("synthetic work error")
                    return {"status": "answered"}
                runtime.handle.side_effect = work
                def construct(address, handler_type, lifecycle, control):
                    server = object.__new__(server_type)
                    server.socket = Mock()
                    server.control_failed = False
                    captured["gate"] = lifecycle
                    def handle(*_):
                        handler = object.__new__(handler_type)
                        handler.path, handler.headers = "/chat", {"Content-Length": "2"}
                        handler.rfile, handler._respond = BytesIO(b"{}"), Mock()
                        try:
                            handler.do_POST()
                        except ValueError as error:
                            worker_errors.append(type(error))
                    server.process_request_thread = handle
                    def interrupt(**_):
                        server.process_request(None, None)
                        if not started.wait(timeout=3):
                            raise RuntimeError("test_handler_did_not_start")
                        raise KeyboardInterrupt()
                    server.serve_forever = interrupt
                    return server
                def run():
                    try:
                        with patch("ssr_chat.api.LifecycleHTTPServer", side_effect=construct), redirect_stdout(StringIO()):
                            serve(runtime, 8776, lifecycle_dir=Path(directory) / "generation")
                    except BaseException as error:
                        serving_errors.append(type(error))
                    finally:
                        returned.set()
                serving = threading.Thread(target=run)
                serving.start()
                try:
                    self.assertTrue(started.wait(timeout=3))
                    self.assertFalse(returned.wait(timeout=0.05))
                    self.assertEqual(captured["gate"].snapshot()["active"], 1)
                finally:
                    release.set()
                    serving.join(timeout=3)
                self.assertFalse(serving.is_alive())
                self.assertEqual(serving_errors, [KeyboardInterrupt])
                self.assertEqual(worker_errors, [ValueError] if fail_work else [])
                status = read_control_status(Path(directory) / "generation")
                self.assertEqual(status["state"], "stopped")
                self.assertEqual(status["active"], 0)
                self.assertEqual(status["completed"], 1)
                self.assertEqual(status["errors"], int(fail_work))


if __name__ == "__main__":
    unittest.main()
