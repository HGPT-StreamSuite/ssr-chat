"""Bounded authenticated Twitch receive -> local model -> dry-run window.

There is deliberately no public-send adapter or synthetic replay entry point.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import re
import sqlite3
from stat import S_ISREG
import threading
import time
from typing import Any

from .analysis import QuestionAnalyzer, TriggerDetector
from .application import build_runtime
from .input_validation import EventValidator
from .models import QueryResult, QuestionAnalysis
from .ollama_model import OllamaModelClient
from .output import DryRunOutput

CLIENT_ID = "ywe81rovont7bw585tofv8jmv8xvif"
BOT_ID = "99567238"
CHANNEL_ID = "928757513"
MODEL = "qwen3.5:9b-q4_K_M"
MODEL_DIGEST = "56671c2ab9385f9cfcb404638e32cd62d88e3501d44822208363c010179a3c90"
SOURCE = "twitch-eventsub"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class LiveError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _object(path: Path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LiveError("duplicate_json_key")
            result[key] = value
        return result
    if path.stat().st_size > 65536:
        raise LiveError("config_too_large")
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(LiveError("invalid_json_number")))
    if not isinstance(value, dict):
        raise LiveError("config_must_be_object")
    return value


def _external_path(value: Any, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise LiveError("explicit_external_path_required")
    path = Path(value)
    path = path if path.is_absolute() else base / path
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or getattr(ancestor, "is_junction", lambda: False)():
            raise LiveError("linked_path_not_allowed")
    path = path.resolve()
    if path.is_relative_to(Path(__file__).resolve().parents[1]):
        raise LiveError("live_state_and_tokens_must_be_outside_source")
    return path


@dataclass(frozen=True)
class LiveConfig:
    token_path: Path
    state_dir: Path
    client_type: str = "public"
    duration_seconds: int = 600
    max_processed: int = 1
    tester_user_id: str = CHANNEL_ID
    local_ai_input: bool = True

    @classmethod
    def read(cls, path: str | Path) -> "LiveConfig":
        path = Path(path).resolve()
        value = _object(path)
        required = {"schema_version", "client_id", "bot_user_id", "broadcaster_user_id", "tester_user_id",
                    "token_path", "state_dir", "client_type", "duration_seconds", "max_processed", "prefix",
                    "local_ai_input", "output", "model_endpoint", "model_name", "model_digest"}
        if set(value) != required:
            raise LiveError("config_fields_mismatch")
        exact = {"schema_version": "1", "client_id": CLIENT_ID, "bot_user_id": BOT_ID,
                 "broadcaster_user_id": CHANNEL_ID, "tester_user_id": CHANNEL_ID, "prefix": "!ask",
                 "output": "dry_run", "model_endpoint": "http://127.0.0.1:11435", "model_name": MODEL,
                 "model_digest": MODEL_DIGEST}
        if any(value[k] != v for k, v in exact.items()):
            raise LiveError("config_identity_or_output_binding_mismatch")
        if type(value["local_ai_input"]) is not bool or not value["local_ai_input"]:
            raise LiveError("explicit_local_ai_input_required")
        if type(value["duration_seconds"]) is not int or not 1 <= value["duration_seconds"] <= 600:
            raise LiveError("duration_outside_approved_window")
        if type(value["max_processed"]) is not int or value["max_processed"] != 1:
            raise LiveError("exactly_one_model_attempt_limit_required")
        if value["client_type"] not in {"public", "confidential"}:
            raise LiveError("unsupported_client_type")
        token = _external_path(value["token_path"], path.parent)
        state = _external_path(value["state_dir"], path.parent)
        if token.is_relative_to(state) or token == state:
            raise LiveError("token_must_be_separate_from_runtime_state")
        return cls(token, state, value["client_type"], value["duration_seconds"])

    def owner(self) -> dict:
        return {"schema_version": "1", "kind": "owned_live_twitch_dry_run", "client_id": CLIENT_ID,
                "bot_user_id": BOT_ID, "broadcaster_user_id": CHANNEL_ID, "tester_user_id": self.tester_user_id,
                "source": SOURCE, "prefix": "!ask", "max_processed": self.max_processed,
                "local_ai_input": self.local_ai_input, "output": "dry_run"}


def validate_identity(identity: Any) -> dict:
    if not isinstance(identity, dict) or identity.get("validated") is not True:
        raise LiveError("validated_twitch_identity_required")
    if identity.get("client_id") != CLIENT_ID or identity.get("user_id") != BOT_ID:
        raise LiveError("validated_twitch_identity_mismatch")
    if identity.get("login") != "dubheorigin" or "user:read:chat" not in identity.get("scopes", []):
        raise LiveError("validated_twitch_login_or_read_scope_mismatch")
    return {key: identity[key] for key in ("client_id", "user_id", "login", "scopes", "validated")}


class DisabledKnowledge:
    def query(self, purpose, question, context_messages):
        return QueryResult(purpose=purpose, status="tool_error", diagnostic="live_knowledge_adapter_disabled")


class DisabledWeather:
    def fetch(self, question, context_messages):
        raise LiveError("live_weather_adapter_disabled")


class GeneralAnalyzer:
    def analyze(self, text, snapshot):
        return QuestionAnalysis(intents=("general_chat",), query_text=TriggerDetector().remove_prefix(text),
                                snapshot_id=snapshot.snapshot_id, queries=())


def build_live_runtime(config: LiveConfig, model=None):
    runtime = build_runtime(config.state_dir, knowledge_reader=DisabledKnowledge(), weather_adapter=DisabledWeather(),
                            analyzer=GeneralAnalyzer(), model=model if model is not None else OllamaModelClient(
                                endpoint="http://127.0.0.1:11435", model=MODEL, timeout=90), output=DryRunOutput())
    runtime.validator = EventValidator(allowed_sources={SOURCE})
    runtime.bot_id = BOT_ID
    runtime._execution_profile.update(knowledge="disabled", weather="disabled", output="dry_run")
    return runtime


class LiveLedger:
    APPLICATION_ID = 0x53534C56

    def __init__(self, config: LiveConfig):
        self.path = config.state_dir / "live_receipts.sqlite3"
        self._owner_path = config.state_dir / "owner.json"
        self._binding = config.owner()
        self._quarantined = False
        if config.state_dir.exists():
            if not self._owner_path.is_file() or not self.path.is_file():
                raise LiveError("existing_state_not_owned_by_this_live_window")
            owner = _object(self._owner_path)
            self._file_identity = self._identity(self.path)
            if owner != {**self._binding, "ledger_file_identity": list(self._file_identity)}:
                raise LiveError("existing_state_not_owned_by_this_live_window")
        else:
            config.state_dir.mkdir(parents=True, exist_ok=False)
            # Create once. Every later connection uses mode=rw and must never
            # replace a missing deduplication history with a new empty store.
            with closing(sqlite3.connect(self.path.as_uri() + "?mode=rwc", uri=True, timeout=3)) as db, db:
                db.execute("CREATE TABLE metadata (owner_digest TEXT NOT NULL)")
                db.execute("INSERT INTO metadata VALUES (?)", (_digest(self._binding),))
                db.execute("CREATE TABLE receipts (event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, status TEXT NOT NULL)")
                db.execute("CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL)")
                db.execute(f"PRAGMA application_id={self.APPLICATION_ID}")
                db.execute("PRAGMA user_version=1")
            self._file_identity = self._identity(self.path)
            with self._owner_path.open("xb") as handle:
                handle.write(_encoded({**self._binding, "ledger_file_identity": list(self._file_identity)}))
                handle.flush()
                os.fsync(handle.fileno())
        self._owner_identity = self._identity(self._owner_path)
        with self.connect() as db:
            pass

    @staticmethod
    def _identity(path):
        info = path.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:
            raise LiveError("live_ledger_file_identity_unavailable")
        return str(info.st_dev), str(info.st_ino)

    def _verify_continuity(self):
        try:
            if (self._quarantined or self._identity(self.path) != self._file_identity
                    or self._identity(self._owner_path) != self._owner_identity
                    or _object(self._owner_path) != {**self._binding, "ledger_file_identity": list(self._file_identity)}):
                raise LiveError("live_ledger_continuity_unavailable")
        except (OSError, ValueError):
            self._quarantined = True
            raise LiveError("live_ledger_continuity_unavailable") from None

    @contextmanager
    def connect(self):
        self._verify_continuity()
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=3)
            db.execute("PRAGMA synchronous=FULL")
            self._verify_continuity()
            with db:
                if (db.execute("PRAGMA application_id").fetchone()[0] != self.APPLICATION_ID
                        or db.execute("PRAGMA user_version").fetchone()[0] != 1
                        or {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} != {"metadata", "receipts", "deliveries"}
                        or db.execute("SELECT owner_digest FROM metadata").fetchall() != [(_digest(self._binding),)]):
                    self._quarantined = True
                    raise LiveError("live_ledger_continuity_unavailable")
                yield db
                self._verify_continuity()
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", 0) & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                self._quarantined = True
            raise LiveError("live_ledger_unavailable") from None
        finally:
            if db is not None:
                db.close()

    def attempts(self) -> int:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]

    def reserve(self, event_id: str, delivery_id: str, payload_hash: str) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            delivery = db.execute("SELECT event_id,payload_hash FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if delivery is not None and delivery != (event_id, payload_hash):
                return "delivery_id_conflict"
            receipt = db.execute("SELECT payload_hash,status FROM receipts WHERE event_id=?", (event_id,)).fetchone()
            if receipt:
                if receipt[0] != payload_hash:
                    return "event_id_conflict"
                return "duplicate_event" if receipt[1] == "complete" else "duplicate_event_uncertain"
            if db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] >= 1:
                return "model_attempt_limit_reached"
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (event_id, payload_hash, "reserved"))
            db.execute("INSERT INTO deliveries VALUES (?,?,?)", (delivery_id, event_id, payload_hash))
        return "reserved"

    def finish(self, event_id: str, status: str):
        with self.connect() as db:
            db.execute("UPDATE receipts SET status=? WHERE event_id=?", (status, event_id))


def normalize_notification(envelope: Any, binding: Any, config: LiveConfig, *, now=None) -> tuple[dict, dict]:
    expected = {"client_id": CLIENT_ID, "reader_user_id": BOT_ID, "broadcaster_user_id": CHANNEL_ID,
                "transport_authenticated": True}
    if not isinstance(binding, dict) or any(binding.get(k) != v for k, v in expected.items()):
        raise LiveError("authenticated_transport_binding_required")
    if any(not isinstance(binding.get(k), str) or not binding[k] for k in ("session_id", "subscription_id")):
        raise LiveError("transport_session_or_subscription_missing")
    if not isinstance(envelope, dict) or set(envelope) != {"metadata", "payload"}:
        raise LiveError("notification_must_be_object")
    metadata, payload = envelope.get("metadata"), envelope.get("payload")
    if not isinstance(metadata, dict) or not isinstance(payload, dict):
        raise LiveError("notification_envelope_invalid")
    if any(metadata.get(k) != v for k, v in {"message_type": "notification", "subscription_type": "channel.chat.message", "subscription_version": "1"}.items()):
        raise LiveError("notification_type_not_supported")
    delivery_id = metadata.get("message_id")
    if not isinstance(delivery_id, str) or not _ID.fullmatch(delivery_id):
        raise LiveError("invalid_delivery_id")
    try:
        stamp = datetime.fromisoformat(metadata["message_timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    except (TypeError, ValueError, KeyError, AttributeError):
        raise LiveError("invalid_notification_timestamp") from None
    if not -30 <= age <= 300:
        raise LiveError("notification_timestamp_outside_window")
    sub, event = payload.get("subscription"), payload.get("event")
    if not isinstance(sub, dict) or not isinstance(event, dict):
        raise LiveError("notification_payload_invalid")
    transport = sub.get("transport")
    if (not isinstance(transport, dict) or sub.get("id") != binding["subscription_id"] or sub.get("type") != "channel.chat.message"
        or sub.get("version") != "1" or sub.get("status") != "enabled"
        or sub.get("condition") != {"broadcaster_user_id": CHANNEL_ID, "user_id": BOT_ID}
        or transport.get("method") != "websocket"
        or transport.get("session_id") != binding["session_id"]):
        raise LiveError("subscription_or_session_binding_mismatch")
    if event.get("broadcaster_user_id") != CHANNEL_ID:
        raise LiveError("channel_mismatch")
    if event.get("chatter_user_id") == BOT_ID:
        raise LiveError("self_event_loop_guard")
    if event.get("chatter_user_id") != config.tester_user_id:
        raise LiveError("chatter_not_approved_tester")
    if any(event.get(k) is not None and event.get(k) != "" for k in ("source_broadcaster_user_id", "source_message_id")):
        raise LiveError("shared_chat_origin_not_supported")
    message_id, message = event.get("message_id"), event.get("message")
    if not isinstance(message_id, str) or not _ID.fullmatch(message_id) or not isinstance(message, dict):
        raise LiveError("invalid_chat_message")
    text, fragments = message.get("text"), message.get("fragments")
    if not isinstance(text, str) or not 1 <= len(text) <= 2000 or not isinstance(fragments, list):
        raise LiveError("invalid_chat_text_or_fragments")
    if not all(isinstance(f, dict) and isinstance(f.get("text"), str) for f in fragments) or "".join(f["text"] for f in fragments) != text:
        raise LiveError("chat_fragments_mismatch")
    if not re.match(r"^!ask(?:\s+|[:：]\s*)\S", text):
        raise LiveError("explicit_test_command_required")
    question = TriggerDetector().remove_prefix(text)
    if QuestionAnalyzer._intents_for(question):
        raise LiveError("live_knowledge_or_weather_query_not_enabled")
    normalized = {"source": SOURCE, "conversation_id": "twitch:" + CHANNEL_ID, "author_id": config.tester_user_id,
                  "message_id": message_id, "text": text, "created_at": stamp.isoformat(), "is_self": False}
    provenance = {"source": SOURCE, "transport_authenticated": True, "client_id": CLIENT_ID,
                  "reader_user_id": BOT_ID, "broadcaster_user_id": CHANNEL_ID, "chatter_user_id": config.tester_user_id,
                  "subscription_id": binding["subscription_id"], "session_id": binding["session_id"],
                  "delivery_id": delivery_id, "message_id": message_id,
                  "source_ref": f"twitch-eventsub://{CHANNEL_ID}/{message_id}", "received_at": datetime.now(timezone.utc).isoformat()}
    return normalized, provenance


class LiveSession:
    def __init__(self, config: LiveConfig, identity: dict, runtime=None, *, deadline=float("inf"), model_budget_seconds=95):
        if config.local_ai_input is not True or config.max_processed != 1 or config.tester_user_id != CHANNEL_ID:
            raise LiveError("live_session_config_outside_approved_scope")
        self.config, self.identity = config, validate_identity(identity)
        self.ledger = LiveLedger(config)
        self.runtime = runtime if runtime is not None else build_live_runtime(config)
        if type(self.runtime.output) is not DryRunOutput:
            raise LiveError("dry_run_output_required")
        self.deadline, self.model_budget_seconds = deadline, model_budget_seconds
        self._lock = threading.Lock()

    @property
    def exhausted(self):
        return self.ledger.attempts() >= 1

    def _log(self, value):
        with (self.config.state_dir / "live_provenance.jsonl").open("ab") as handle:
            handle.write(_encoded(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def process(self, envelope, binding):
        base = {"source": SOURCE, "public_reply": False, "public_output": "not_attempted", "model_invoked": False}
        with self._lock:
            try:
                event, provenance = normalize_notification(envelope, binding, self.config)
                if time.monotonic() + self.model_budget_seconds >= self.deadline:
                    raise LiveError("insufficient_time_for_bounded_model_call")
            except LiveError as error:
                return {**base, "status": "held", "reason_code": error.code}
            stable = {k: event[k] for k in ("source", "conversation_id", "author_id", "message_id", "text")}
            reservation = self.ledger.reserve(event["message_id"], provenance["delivery_id"], _digest(stable))
            if reservation != "reserved":
                return {**base, "status": "held", "reason_code": reservation}
            try:
                self._log({"phase": "reserved", "provenance": provenance, "normalized_event": event,
                           "local_ai_input": True, "model": MODEL, "model_digest": MODEL_DIGEST, "output": "dry_run"})
                result = self.runtime.handle(event)
                send = result.get("send_result", {})
                if send.get("attempted") is True or send.get("public_reply") is True or result.get("public_reply") is True:
                    raise LiveError("unexpected_output_contract")
                invoked = result.get("status") == "answered" or result.get("reason") == "model_error"
                self._log({"phase": "finished", "provenance": provenance, "result": result, "model_invoked": invoked})
                self.ledger.finish(event["message_id"], "complete")
                return {**base, "status": result.get("status", "held"), "model_invoked": invoked,
                        "reason_code": result.get("reason"), "message_id": event["message_id"],
                        "source_ref": provenance["source_ref"], "transport_authenticated": True,
                        "answer": result.get("answer"), "send_result": send, "run_id": result.get("run_id")}
            except Exception:
                try:
                    self.ledger.finish(event["message_id"], "uncertain")
                except (LiveError, OSError):
                    pass
                return {**base, "status": "held", "reason_code": "accepted_event_outcome_uncertain", "model_invoked": None}


def _make_auth(config):
    from .twitch_auth import TwitchAuth
    return TwitchAuth(CLIENT_ID, BOT_ID, config.token_path, client_type=config.client_type)


def _make_transport(auth, *, on_status=None):
    from .twitch_transport import TwitchTransport
    return TwitchTransport(auth, CHANNEL_ID, on_status=on_status)


def _check_model(model):
    tags = model._request_json("/api/tags")
    matches = [v for v in tags.get("models", []) if v.get("name") == MODEL or v.get("model") == MODEL]
    if not matches or any(v.get("digest") != MODEL_DIGEST for v in matches):
        raise LiveError("installed_model_digest_mismatch_no_download_attempted")


def offline_check(config):
    """Report local prerequisites without opening tokens, sockets or runtime state."""
    try:
        websocket_version = metadata.version("websockets")
        dependency_ready = websocket_version == "17.3" and util.find_spec("websockets") is not None
    except (metadata.PackageNotFoundError, ImportError, ValueError):
        websocket_version, dependency_ready = None, False
    token_exists = config.token_path.is_file()
    return {"status": ("dependency_not_ready" if not dependency_ready else
                       "ready_for_token_validation" if token_exists else "login_required"),
            "dependency": {"name": "websockets", "required_version": "17.3",
                           "installed_version": websocket_version, "ready": dependency_ready},
            "token_file_exists": token_exists, "token_contents_read": False,
            "ready_for_login": dependency_ready and not token_exists,
            "runtime_state_exists": config.state_dir.exists(),
            "network_used": False, "model_invoked": False, "source": SOURCE,
            "twitch_receive_verified": False, "twitch_send_verified": False,
            "public_send_enabled": False, "public_output": "not_attempted", "output": "dry_run"}


async def listen(config, *, auth=None, transport_factory=None, model=None, emit=print):
    auth = auth if auth is not None else _make_auth(config)
    identity = validate_identity(auth.load_and_validate())
    duration = config.duration_seconds
    deadline = time.monotonic() + duration
    timeout = min(90, max(1, duration - 5))
    model = model if model is not None else OllamaModelClient(endpoint="http://127.0.0.1:11435", model=MODEL, timeout=timeout)
    _check_model(model)
    session = LiveSession(config, identity, build_live_runtime(config, model), deadline=deadline, model_budget_seconds=timeout + 4)
    if session.exhausted:
        return {"status": "held", "reason_code": "model_attempt_limit_reached", "source": SOURCE, "public_output": "not_attempted"}
    def transport_status(event):
        emit(json.dumps({"transport_status": event, "public_output": "not_attempted"}, ensure_ascii=False))
    transport = transport_factory(auth) if transport_factory is not None else _make_transport(auth, on_status=transport_status)
    results = []
    async def receive(envelope, binding):
        result = await asyncio.to_thread(session.process, envelope, binding)
        # Rejected notification text never reaches this output or the runtime logs.
        emit(json.dumps({"event_result": result}, ensure_ascii=False))
        if result.get("model_invoked") is not False:
            results.append(result)
        return not session.exhausted and time.monotonic() + timeout + 4 < deadline
    summary = await transport.run(receive, duration_seconds=duration, max_events=1000, queue_size=16,
                                  callback_timeout_seconds=timeout + 5)
    dry_run_verified = any(result.get("status") == "answered" and result.get("send_result", {}).get("status") == "dry_run_not_sent"
                           and result.get("send_result", {}).get("attempted") is False for result in results)
    processing_failure = next((result for result in results if result.get("status") != "answered"), None)
    failed = summary.get("status") == "failed" or processing_failure is not None
    reason = summary.get("reason") if summary.get("status") == "failed" else (processing_failure or {}).get("reason_code")
    return {"status": "failed" if failed else "dry_run_completed" if dry_run_verified else "window_closed_without_dry_run",
            "reason_code": reason, "source": SOURCE, "authenticated_identity": identity,
            "transport": summary, "processed_results": results, "model_attempts": session.ledger.attempts(),
            "twitch_receive_verified": any(result.get("transport_authenticated") is True for result in results),
            "dry_run_verified": dry_run_verified, "twitch_send_verified": False,
            "public_output": "not_attempted", "output": "dry_run", "max_duration_seconds": duration}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "login", "status", "listen"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = LiveConfig.read(args.config)
        if args.command == "check":
            result = offline_check(config)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result["dependency"]["ready"] else 2
        auth = _make_auth(config)
        if args.command == "login":
            grant = auth.begin_device()
            print(json.dumps({"login_required": grant.safe_prompt()}, ensure_ascii=False), flush=True)
            result = {"status": "authenticated", "identity": validate_identity(auth.poll_device(grant))}
        elif args.command == "status":
            result = {"status": "authenticated", "identity": validate_identity(auth.load_and_validate()),
                      "twitch_receive_verified": False, "output": "dry_run", "public_send_enabled": False}
        else:
            result = asyncio.run(listen(config, auth=auth))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.command == "listen":
            if result.get("status") == "failed": return 2
            if not result.get("dry_run_verified"): return 3
        return 0
    except Exception as error:
        code = getattr(error, "code", "twitch_live_failed")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}", code):
            code = "twitch_live_failed"
        print(json.dumps({"status": "failed", "reason_code": code, "public_output": "not_attempted"}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
