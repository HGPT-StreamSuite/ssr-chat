"""Real loopback HTTP verification; fixture model and isolated temporary state."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from ssr_chat.api import LifecycleHTTPServer, handler_for
from ssr_chat.api_lifecycle import FileLifecycleControl, RequestLifecycle, read_control_status, write_control_command
from ssr_chat.application import build_runtime


class LifecycleHttpTests(unittest.TestCase):
    def test_drain_rejects_new_work_and_stop_waits_for_admitted_response(self):
        temp_root = Path(__file__).resolve().parents[1] / ".test-temp"
        temp_root.mkdir(exist_ok=True)
        started, release = threading.Event(), threading.Event()

        class BlockingModel:
            calls = 0

            def generate(self, prompt):
                self.calls += 1
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test_release_timeout")
                return "這是受控本機回覆。"

        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            root = Path(directory)
            model = BlockingModel()
            runtime = build_runtime(root / "state", model=model)
            lifecycle = RequestLifecycle()
            # Bind an ephemeral port first, then publish the actual bound port.
            server = LifecycleHTTPServer(("127.0.0.1", 0), handler_for(runtime, lifecycle=lifecycle), lifecycle, None)
            control_dir = root / "control"
            server.control = FileLifecycleControl(lifecycle, control_dir, server.server_port)
            thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
            thread.start()

            def request(method, path, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                try:
                    payload = json.dumps(body).encode() if body is not None else None
                    connection.request(method, path, body=payload, headers={"Content-Type": "application/json"})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            def wait_for_state(state):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = read_control_status(control_dir)
                    if status["state"] == state:
                        return status
                    threading.Event().wait(0.01)
                self.fail("lifecycle state did not become " + state)

            try:
                self.assertEqual(request("GET", "/health")[1]["status"], "ready")
                event = json.loads((Path(__file__).resolve().parents[1] / "fixtures/events/general.json").read_text(encoding="utf-8"))
                with ThreadPoolExecutor(max_workers=1) as executor:
                    active = executor.submit(request, "POST", "/chat", event)
                    self.assertTrue(started.wait(3))
                    write_control_command(control_dir, "drain")
                    status = wait_for_state("draining")
                    self.assertEqual((status["accepted"], status["active"], status["completed"]), (1, 1, 0))
                    self.assertEqual(request("GET", "/health")[0], 503)
                    self.assertEqual(request("POST", "/chat", event)[0], 503)
                    write_control_command(control_dir, "stop")
                    deadline = time.monotonic() + 3
                    while not lifecycle.snapshot()["stop_requested"] and time.monotonic() < deadline:
                        threading.Event().wait(0.01)
                    self.assertTrue(lifecycle.snapshot()["stop_requested"])
                    self.assertTrue(thread.is_alive())
                    self.assertFalse(active.done())
                    release.set()
                    code, result = active.result(timeout=5)
                    self.assertEqual(code, 200)
                    self.assertEqual(result["send_result"]["status"], "dry_run_not_sent")
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(model.calls, 1)
                settled = lifecycle.snapshot()
                self.assertEqual((settled["accepted"], settled["completed"], settled["active"], settled["rejected"]), (1, 1, 0, 1))
            finally:
                release.set()
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                lifecycle.drain()
                lifecycle.mark_stopped()
                server.control.publish()
            self.assertEqual(read_control_status(control_dir)["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
