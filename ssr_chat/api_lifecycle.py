"""Opt-in, process-local request accounting and local file lifecycle control.

This controls only the server instance that creates the control directory. It is
not a process killer, a service registry, or authority to change another server.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Any
from uuid import uuid4


class RequestLifecycle:
    """One-way admission gate. Every admitted lease must settle exactly once."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: set[int] = set()
        self._accepted = self._completed = self._errors = self._rejected = 0
        self._state = "ready"
        self._stop_requested = False

    def admit(self) -> int | None:
        with self._lock:
            if self._state != "ready":
                self._rejected += 1
                return None
            self._accepted += 1
            self._active.add(self._accepted)
            return self._accepted

    def complete(self, lease: int, *, error: bool = False) -> None:
        with self._lock:
            if lease not in self._active:
                raise ValueError("unknown_or_completed_request_lease")
            self._active.remove(lease)
            self._completed += 1
            self._errors += int(error)

    def drain(self) -> None:
        with self._lock:
            if self._state == "ready":
                self._state = "draining"

    def request_stop(self) -> None:
        with self._lock:
            self.drain()
            self._stop_requested = True

    @property
    def stop_ready(self) -> bool:
        with self._lock:
            return self._state == "draining" and self._stop_requested and not self._active

    def mark_stopped(self) -> None:
        with self._lock:
            if self._active or self._state == "ready":
                raise RuntimeError("requests_not_drained")
            self._state = "stopped"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "ready": self._state == "ready",
                    "accepted": self._accepted, "completed": self._completed,
                    "active": len(self._active), "errors": self._errors,
                    "rejected": self._rejected, "stop_requested": self._stop_requested}


def _identity(path: Path, *, directory: bool = False) -> tuple[int, int]:
    info = path.lstat()
    if ((directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and not stat.S_ISREG(info.st_mode)) or not info.st_ino):
        raise RuntimeError("invalid_lifecycle_file_identity")
    return info.st_dev, info.st_ino


def _local_path(value: str | Path) -> Path:
    text = str(value)
    if text.startswith(("\\\\", "//")):
        raise ValueError("lifecycle_directory_must_be_local")
    path = Path(value).absolute()
    # Reject mapped network drives too. This check is intentionally Windows-only.
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("lifecycle_directory_must_be_local")
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("lifecycle_linked_path_refused")
    return path.resolve()


def _strict_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > 16_384:
        raise ValueError("lifecycle_command_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_lifecycle_key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_lifecycle_value")))
    if not isinstance(value, dict):
        raise ValueError("lifecycle_command_must_be_object")
    return value


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise RuntimeError("lifecycle_linked_file_refused")
    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        for attempt in range(10):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                # Windows readers can briefly deny replace while status is open.
                time.sleep(0.02)
    finally:
        temp.unlink(missing_ok=True)


class FileLifecycleControl:
    """Exclusive per-start directory with immutable owner binding and commands.

    The directory's parent must already exist. Operator ACLs must limit access
    to the intended local user. This is not protection against that same user.
    """

    def __init__(self, lifecycle: RequestLifecycle, directory: str | Path, port: int) -> None:
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("invalid_lifecycle_port")
        self.lifecycle = lifecycle
        self.directory = _local_path(directory)
        self.directory.mkdir(exist_ok=False)
        self._directory_id = _identity(self.directory, directory=True)
        self._owner = {"schema_version": 1, "generation": uuid4().hex,
                       "control_token": secrets.token_urlsafe(32), "pid": os.getpid(),
                       "started_at": datetime.now(timezone.utc).isoformat(),
                       "directory": str(self.directory), "bind": "127.0.0.1", "port": port}
        self._owner_bytes = (json.dumps(self._owner, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._owner_path = self.directory / "owner.json"
        with self._owner_path.open("xb") as output:
            output.write(self._owner_bytes)
            output.flush()
            os.fsync(output.fileno())
        self._owner_id = _identity(self._owner_path)
        self._last_digest: str | None = None
        self._seen_requests: set[str] = set()
        self._last_command: dict[str, Any] | None = None
        self._control_error: str | None = None
        self.publish()

    def _verify(self) -> None:
        if (_local_path(self.directory) != self.directory
                or _identity(self.directory, directory=True) != self._directory_id
                or _identity(self._owner_path) != self._owner_id
                or self._owner_path.read_bytes() != self._owner_bytes):
            raise RuntimeError("lifecycle_owner_binding_changed")

    def publish(self) -> None:
        self._verify()
        _replace_json(self.directory / "status.json", {
            "schema_version": 1, "generation": self._owner["generation"],
            "pid": self._owner["pid"], "bind": "127.0.0.1", "port": self._owner["port"],
            **self.lifecycle.snapshot(), "last_command": self._last_command,
            "control_error": self._control_error,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })

    def poll(self) -> None:
        """Check one command, then publish. Invalid commands never grant control."""
        self._verify()
        path = self.directory / "command.json"
        if path.exists() or path.is_symlink():
            try:
                _identity(path)
                with path.open("rb") as source:
                    raw = source.read(16_385)
                digest = hashlib.sha256(raw).hexdigest()
                if digest != self._last_digest:
                    self._last_digest = digest
                    self._accept(_strict_object(raw))
            except (ValueError, OSError, RuntimeError, UnicodeError):
                self._control_error = "invalid_lifecycle_command"
        self.publish()

    def _accept(self, command: dict[str, Any]) -> None:
        fields = {"generation", "control_token", "request_id", "action"}
        if (set(command) != fields or command["generation"] != self._owner["generation"]
                or not isinstance(command["control_token"], str)
                or not hmac.compare_digest(command["control_token"].encode("utf-8"), self._owner["control_token"].encode("utf-8"))
                or not isinstance(command["request_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", command["request_id"]) is None
                or command["action"] not in ("drain", "stop")):
            raise ValueError("invalid_lifecycle_command")
        if command["request_id"] in self._seen_requests:
            raise ValueError("replayed_lifecycle_command")
        self._seen_requests.add(command["request_id"])
        if command["action"] == "drain":
            self.lifecycle.drain()
        else:
            self.lifecycle.request_stop()
        self._control_error = None
        self._last_command = {"request_id": command["request_id"], "action": command["action"],
                              "accepted": True}


def write_control_command(directory: str | Path, action: str) -> str:
    """Local operator helper; return the request ID to match in status.json."""
    if action not in ("drain", "stop"):
        raise ValueError("invalid_lifecycle_action")
    root = _local_path(directory)
    _identity(root, directory=True)
    owner_path = root / "owner.json"
    _identity(owner_path)
    owner = _strict_object(owner_path.read_bytes())
    if owner.get("directory") != str(root) or owner.get("bind") != "127.0.0.1":
        raise ValueError("invalid_lifecycle_owner")
    request_id = uuid4().hex
    _replace_json(root / "command.json", {"generation": owner["generation"],
                  "control_token": owner["control_token"], "request_id": request_id, "action": action})
    return request_id


def read_control_status(directory: str | Path) -> dict[str, Any]:
    root = _local_path(directory)
    _identity(root, directory=True)
    owner_path, status_path = root / "owner.json", root / "status.json"
    _identity(owner_path)
    _identity(status_path)
    owner = _strict_object(owner_path.read_bytes())
    status = _strict_object(status_path.read_bytes())
    if (owner.get("directory") != str(root) or owner.get("bind") != "127.0.0.1"
            or any(status.get(key) != owner.get(key) for key in ("generation", "pid", "port", "bind"))):
        raise ValueError("lifecycle_status_binding_mismatch")
    return status


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Control one opt-in local API generation through its private directory.")
    parser.add_argument("action", choices=("status", "drain", "stop"))
    parser.add_argument("--directory", required=True)
    args = parser.parse_args(argv)
    try:
        result = (read_control_status(args.directory) if args.action == "status" else
                  {"status": "submitted", "action": args.action,
                   "request_id": write_control_command(args.directory, args.action)})
    except (OSError, ValueError, RuntimeError, KeyError, UnicodeError):
        print(json.dumps({"status": "error", "reason_code": "lifecycle_control_failed"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
