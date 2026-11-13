"""Warm component workers behave like the ordinary subprocess they replace, but keep imported state."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from ssr_chat.warm_pool import (
    Entry, WarmCrashed, WarmPool, WarmTimeout, WarmUnavailable, parse_command,
)

LIB = '''COUNT = 0


def bump():
    global COUNT
    COUNT += 1
    return COUNT
'''

TOOL = '''import json
import os
import sys
import time

import counter_lib

payload = json.loads(sys.stdin.read())
number = counter_lib.bump()
mode = payload.get("mode")
if mode == "exit":
    raise SystemExit(payload["code"])
if mode == "exit_text":
    raise SystemExit("fatal text")
if mode == "raise":
    raise RuntimeError("boom")
if mode == "sleep":
    time.sleep(payload["seconds"])
if mode == "die":
    os._exit(3)
if mode == "fd1":
    os.write(1, b"stray bytes that must not reach the protocol")
if mode == "stderr":
    sys.stderr.write("diagnostic\\n")
print(json.dumps({"n": number, "argv": sys.argv[1:], "text": payload.get("text"), "pid": os.getpid(),
                  "cwd": os.path.basename(os.getcwd())}, ensure_ascii=False))
'''


class ParseCommandTests(unittest.TestCase):
    def test_accepts_plain_script_and_module_forms(self):
        entry = parse_command(["python.exe", "-B", "-X", "utf8", "source/ssr.py", "--config", "c.json", "--request", "-"], "C:/repo")
        self.assertEqual((entry.kind, entry.args), ("script", ("--config", "c.json", "--request", "-")))
        self.assertEqual(Path(entry.target), Path("C:/repo/source/ssr.py"))
        module = parse_command(["/usr/bin/python3.13", "-Xutf8", "-m", "source.hgpt_ssr", "--input", "-"], "/repo")
        self.assertEqual((module.kind, module.target, module.args), ("module", "source.hgpt_ssr", ("--input", "-")))

    def test_unknown_shapes_stay_ordinary_subprocesses(self):
        for command in ([], ["uv", "run", "python", "x.py"], ["python", "-I", "x.py"], ["python", "-c", "print(1)"],
                        ["python", "-S", "x.py"], ["python", "-B"], ["python", "-m"], ["node", "x.js"]):
            with self.subTest(command=command):
                self.assertIsNone(parse_command(command, "/repo"))
        self.assertIsNone(parse_command(["python", "x.py"], None))


class WarmPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "counter_lib.py").write_text(LIB, encoding="utf-8")
        (self.root / "tool.py").write_text(TOOL, encoding="utf-8")
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(TOOL, encoding="utf-8")
        self.pool = WarmPool()
        self.addCleanup(self.pool.close)
        self.env = {key: value for key, value in os.environ.items()
                    if key.lower() in {"systemroot", "path", "temp", "tmp", "windir"}}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")

    def script(self, *args):
        return Entry(sys.executable, "script", str(self.root / "tool.py"), tuple(args), str(self.root))

    def module(self, *args):
        return Entry(sys.executable, "module", "pkg", tuple(args), str(self.root))

    def call(self, entry, payload, timeout=20):
        import json
        return self.pool.call(entry, json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout, env=self.env)

    def cold(self, args, payload, *, module=False):
        import json
        command = [sys.executable, "-B", "-X", "utf8", *(["-m", "pkg"] if module else ["tool.py"]), *args]
        return subprocess.run(command, cwd=self.root, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                              capture_output=True, env=self.env, check=False)

    def test_imported_module_state_survives_between_calls(self):
        import json
        results = [json.loads(self.call(self.script("--a", "1"), {"text": "x"}).stdout) for _ in range(3)]
        self.assertEqual([item["n"] for item in results], [1, 2, 3])
        self.assertEqual(len({item["pid"] for item in results}), 1)
        self.assertEqual(results[0]["argv"], ["--a", "1"])
        self.assertEqual(results[0]["cwd"], self.root.name)

    def test_module_entry_runs_like_python_dash_m(self):
        import json
        first = json.loads(self.call(self.module("--m"), {"text": "x"}).stdout)
        second = json.loads(self.call(self.module("--m"), {"text": "x"}).stdout)
        self.assertEqual((first["n"], second["n"], first["argv"]), (1, 2, ["--m"]))

    def test_output_exit_code_and_stderr_match_an_ordinary_process(self):
        cases = [({"text": "plain"}, ()), ({"text": "中文與 emoji 😀"}, ()), ({"mode": "exit", "code": 5}, ()),
                 ({"mode": "exit_text"}, ()), ({"mode": "stderr", "text": "s"}, ())]
        for payload, args in cases:
            with self.subTest(payload=payload):
                warm, cold = self.call(self.script(*args), payload), self.cold(args, payload)
                self.assertEqual(warm.exit_code, cold.returncode)
                if payload.get("mode") in {"exit", "exit_text"}:
                    self.assertEqual(warm.stdout, cold.stdout)
                    self.assertEqual(warm.stderr, cold.stderr)
                else:
                    # pid and the module counter legitimately differ; everything else is byte-identical.
                    import json
                    left, right = json.loads(warm.stdout), json.loads(cold.stdout)
                    for key in ("pid", "n"):
                        left.pop(key), right.pop(key)
                    self.assertEqual(left, right)
                    self.assertEqual(warm.stderr, cold.stderr)
                    self.assertTrue(warm.stdout.endswith(os.linesep.encode()))

    def test_uncaught_exception_is_exit_one_with_traceback_on_stderr(self):
        done = self.call(self.script(), {"mode": "raise"})
        self.assertEqual(done.exit_code, 1)
        self.assertIn(b"RuntimeError: boom", done.stderr)
        self.assertEqual(done.stdout, b"")
        self.assertEqual(self.cold((), {"mode": "raise"}).returncode, 1)

    def test_stray_writes_to_the_real_stdout_cannot_corrupt_results(self):
        import json
        self.call(self.script(), {"mode": "fd1"})
        again = json.loads(self.call(self.script(), {"text": "after"}).stdout)
        self.assertEqual((again["n"], again["text"]), (2, "after"))

    def test_timeout_kills_the_worker_and_the_next_call_starts_a_fresh_one(self):
        import json
        first = json.loads(self.call(self.script(), {"text": "a"}).stdout)
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.call(self.script(), {"mode": "sleep", "seconds": 30}, timeout=0.7)
        self.assertIsInstance(caught.exception, WarmTimeout)
        self.assertLess(time.monotonic() - started, 10)
        fresh = json.loads(self.call(self.script(), {"text": "b"}).stdout)
        self.assertEqual(fresh["n"], 1)
        self.assertNotEqual(fresh["pid"], first["pid"])

    def test_worker_death_is_reported_and_recovered_from(self):
        import json
        with self.assertRaises(WarmCrashed):
            self.call(self.script(), {"mode": "die"})
        self.assertTrue(issubclass(WarmCrashed, WarmUnavailable) and issubclass(WarmUnavailable, OSError))
        self.assertEqual(json.loads(self.call(self.script(), {"text": "x"}).stdout)["n"], 1)

    def test_missing_interpreter_is_a_start_failure(self):
        entry = Entry(str(self.root / "no-such-python.exe"), "script", str(self.root / "tool.py"), (), str(self.root))
        with self.assertRaises(WarmUnavailable):
            self.call(entry, {})

    def test_concurrent_calls_to_one_worker_are_serialised(self):
        import json
        numbers, errors = [], []

        def run():
            try:
                numbers.append(json.loads(self.call(self.script(), {"mode": "sleep", "seconds": 0.1}).stdout)["n"])
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(numbers), [1, 2, 3, 4])

    def test_close_stops_every_worker(self):
        self.call(self.script(), {"text": "x"})
        workers = [worker.process for worker in self.pool._workers.values()]
        self.assertTrue(all(process.poll() is None for process in workers))
        self.pool.close()
        self.assertTrue(all(process.poll() is not None for process in workers))


if __name__ == "__main__":
    unittest.main()
