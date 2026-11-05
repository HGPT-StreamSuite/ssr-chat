"""Offline acceptance boundaries: auth, transport and model are test doubles."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from ssr_chat import twitch_live as live


def identity():
    return {"client_id": live.CLIENT_ID, "user_id": live.BOT_ID, "login": "dubheorigin",
            "scopes": ["user:read:chat"], "expires_in": 1000, "validated": True}


def binding():
    return {"client_id": live.CLIENT_ID, "reader_user_id": live.BOT_ID, "broadcaster_user_id": live.CHANNEL_ID,
            "subscription_id": "subscription-1", "session_id": "session-1", "transport_authenticated": True}


def notification(text="!ask 請用一句話打招呼", message_id="message-1", delivery_id="delivery-1"):
    return {"metadata": {"message_type": "notification", "subscription_type": "channel.chat.message",
             "subscription_version": "1", "message_id": delivery_id, "message_timestamp": datetime.now(timezone.utc).isoformat()},
            "payload": {"subscription": {"id": "subscription-1", "status": "enabled", "type": "channel.chat.message", "version": "1",
                "condition": {"broadcaster_user_id": live.CHANNEL_ID, "user_id": live.BOT_ID},
                "transport": {"method": "websocket", "session_id": "session-1"}},
                "event": {"broadcaster_user_id": live.CHANNEL_ID, "chatter_user_id": live.CHANNEL_ID, "message_id": message_id,
                          "message": {"text": text, "fragments": [{"type": "text", "text": text}]},
                          "source_broadcaster_user_id": None, "source_message_id": None}}}


class FakeModel:
    def __init__(self, fail=False): self.calls, self.fail = [], fail
    def generate(self, prompt):
        self.calls.append(prompt)
        if self.fail: raise RuntimeError("private exception detail")
        return "你好，這是離線測試模型輸出。"
    def _request_json(self, path):
        assert path == "/api/tags"
        return {"models": [{"name": live.MODEL, "digest": live.MODEL_DIGEST}]}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = live.LiveConfig(self.root / "token.json", self.root / "state")
        self.model = FakeModel()

    def session(self, **kwargs):
        return live.LiveSession(self.config, identity(), live.build_live_runtime(self.config, self.model), **kwargs)

    def config_data(self):
        return {"schema_version": "1", "client_id": live.CLIENT_ID, "bot_user_id": live.BOT_ID,
                "broadcaster_user_id": live.CHANNEL_ID, "tester_user_id": live.CHANNEL_ID,
                "token_path": str(self.config.token_path), "state_dir": str(self.config.state_dir), "client_type": "public",
                "duration_seconds": 600, "max_processed": 1, "prefix": "!ask", "local_ai_input": True,
                "output": "dry_run", "model_endpoint": "http://127.0.0.1:11435", "model_name": live.MODEL,
                "model_digest": live.MODEL_DIGEST}

    def test_config_exact_identity_model_scope_and_limits(self):
        path = self.root / "config.json"
        data = self.config_data()
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(live.LiveConfig.read(path), self.config)
        changes = {"bot_user_id": "1", "broadcaster_user_id": "1", "tester_user_id": "1", "client_id": "wrong",
                   "model_endpoint": "https://external.invalid", "model_digest": "wrong", "local_ai_input": False,
                   "output": "twitch", "duration_seconds": 601, "max_processed": 2, "prefix": "!askme"}
        for field, value in changes.items():
            with self.subTest(field=field):
                path.write_text(json.dumps({**data, field: value}), encoding="utf-8")
                with self.assertRaises(live.LiveError): live.LiveConfig.read(path)
        path.write_text(json.dumps({**data, "access_token": "not-a-real-token"}), encoding="utf-8")
        with self.assertRaises(live.LiveError): live.LiveConfig.read(path)

    def test_identity_requires_validated_exact_bot_client_login_and_scope(self):
        for field, value in {"validated": False, "user_id": "1", "client_id": "other", "login": "other", "scopes": []}.items():
            with self.subTest(field=field), self.assertRaises(live.LiveError):
                live.validate_identity({**identity(), field: value})

    def test_real_source_mapping_and_general_model_dry_run_without_fixtures(self):
        session = self.session()
        result = session.process(notification(), binding())
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["source"], "twitch-eventsub")
        self.assertTrue(result["transport_authenticated"])
        self.assertEqual(result["send_result"]["status"], "dry_run_not_sent")
        self.assertFalse(result["send_result"]["attempted"])
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0].query_results, ())
        self.assertEqual(self.model.calls[0].analysis["intents"], ["general_chat"])
        self.assertEqual(session.runtime.execution_profile["knowledge"], "disabled")
        self.assertEqual(session.runtime.execution_profile["weather"], "disabled")
        self.assertEqual(session.runtime.bot_id, live.BOT_ID)
        self.assertEqual(session.runtime.validator.allowed_sources, {"twitch-eventsub"})
        evidence = (self.config.state_dir / "live_provenance.jsonl").read_text(encoding="utf-8")
        self.assertIn("twitch-eventsub://928757513/message-1", evidence)
        self.assertNotIn("synthetic", evidence)

    def test_rejected_chatter_has_no_text_log_and_no_model(self):
        session = self.session()
        value = notification("!ask unapproved-private-text")
        value["payload"]["event"]["chatter_user_id"] = "unapproved"
        result = session.process(value, binding())
        self.assertEqual(result["reason_code"], "chatter_not_approved_tester")
        self.assertEqual(self.model.calls, [])
        self.assertFalse((self.config.state_dir / "live_provenance.jsonl").exists())
        self.assertFalse((self.config.state_dir / "conversation_events.jsonl").exists())
        self.assertNotIn("unapproved-private-text", json.dumps(result))

    def test_binding_channel_self_shared_origin_and_shape_denials(self):
        session = self.session()
        cases = []
        for field, value in {"broadcaster_user_id": "wrong", "chatter_user_id": live.BOT_ID,
                             "source_broadcaster_user_id": live.CHANNEL_ID, "source_message_id": "shared"}.items():
            case = notification(); case["payload"]["event"][field] = value; cases.append((case, binding()))
        for field, value in {"client_id": "wrong", "reader_user_id": "wrong", "transport_authenticated": False,
                             "subscription_id": "wrong", "session_id": "wrong"}.items():
            cases.append((notification(), {**binding(), field: value}))
        case = notification(); case["synthetic"] = True; cases.append((case, binding()))
        case = notification(); case["payload"]["subscription"]["transport"] = []; cases.append((case, binding()))
        case = notification(); case["payload"]["event"]["message"]["fragments"] = []; cases.append((case, binding()))
        for case, transport in cases:
            with self.subTest(case=case):
                self.assertEqual(session.process(case, transport)["status"], "held")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(session.ledger.attempts(), 0)

    def test_timestamp_type_prefix_and_unsupported_facts_denials(self):
        session = self.session()
        for offset in (-301, 31):
            case = notification()
            case["metadata"]["message_timestamp"] = (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()
            self.assertEqual(session.process(case, binding())["reason_code"], "notification_timestamp_outside_window")
        for text in ("hello", "!ask", "!askmore hello", "!ask 今天天氣如何", "!ask 介紹一下露娜", "!ask 遊戲攻略"):
            self.assertEqual(session.process(notification(text), binding())["status"], "held")
        case = notification(); case["metadata"]["subscription_version"] = "2"
        self.assertEqual(session.process(case, binding())["reason_code"], "notification_type_not_supported")
        self.assertEqual(self.model.calls, [])

    def test_duplicate_after_restart_does_not_reprocess(self):
        self.session().process(notification(), binding())
        session = self.session()
        result = session.process(notification(delivery_id="delivery-2"), binding())
        self.assertEqual(result["reason_code"], "duplicate_event")
        self.assertEqual(len(self.model.calls), 1)

    def test_same_event_changed_content_and_delivery_reuse_are_conflicts(self):
        session = self.session()
        session.process(notification(), binding())
        self.assertEqual(session.process(notification("!ask a changed greeting", delivery_id="delivery-2"), binding())["reason_code"], "event_id_conflict")
        self.assertEqual(session.process(notification(message_id="message-2"), binding())["reason_code"], "delivery_id_conflict")
        self.assertEqual(len(self.model.calls), 1)

    def test_reserved_crash_never_replays_after_restart(self):
        session = self.session()
        event, provenance = live.normalize_notification(notification(), binding(), self.config)
        stable = {k: event[k] for k in ("source", "conversation_id", "author_id", "message_id", "text")}
        self.assertEqual(session.ledger.reserve(event["message_id"], provenance["delivery_id"], live._digest(stable)), "reserved")
        restarted = self.session()
        self.assertEqual(restarted.process(notification(), binding())["reason_code"], "duplicate_event_uncertain")
        self.assertEqual(self.model.calls, [])

    def test_deleted_ledger_is_not_recreated_or_processed(self):
        session = self.session()
        session.ledger.path.unlink()
        with self.assertRaises(live.LiveError):
            session.process(notification(), binding())
        self.assertFalse(session.ledger.path.exists())
        self.assertEqual(self.model.calls, [])
        with self.assertRaises(live.LiveError):
            self.session()

    def test_replaced_ledger_is_rejected_in_process_and_after_restart(self):
        session = self.session()
        replacement = self.root / "replacement.sqlite3"
        shutil.copyfile(session.ledger.path, replacement)
        # A byte-identical empty substitute must not reset the receipt history.
        session.process(notification(), binding())
        os.replace(replacement, session.ledger.path)
        with self.assertRaises(live.LiveError):
            session.process(notification(message_id="message-2", delivery_id="delivery-2"), binding())
        self.assertEqual(len(self.model.calls), 1)
        with self.assertRaises(live.LiveError):
            self.session()

    def test_owner_change_and_ledger_metadata_change_stop_before_model(self):
        session = self.session()
        with closing(sqlite3.connect(session.ledger.path)) as db, db:
            db.execute("UPDATE metadata SET owner_digest='changed'")
        with self.assertRaises(live.LiveError):
            session.process(notification(), binding())
        self.assertEqual(self.model.calls, [])
        # Once quarantined, restoring a marker cannot resume this instance.
        with closing(sqlite3.connect(session.ledger.path)) as db, db:
            db.execute("UPDATE metadata SET owner_digest=?", (live._digest(self.config.owner()),))
        with self.assertRaises(live.LiveError):
            session.ledger.attempts()

    def test_changed_owner_file_stops_before_model(self):
        session = self.session()
        owner = self.config.state_dir / "owner.json"
        owner.write_text("{}", encoding="utf-8")
        with self.assertRaises(live.LiveError):
            session.process(notification(), binding())
        self.assertEqual(self.model.calls, [])

    def test_model_failure_is_recorded_without_retry_or_send(self):
        self.model.fail = True
        session = self.session()
        result = session.process(notification(), binding())
        self.assertEqual(result["reason_code"], "model_error")
        self.assertTrue(result["model_invoked"])
        self.assertFalse(result["send_result"]["attempted"])
        self.assertEqual(session.process(notification(), binding())["reason_code"], "duplicate_event")
        self.assertEqual(len(self.model.calls), 1)

    def test_pre_model_trace_failure_leaves_uncertain_reservation(self):
        session = self.session()
        with patch.object(session, "_log", side_effect=OSError("test failure")):
            result = session.process(notification(), binding())
        self.assertEqual(result["reason_code"], "accepted_event_outcome_uncertain")
        self.assertEqual(session.process(notification(), binding())["reason_code"], "duplicate_event_uncertain")
        self.assertEqual(self.model.calls, [])

    def test_only_one_attempt_with_concurrent_messages(self):
        session = self.session()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda n: session.process(notification(message_id=f"message-{n}", delivery_id=f"delivery-{n}"), binding()), (1, 2)))
        self.assertEqual(sum(r["status"] == "answered" for r in results), 1)
        self.assertEqual(len(self.model.calls), 1)
        self.assertTrue(session.exhausted)

    def test_time_budget_holds_before_reserve(self):
        session = self.session(deadline=time.monotonic() + 1)
        result = session.process(notification(), binding())
        self.assertEqual(result["reason_code"], "insufficient_time_for_bounded_model_call")
        self.assertEqual(session.ledger.attempts(), 0)

    def test_foreign_state_and_non_dry_output_refused(self):
        self.config.state_dir.mkdir()
        (self.config.state_dir / "unrelated.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(live.LiveError): self.session()
        self.assertEqual((self.config.state_dir / "unrelated.txt").read_text(), "keep")

    def test_model_digest_check_prevents_generation(self):
        with patch.object(self.model, "_request_json", return_value={"models": [{"name": live.MODEL, "digest": "wrong"}]}):
            with self.assertRaises(live.LiveError): live._check_model(self.model)
        self.assertEqual(self.model.calls, [])

    def test_listen_uses_fake_auth_transport_model_and_stops_after_one(self):
        class FakeAuth:
            def load_and_validate(self): return identity()
        seen = []
        class FakeTransport:
            async def run(self, callback, **kwargs):
                self.kwargs = kwargs
                outsider = notification("!ask ignored text"); outsider["payload"]["event"]["chatter_user_id"] = "other"
                for value in (outsider, notification(), notification(message_id="message-2", delivery_id="delivery-2")):
                    seen.append(value)
                    if await callback(value, binding()) is False: break
                return {"test_double": True, "callbacks": len(seen)}
        emitted = []
        result = asyncio.run(live.listen(self.config, auth=FakeAuth(), transport_factory=lambda _: FakeTransport(), model=self.model, emit=emitted.append))
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["model_attempts"], 1)
        self.assertEqual(len(result["processed_results"]), 1)
        self.assertEqual(result["output"], "dry_run")
        self.assertEqual(result["status"], "dry_run_completed")
        self.assertTrue(result["twitch_receive_verified"])
        self.assertTrue(result["dry_run_verified"])
        self.assertFalse(result["twitch_send_verified"])
        self.assertNotIn("ignored text", "".join(emitted))

    def test_cli_status_is_safe_and_does_not_claim_receive(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config_data()), encoding="utf-8")
        class FakeAuth:
            def load_and_validate(self): return {**identity(), "access_token": "secret-never-output"}
        output = StringIO()
        with patch.object(live, "_make_auth", return_value=FakeAuth()), redirect_stdout(output):
            code = live.main(["status", "--config", str(path)])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(value["twitch_receive_verified"])
        self.assertNotIn("secret-never-output", output.getvalue())

    def test_cli_offline_check_never_creates_auth_or_runtime_state(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config_data()), encoding="utf-8")
        output = StringIO()
        with patch.object(live.metadata, "version", return_value="17.3"), \
                patch.object(live.util, "find_spec", return_value=object()), \
                patch.object(live, "_make_auth", side_effect=AssertionError("auth must not be created")), \
                patch.object(live, "listen", side_effect=AssertionError("listen must not run")), \
                redirect_stdout(output):
            self.assertEqual(live.main(["check", "--config", str(path)]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "login_required")
        self.assertTrue(result["ready_for_login"])
        for field in ("network_used", "model_invoked", "twitch_receive_verified", "twitch_send_verified"):
            self.assertFalse(result[field])
        self.assertFalse(self.config.token_path.exists())
        self.assertFalse(self.config.state_dir.exists())

    def test_offline_check_existing_token_does_not_read_or_validate_it(self):
        self.config.token_path.write_bytes(b"private-token-must-not-be-read")
        with patch.object(live.metadata, "version", return_value="17.3"), \
                patch.object(live.util, "find_spec", return_value=object()), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("no token read")):
            result = live.offline_check(self.config)
        self.assertEqual(result["status"], "ready_for_token_validation")
        self.assertFalse(result["token_contents_read"])
        self.assertFalse(result["twitch_receive_verified"])
        self.assertFalse(self.config.state_dir.exists())

    def test_offline_check_refuses_missing_or_wrong_websocket_dependency(self):
        for version in ("16.0", "17.4"):
            with self.subTest(version=version), patch.object(live.metadata, "version", return_value=version):
                result = live.offline_check(self.config)
                self.assertEqual(result["status"], "dependency_not_ready")
                self.assertFalse(result["ready_for_login"])
        with patch.object(live.metadata, "version", side_effect=live.metadata.PackageNotFoundError("websockets")):
            self.assertEqual(live.offline_check(self.config)["status"], "dependency_not_ready")

    def test_transport_failure_and_empty_window_are_not_acceptance(self):
        class FakeAuth:
            def load_and_validate(self): return identity()
        class FakeTransport:
            async def run(self, callback, **kwargs):
                return {"status": "failed", "reason": "subscription_rejected"}
        result = asyncio.run(live.listen(self.config, auth=FakeAuth(), transport_factory=lambda _: FakeTransport(), model=self.model, emit=lambda _: None))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_code"], "subscription_rejected")
        self.assertFalse(result["dry_run_verified"])
        self.assertFalse(result["twitch_receive_verified"])
        class EmptyTransport:
            async def run(self, callback, **kwargs): return {"status": "stopped", "reason": "duration_limit"}
        result = asyncio.run(live.listen(self.config, auth=FakeAuth(), transport_factory=lambda _: EmptyTransport(), model=self.model, emit=lambda _: None))
        self.assertEqual(result["status"], "window_closed_without_dry_run")
        self.assertFalse(result["dry_run_verified"])
        self.assertEqual(self.model.calls, [])

    def test_cli_listen_transport_failure_and_no_dry_run_exit_nonzero(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config_data()), encoding="utf-8")
        for result, expected in (({"status": "failed", "dry_run_verified": False}, 2),
                                 ({"status": "window_closed_without_dry_run", "dry_run_verified": False}, 3),
                                 ({"status": "dry_run_completed", "dry_run_verified": True}, 0)):
            with self.subTest(result=result), patch.object(live, "_make_auth", return_value=object()), patch.object(live, "listen", new=AsyncMock(return_value=result)), redirect_stdout(StringIO()):
                self.assertEqual(live.main(["listen", "--config", str(path)]), expected)


if __name__ == "__main__":
    unittest.main()
