"""Warm component interpreters: run the same CLI entry points repeatedly inside one long-lived Python process.

The planner, dispatcher, collector and native readers are independent repositories whose packages are all
named ``hgpt_ssr``, so they cannot be imported side by side. Instead each distinct entry (interpreter, script or
module, cwd) gets its own worker process that executes the unchanged entry point with ``runpy`` for every call.
Same code, same argv, same JSON on stdin/stdout, but module-level state such as an already loaded embedding model
survives between calls.

Protocol with ``warm_worker.py``: 4-byte big-endian length + ASCII JSON frame, both directions.
"""
from __future__ import annotations

import atexit
import base64
import json
import queue
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Sequence

WORKER = Path(__file__).with_name("warm_worker.py")
MAX_FRAME = 32_000_000


class WarmUnavailable(OSError):
    """The worker could not be started or written to (maps to the same errors as a failed spawn)."""


class WarmCrashed(WarmUnavailable):
    """The worker exited after it accepted the request."""


class WarmTimeout(subprocess.TimeoutExpired):
    """The call exceeded its time limit; the worker was killed and restarts on the next call."""

    def __init__(self, timeout: float) -> None:
        super().__init__("warm-worker", timeout)


class WarmResult(NamedTuple):
    exit_code: int
    stdout: bytes
    stderr: bytes
    pid: int


@dataclass(frozen=True)
class Entry:
    python: str
    kind: str          # "script" or "module"
    target: str        # absolute script path, or dotted module name
    args: tuple[str, ...]
    cwd: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.python, self.kind, self.target, self.cwd)


def parse_command(command: Sequence[str], cwd: str | Path | None) -> Entry | None:
    """Recognise ``python [-B] [-X utf8] (script.py | -m module) args...``; anything else returns None.

    Only flags that do not change import or isolation semantics are accepted, so a command that is not
    understood keeps running as an ordinary subprocess.
    """
    if not command or cwd is None or not Path(command[0]).stem.lower().startswith("python"):
        return None
    index = 1
    while index < len(command):
        flag = command[index]
        if flag == "-B" or flag == "-Xutf8":
            index += 1
        elif flag == "-X" and index + 1 < len(command) and command[index + 1] == "utf8":
            index += 2
        else:
            break
    if index >= len(command):
        return None
    if command[index] == "-m":
        if index + 1 >= len(command):
            return None
        return Entry(command[0], "module", command[index + 1], tuple(command[index + 2:]), str(cwd))
    if command[index].startswith("-"):
        return None
    return Entry(command[0], "script", str(Path(cwd) / command[index]), tuple(command[index + 1:]), str(cwd))


def read_frame(stream) -> dict | None:
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack(">I", head)
    if size > MAX_FRAME:
        raise ValueError("frame_too_large")
    body = stream.read(size)
    if len(body) < size:
        return None
    return json.loads(body.decode("ascii"))


def write_frame(stream, value: dict) -> None:
    body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")
    stream.write(struct.pack(">I", len(body)) + body)
    stream.flush()


class _Worker:
    def __init__(self, entry: Entry, hook: str | None) -> None:
        self.entry, self.hook = entry, hook
        self.lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.responses: queue.Queue = queue.Queue()

    def _spawn(self, env: dict[str, str]) -> None:
        try:
            process = subprocess.Popen(
                [self.entry.python, "-B", "-X", "utf8", str(WORKER)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=self.entry.cwd, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise WarmUnavailable("warm_worker_start_failed") from exc
        responses: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, args=(process, responses), daemon=True).start()
        self.process, self.responses = process, responses

    @staticmethod
    def _pump(process: subprocess.Popen, responses: queue.Queue) -> None:
        try:
            while (frame := read_frame(process.stdout)) is not None:
                responses.put(frame)
        except (OSError, ValueError):
            pass
        finally:
            responses.put(None)

    def kill(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        for stream in (process.stdin,):
            try:
                stream.close()
            except OSError:
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:  # the pump thread sees EOF once the process is gone
            process.stdout.close()
        except (OSError, ValueError):
            pass

    def call(self, entry: Entry, stdin: bytes, timeout: float, env: dict[str, str]) -> WarmResult:
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=max(timeout, 0)):
            raise WarmTimeout(timeout)
        try:
            if self.process is None or self.process.poll() is not None:
                self.kill()
                self._spawn(env)
            request = {"args": list(entry.args), "stdin": base64.b64encode(stdin).decode("ascii"),
                       "entry": {"kind": entry.kind, "target": entry.target, "cwd": entry.cwd},
                       "hook": self.hook}
            try:
                write_frame(self.process.stdin, request)
            except (OSError, ValueError) as exc:
                self.kill()
                raise WarmUnavailable("warm_worker_write_failed") from exc
            try:
                frame = self.responses.get(timeout=max(deadline - time.monotonic(), 0))
            except queue.Empty:
                self.kill()
                raise WarmTimeout(timeout) from None
            if frame is None:
                self.kill()
                raise WarmCrashed("warm_worker_exited")
            return WarmResult(int(frame["exit"]), base64.b64decode(frame["stdout"]),
                              base64.b64decode(frame["stderr"]), self.process.pid)
        finally:
            self.lock.release()


class WarmPool:
    """One long-lived worker per distinct entry. Calls to the same worker are serialised."""

    def __init__(self) -> None:
        self._workers: dict[tuple, _Worker] = {}
        self._guard = threading.Lock()
        atexit.register(self.close)

    def call(self, entry: Entry, stdin: bytes, timeout: float, *, env: dict[str, str],
             hook: str | None = None) -> WarmResult:
        key = (*entry.key, hook)
        with self._guard:
            worker = self._workers.get(key)
            if worker is None:
                worker = self._workers[key] = _Worker(entry, hook)
        return worker.call(entry, stdin, timeout, env)

    def close(self) -> None:
        with self._guard:
            workers, self._workers = list(self._workers.values()), {}
        for worker in workers:
            worker.kill()
