"""Opt-in synthetic EventSub replay. No socket, OAuth, subscription or live send."""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from stat import S_ISREG
import threading
from uuid import UUID

from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError, load_object
from .output import DryRunOutput


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def token(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


class ReplayLedger:
    """Reserve before invoking the entry; incomplete reservations never auto-retry."""
    APPLICATION_ID = 1213485906

    def __init__(self, path, binding_sha256):
        self.path = Path(path).absolute()
        self._binding_sha256 = binding_sha256
        self._quarantined = False
        existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "rw" if existed else "rwc"
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True, timeout=5, isolation_level=None)) as db:
            self._file_identity = self._identity()
            db.execute("BEGIN IMMEDIATE")
            application = db.execute("PRAGMA application_id").fetchone()[0]
            if application == 0 and not existed:
                db.execute("CREATE TABLE meta(binding_sha256 TEXT NOT NULL)")
                db.execute("CREATE TABLE messages(event_key TEXT PRIMARY KEY, event_hash TEXT NOT NULL, phase TEXT NOT NULL)")
                db.execute("CREATE TABLE deliveries(delivery_id TEXT PRIMARY KEY, event_key TEXT NOT NULL, event_hash TEXT NOT NULL)")
                db.execute("INSERT INTO meta VALUES (?)", (binding_sha256,))
                db.execute("PRAGMA application_id = 1213485906")
                db.execute("PRAGMA user_version = 1")
            elif application != self.APPLICATION_ID or db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("replay_ledger_not_owned")
            if db.execute("SELECT binding_sha256 FROM meta").fetchall() != [(binding_sha256,)]:
                raise ValueError("replay_ledger_binding_changed")
            self._verify_identity()
            db.commit()

    def _identity(self):
        info = self.path.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:
            raise OSError("replay_ledger_file_identity_unavailable")
        return info.st_dev, info.st_ino

    def _verify_identity(self):
        try:
            if self._quarantined or self._identity() != self._file_identity:
                raise OSError("replay_ledger_file_changed")
        except OSError:
            self._quarantined = True
            raise sqlite3.DatabaseError("replay_ledger_continuity_unavailable") from None

    @contextmanager
    def _transaction(self):
        self._verify_identity()
        # rw never creates a substitute store if the dedup history disappears.
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._verify_identity()
                if (db.execute("PRAGMA application_id").fetchone()[0] != self.APPLICATION_ID
                        or db.execute("PRAGMA user_version").fetchone()[0] != 1
                        or db.execute("SELECT binding_sha256 FROM meta").fetchall() != [(self._binding_sha256,)]):
                    raise sqlite3.DatabaseError("replay_ledger_owner_or_binding_changed")
                yield db
                self._verify_identity()
                db.commit()
            except sqlite3.Error as error:
                try: db.rollback()
                except sqlite3.Error: pass
                code = getattr(error, "sqlite_errorcode", None)
                if code is None or code & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    self._quarantined = True
                raise

    def reserve(self, delivery_id, event_key, event_hash):
        with self._transaction() as db:
            delivery = db.execute("SELECT event_key,event_hash FROM deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            previous = db.execute("SELECT event_hash,phase FROM messages WHERE event_key=?", (event_key,)).fetchone()
            if previous and previous[1] not in {"reserved", "complete", "uncertain"}:
                raise sqlite3.DatabaseError("replay_ledger_phase_invalid")
            if (delivery and delivery != (event_key, event_hash)) or (previous and previous[0] != event_hash):
                return "event_id_conflict"
            if previous:
                db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?)", (delivery_id, event_key, event_hash))
                return "duplicate_event" if previous[1] == "complete" else "event_outcome_uncertain"
            db.execute("INSERT INTO messages VALUES (?,?,?)", (event_key, event_hash, "reserved"))
            db.execute("INSERT INTO deliveries VALUES (?,?,?)", (delivery_id, event_key, event_hash))
            return None

    def finish(self, event_key, phase):
        with self._transaction() as db:
            changed = db.execute("UPDATE messages SET phase=? WHERE event_key=? AND phase='reserved'", (phase, event_key)).rowcount
            if changed != 1: raise sqlite3.DatabaseError("reservation_missing")


class TwitchReplayBridge:
    def __init__(self, binding, entry, ledger_path, trace, *, clock=None):
        fields = {"schema_version", "mode", "synthetic", "subscription_id", "broadcaster_user_id", "reader_user_id",
                  "chatter_user_id", "actor_account_id", "entry_contexts"}
        if (not isinstance(binding, dict) or set(binding) != fields or binding["schema_version"] != "1.0.1"
                or binding["mode"] != "synthetic_replay_only" or binding["synthetic"] is not True
                or not token(binding["subscription_id"]) or entry is None):
            raise PipelineError("twitch_replay_binding_invalid")
        for key in ("broadcaster_user_id", "reader_user_id", "chatter_user_id"):
            if not isinstance(binding[key], str) or re.fullmatch(r"[1-9][0-9]{0,19}", binding[key]) is None:
                raise PipelineError("twitch_replay_identity_invalid")
        if binding["chatter_user_id"] == binding["reader_user_id"]:
            raise PipelineError("twitch_replay_self_binding_invalid")
        contexts = entry.trusted_ingress_contexts
        if (not isinstance(binding["entry_contexts"], dict) or not contexts or binding["entry_contexts"] != contexts
                or entry.profile.get("data_mode") != "synthetic_only" or entry.profile.get("public_output_enabled") is not False):
            raise PipelineError("twitch_replay_entry_binding_mismatch")
        for context in contexts.values():
            scope = context.get("scope", {})
            if (context.get("actor_account_id") != binding["actor_account_id"]
                    or not isinstance(scope.get("platform"), str) or not isinstance(scope.get("channel_id"), str)
                    or not scope["platform"] or not scope["channel_id"]
                    or (binding["actor_account_id"] is None and scope.get("domain") in {"chat_history", "stt_transcript"})):
                raise PipelineError("twitch_replay_actor_or_channel_binding_invalid")
        if binding["actor_account_id"] is not None and (not isinstance(binding["actor_account_id"], str) or not binding["actor_account_id"].strip()):
            raise PipelineError("twitch_replay_actor_or_channel_binding_invalid")
        self.binding, self.entry, self.trace = deepcopy(binding), entry, trace
        self.binding_sha256 = digest(binding)
        self.ledger = ReplayLedger(ledger_path, self.binding_sha256)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._session = None
        self._lock = threading.RLock()
        self._output = DryRunOutput()

    @classmethod
    def read(cls, path, entry, state_dir, trace):
        return cls(load_object(Path(path).read_bytes()), entry, Path(state_dir) / "twitch_replay.sqlite3", trace)

    @property
    def profile(self):
        return {"configured": True, "mode": "synthetic_replay_only", "transport_authenticated": False,
                "live_connection_enabled": False, "public_output_enabled": False, "output": "dry_run",
                "deduplication": "persistent_reservation_before_entry", "reconnect": "local_control_only",
                "ledger_integrity": "owned_file_and_binding_each_transaction", "ledger_change_response": "quarantine_until_operator_restart"}

    def replay(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str) or payload["operation"] not in {"connect", "disconnect", "notification"}:
            raise RetrievalInputError("synthetic_replay_operation_required")
        operation = payload["operation"]
        fields = {"operation", "session_id", "synthetic"} | ({"envelope"} if operation == "notification" else set())
        if set(payload) != fields or not token(payload.get("session_id")):
            raise RetrievalInputError("synthetic_replay_fields_invalid")
        result = {"status": "held", "mode": "synthetic_replay_only", "transport_authenticated": False,
                  "answer": None, "model_invoked": False, "public_output": "not_attempted",
                  "send_result": {"status": "not_requested", "attempted": False, "public_reply": False}}

        def held(code):
            return {**result, "reason_code": code}

        if payload["synthetic"] is not True: return held("synthetic_source_required")
        session = payload["session_id"]
        with self._lock:
            if operation == "connect":
                if self._session not in (None, session): return held("session_already_connected")
                self._session = session
                return {**result, "status": "connected"}
            if self._session != session: return held("session_not_connected")
            if operation == "disconnect":
                self._session = None
                return {**result, "status": "disconnected"}
            normalized, reason = self._validate(payload["envelope"], session)
            if reason: return held(reason)
            event_key, event_hash = normalized.pop("event_key"), normalized.pop("event_hash")
            request_id = "twitch-replay-" + hashlib.sha256(event_key.encode("utf-8")).hexdigest()
            result.update(request_id=request_id, source_binding="configured_synthetic_ids", normalized_event=normalized)
            try:
                reason = self.ledger.reserve(normalized["delivery_id"], event_key, event_hash)
            except sqlite3.Error: return held("replay_ledger_unavailable")
            if reason: return held(reason)
            audit = {"request_id": request_id, "binding_sha256": self.binding_sha256, "event_sha256": event_hash,
                     "normalized_event": normalized, "phase": "reserved"}
            try:
                self.trace.append(deepcopy(audit))
            except (OSError, ValueError): return held("replay_audit_unavailable")
            result["model_invoked"] = None
            try:
                target = self.entry.query({"request_id": request_id, "text": normalized["text"]})
                if (not isinstance(target, dict) or target.get("request_id") != request_id
                        or target.get("public_output") != "not_attempted" or target.get("status") not in {"answered", "held", "no_match", "evidence_ready", "model_error"}
                        or (target.get("model_invoked") is not None and type(target["model_invoked"]) is not bool)
                        or (target.get("status") == "answered" and (not isinstance(target.get("answer"), str) or not target["answer"].strip() or target.get("model_invoked") is not True))
                        or (target.get("status") != "answered" and target.get("answer") is not None)):
                    raise ValueError("entry_result_invalid")
                result.update(status=target["status"], model_invoked=target.get("model_invoked"), answer=target.get("answer"), entry_result=target)
                if target.get("reason_code"): result["reason_code"] = target["reason_code"]
                if target["status"] == "answered":
                    result["send_result"] = {**self._output.send(normalized, target["answer"], target["entry_id"]), "delivery_state": "confirmed_not_sent"}
                self.ledger.finish(event_key, "uncertain" if result["model_invoked"] is None else "complete")
                self.trace.append({**audit, "phase": "finished", "status": result["status"], "model_invoked": result["model_invoked"], "public_output": "not_attempted", "send_result": result["send_result"]})
                return result
            except Exception:
                try: self.ledger.finish(event_key, "uncertain")
                except sqlite3.Error: pass
                result.pop("entry_result", None)
                result.update(status="held", answer=None, model_invoked=None)
                return held("entry_outcome_uncertain")

    def _validate(self, envelope, session):
        return validate_fixture_notification(envelope, session, binding=self.binding, clock=self._clock)


def validate_fixture_notification(envelope, session, *, binding, clock):
    def fail(reason): return None, reason
    if not isinstance(envelope, dict) or set(envelope) != {"metadata", "payload"}: return fail("eventsub_envelope_invalid")
    meta, body = envelope["metadata"], envelope["payload"]
    if not isinstance(meta, dict) or meta.get("message_type") != "notification": return fail("eventsub_type_not_supported")
    if (set(meta) != {"message_id", "message_type", "message_timestamp", "subscription_type", "subscription_version"}
            or not token(meta.get("message_id")) or meta.get("subscription_type") != "channel.chat.message" or meta.get("subscription_version") != "1"):
        return fail("eventsub_metadata_mismatch")
    try:
        stamp = meta["message_timestamp"]
        if not isinstance(stamp, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", stamp) is None: raise ValueError()
        age = (clock() - datetime.fromisoformat(stamp)).total_seconds()
        if not -30 <= age <= 300: return fail("eventsub_timestamp_outside_window")
    except (ValueError, TypeError): return fail("eventsub_timestamp_invalid")
    if not isinstance(body, dict) or set(body) != {"subscription", "event"}: return fail("eventsub_payload_invalid")
    sub, event = body["subscription"], body["event"]
    if (not isinstance(sub, dict) or set(sub) - {"id", "status", "type", "version", "condition", "transport", "created_at", "cost"}
            or sub.get("id") != binding["subscription_id"] or sub.get("status") != "enabled"
            or sub.get("type") != "channel.chat.message" or sub.get("version") != "1"
            or sub.get("condition") != {"broadcaster_user_id": binding["broadcaster_user_id"], "user_id": binding["reader_user_id"]}
            or sub.get("transport") != {"method": "websocket", "session_id": session}):
        return fail("eventsub_subscription_or_transport_mismatch")
    allowed = {"broadcaster_user_id", "broadcaster_user_login", "broadcaster_user_name", "chatter_user_id", "chatter_user_login", "chatter_user_name",
               "message_id", "message", "message_type", "badges", "cheer", "color", "reply", "channel_points_custom_reward_id",
               "source_broadcaster_user_id", "source_broadcaster_user_login", "source_broadcaster_user_name", "source_message_id", "source_badges", "is_source_only"}
    if not isinstance(event, dict) or set(event) - allowed: return fail("eventsub_chat_fields_invalid")
    if event.get("broadcaster_user_id") != binding["broadcaster_user_id"]: return fail("eventsub_channel_mismatch")
    if event.get("chatter_user_id") == binding["reader_user_id"]: return fail("self_event_loop_guard")
    if event.get("chatter_user_id") != binding["chatter_user_id"]: return fail("eventsub_chatter_not_bound")
    if any(event.get(key) is not None for key in ("source_broadcaster_user_id", "source_message_id", "source_badges")) or event.get("is_source_only", False) is not False:
        return fail("shared_chat_origin_not_supported")
    if event.get("message_type") != "text" or any(event.get(key) is not None for key in ("cheer", "reply", "channel_points_custom_reward_id")):
        return fail("eventsub_message_context_not_supported")
    try:
        identifier = event["message_id"]
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier: raise ValueError()
    except (KeyError, ValueError, AttributeError): return fail("eventsub_chat_message_id_invalid")
    message = event.get("message")
    if not isinstance(message, dict) or set(message) != {"text", "fragments"}: return fail("eventsub_message_invalid")
    text, fragments = message["text"], message["fragments"]
    if (not isinstance(text, str) or not text.strip() or len(text) > 16_384 or re.search(r"[\x00-\x1f\x7f]", text)
            or not isinstance(fragments, list) or not 1 <= len(fragments) <= 32): return fail("eventsub_message_invalid")
    for fragment in fragments:
        if (not isinstance(fragment, dict) or set(fragment) - {"type", "text", "cheermote", "emote", "mention", "gif"}
                or fragment.get("type") != "text" or not isinstance(fragment.get("text"), str)
                or any(fragment.get(key) is not None for key in ("cheermote", "emote", "mention", "gif"))): return fail("eventsub_plain_text_only")
    if "".join(fragment["text"] for fragment in fragments) != text: return fail("eventsub_fragment_text_mismatch")
    event_key = encoded([binding["broadcaster_user_id"], identifier]).decode("utf-8")
    return {"message_id": identifier, "conversation_id": "synthetic-twitch:" + binding["broadcaster_user_id"],
            "author_id": binding["chatter_user_id"], "text": text, "source": "synthetic-twitch-eventsub",
            "delivery_id": meta["message_id"], "received_session_id": session, "notification_timestamp": meta["message_timestamp"],
            "source_ref": "synthetic-eventsub://" + binding["broadcaster_user_id"] + "/" + identifier,
            "event_key": event_key, "event_hash": digest(event)}, None
