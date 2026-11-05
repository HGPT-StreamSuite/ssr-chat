from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr
from copy import deepcopy
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from ssr_chat.api import dispatch
from ssr_chat.cli import main
from ssr_chat.modular_entry import EntryGateway
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.twitch_replay import TwitchReplayBridge

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/twitch/notification.synthetic.json"
NOW = datetime(2026, 11, 3, 22, 0, 0, tzinfo=timezone.utc)


class TwitchReplayTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / ".test-temp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = {"scope": {"domain": "channel_knowledge", "platform": "synthetic-chat", "channel_id": "channel-a",
            "game_id": "synthetic-game", "session_id": "synthetic-session", "content_version": "v1"},
            "actor_account_id": None, "time_from": None, "time_to": None, "limit": 20, "current_event": {}}
        self.answer = Mock()
        self.answer.profile = {"domain": "channel_knowledge", "layer": "raw", "public_output_enabled": False, "data_mode": "synthetic_only", "citations_required": True}
        self.answer.retrieval.trusted_answer_context = deepcopy(self.context)
        self.answer.answer.side_effect = self.respond
        self.entry = EntryGateway({"schema_version": "1.0.1", "routing": "explicit_command_v1", "targets": {"/channel": "/answer/channel"}}, {}, {"/answer/channel": self.answer}, [])
        self.binding = {"schema_version": "1.0.1", "mode": "synthetic_replay_only", "synthetic": True, "subscription_id": "synthetic-chat-subscription",
            "broadcaster_user_id": "1001", "reader_user_id": "9001", "chatter_user_id": "2001", "actor_account_id": None, "entry_contexts": {"/channel": self.context}}
        self.trace = []
        self.bridge = self.rebuild()
        self.control("connect")
        self.runtime = Mock(); self.runtime.execution_profile = {"knowledge": "fixture", "model": "fixture", "weather": "fixture", "output": "dry_run"}

    @staticmethod
    def respond(payload):
        return {"status": "answered", "request_id": payload["request_id"], "answer": "Launch at 10:00 [E1]", "model_invoked": True, "public_output": "not_attempted"}

    def rebuild(self, trace=None):
        return TwitchReplayBridge(self.binding, self.entry, self.root / "replay.sqlite3", self.trace if trace is None else trace, clock=lambda: NOW)

    def control(self, operation, session="synthetic-session-1", bridge=None):
        return (bridge or self.bridge).replay({"operation": operation, "session_id": session, "synthetic": True})

    def notification(self, session="synthetic-session-1", delivery="synthetic-delivery-1", text="/channel launch"):
        value = json.loads(FIXTURE.read_text(encoding="utf-8"))
        value["session_id"] = session
        value["envelope"]["metadata"]["message_id"] = delivery
        value["envelope"]["payload"]["subscription"]["transport"]["session_id"] = session
        message = value["envelope"]["payload"]["event"]["message"]
        message["text"] = text; message["fragments"][0]["text"] = text
        return value

    def test_exact_ids_raw_text_main_entry_and_existing_dry_run_contract(self):
        value = self.notification(text="/channel launch  time?")
        with patch("socket.socket.connect", side_effect=AssertionError("No transport allowed in this unit test")):
            result = self.bridge.replay(value)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(self.answer.answer.call_args.args[0]["text"], "launch  time?")
        self.assertEqual(result["normalized_event"]["text"], "/channel launch  time?")
        self.assertEqual(result["normalized_event"]["author_id"], "2001")
        self.assertEqual(result["normalized_event"]["source"], "synthetic-twitch-eventsub")
        self.assertFalse(result["transport_authenticated"])
        self.assertEqual(result["send_result"]["status"], "dry_run_not_sent")
        self.assertFalse(result["send_result"]["attempted"]); self.assertFalse(result["send_result"]["public_reply"])
        self.assertEqual(result["send_result"]["delivery_state"], "confirmed_not_sent")
        self.assertEqual([r["phase"] for r in self.trace], ["reserved", "finished"])
        self.assertEqual(self.trace[0]["request_id"], result["entry_result"]["request_id"])

    def test_duplicate_delivery_and_same_chat_message_new_delivery_never_reinvoke(self):
        first = self.bridge.replay(self.notification())
        for value in (self.notification(), self.notification(delivery="synthetic-delivery-2")):
            duplicate = self.bridge.replay(value)
            self.assertEqual(duplicate["reason_code"], "duplicate_event")
            self.assertEqual(duplicate["request_id"], first["request_id"])
            self.assertFalse(duplicate["model_invoked"]); self.assertIsNone(duplicate["answer"])
        self.answer.answer.assert_called_once()

    def test_disconnect_reconnect_and_process_restart_keep_persistent_dedup(self):
        self.bridge.replay(self.notification())
        self.assertEqual(self.control("disconnect")["status"], "disconnected")
        self.assertEqual(self.bridge.replay(self.notification())["reason_code"], "session_not_connected")
        self.control("connect", "synthetic-session-2")
        self.assertEqual(self.bridge.replay(self.notification(session="synthetic-session-2", delivery="reconnect-delivery"))["reason_code"], "duplicate_event")
        restarted = self.rebuild()
        value = self.notification(session="synthetic-session-3", delivery="restart-delivery")
        self.assertEqual(restarted.replay(value)["reason_code"], "session_not_connected")
        self.control("connect", "synthetic-session-3", restarted)
        self.assertEqual(restarted.replay(value)["reason_code"], "duplicate_event")
        self.answer.answer.assert_called_once()

    def test_message_and_delivery_identity_collisions_are_conflicts(self):
        self.bridge.replay(self.notification())
        changed = self.notification(text="/channel different")
        self.assertEqual(self.bridge.replay(changed)["reason_code"], "event_id_conflict")
        changed = self.notification()
        changed["envelope"]["payload"]["event"]["message_id"] = "00000000-0000-4000-8000-000000000002"
        self.assertEqual(self.bridge.replay(changed)["reason_code"], "event_id_conflict")
        self.answer.answer.assert_called_once()

    def test_concurrent_duplicates_invoke_once(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self.bridge.replay, [self.notification(), self.notification()]))
        self.assertEqual(sorted(r["status"] for r in results), ["answered", "held"])
        self.answer.answer.assert_called_once()

    def test_crash_after_reservation_holds_redelivery_after_restart(self):
        self.answer.answer.side_effect = SystemExit("synthetic interruption")
        with self.assertRaises(SystemExit): self.bridge.replay(self.notification())
        restored = self.rebuild(); self.control("connect", bridge=restored)
        self.assertEqual(restored.replay(self.notification())["reason_code"], "event_outcome_uncertain")
        self.answer.answer.assert_called_once()

    def test_unknown_target_outcome_has_no_automatic_retry_or_false_no_model_claim(self):
        self.answer.answer.side_effect = RuntimeError("PRIVATE_ERROR")
        first = self.bridge.replay(self.notification())
        self.assertEqual(first["reason_code"], "entry_target_failed"); self.assertIsNone(first["model_invoked"])
        self.assertNotIn("PRIVATE_ERROR", json.dumps(first))
        self.assertEqual(self.bridge.replay(self.notification())["reason_code"], "event_outcome_uncertain")
        self.answer.answer.assert_called_once()

    def test_wrong_source_identity_transport_self_shared_and_timestamp_reject_before_entry(self):
        cases = []
        for field, replacement, reason in (("broadcaster_user_id", "1002", "eventsub_channel_mismatch"),
            ("chatter_user_id", "2002", "eventsub_chatter_not_bound"), ("chatter_user_id", "9001", "self_event_loop_guard"),
            ("source_broadcaster_user_id", "1002", "shared_chat_origin_not_supported"), ("message_id", "missing-stable-id", "eventsub_chat_message_id_invalid")):
            value = self.notification(); value["envelope"]["payload"]["event"][field] = replacement; cases.append((value, reason))
        value = self.notification(); value["synthetic"] = False; cases.append((value, "synthetic_source_required"))
        for key, replacement in (("id", "wrong-subscription"), ("condition", {"broadcaster_user_id": "1001", "user_id": "other"}),
                                 ("transport", {"method": "webhook", "session_id": "synthetic-session-1"})):
            value = self.notification(); value["envelope"]["payload"]["subscription"][key] = replacement; cases.append((value, "eventsub_subscription_or_transport_mismatch"))
        for stamp in ("2026-11-03T21:00:00Z", "2026-11-03T22:02:00Z"):
            value = self.notification(); value["envelope"]["metadata"]["message_timestamp"] = stamp; cases.append((value, "eventsub_timestamp_outside_window"))
        for value, reason in cases:
            with self.subTest(reason=reason): self.assertEqual(self.bridge.replay(value)["reason_code"], reason)
        self.answer.answer.assert_not_called()

    def test_display_name_badge_and_claimed_authority_cannot_change_bound_ids(self):
        value = self.notification(); event = value["envelope"]["payload"]["event"]
        event.update(chatter_user_id="2002", chatter_user_name="SyntheticViewer", badges=[{"set_id": "moderator", "id": "1", "info": ""}])
        self.assertEqual(self.bridge.replay(value)["reason_code"], "eventsub_chatter_not_bound")
        value = self.notification(); value["envelope"]["payload"]["event"]["actor_account_id"] = "alice"
        self.assertEqual(self.bridge.replay(value)["reason_code"], "eventsub_chat_fields_invalid")
        self.answer.answer.assert_not_called()

    def test_unsupported_controls_urls_fragments_and_context_never_connect_or_query(self):
        with patch("socket.socket.connect", side_effect=AssertionError("No network")):
            value = self.notification(); value["envelope"] = {"metadata": {"message_type": "session_reconnect"}, "payload": {"session": {"reconnect_url": "wss://private.invalid"}}}
            self.assertEqual(self.bridge.replay(value)["reason_code"], "eventsub_type_not_supported")
            value = self.notification(); value["envelope"]["payload"]["event"]["message"]["fragments"][0]["text"] = "different"
            self.assertEqual(self.bridge.replay(value)["reason_code"], "eventsub_fragment_text_mismatch")
            value = self.notification(); value["envelope"]["payload"]["event"]["reply"] = {"parent_user_id": "other"}
            self.assertEqual(self.bridge.replay(value)["reason_code"], "eventsub_message_context_not_supported")
        self.answer.answer.assert_not_called()

    def test_http_disabled_strict_json_and_body_authority_reject(self):
        status, body = dispatch(self.runtime, "POST", "/twitch/replay", b"{}")
        self.assertEqual((status, body["reason_code"]), (503, "twitch_replay_not_configured"))
        for raw in (b'{"operation":"connect","operation":"disconnect"}', b'[]', b'{"x":NaN}', b'{bad', b'{"operation":{}}'):
            status, _ = dispatch(self.runtime, "POST", "/twitch/replay", raw, twitch_replay_gateway=self.bridge)
            self.assertEqual(status, 400)
        for field in ("scope", "actor_account_id", "authenticated", "access_token", "reconnect_url"):
            value = self.notification(); value[field] = "PRIVATE"
            status, _ = dispatch(self.runtime, "POST", "/twitch/replay", json.dumps(value).encode(), twitch_replay_gateway=self.bridge)
            self.assertEqual(status, 400)
        self.runtime.handle.assert_not_called(); self.answer.answer.assert_not_called()

    def test_binding_mismatch_and_real_mode_fail_before_ledger_or_server_start(self):
        for field, replacement in (("mode", "live"), ("synthetic", False), ("actor_account_id", "alice"), ("entry_contexts", {})):
            value = deepcopy(self.binding); value[field] = replacement
            with self.assertRaises(PipelineError): TwitchReplayBridge(value, self.entry, self.root / "other.sqlite3", [])
        contexts = self.entry.trusted_ingress_contexts
        contexts["/channel"]["scope"]["channel_id"] = "other"
        self.assertEqual(self.entry.trusted_ingress_contexts["/channel"]["scope"]["channel_id"], "channel-a")
        binding_file = self.root / "binding.json"; binding_file.write_text(json.dumps(self.binding), encoding="utf-8")
        error = StringIO()
        with redirect_stderr(error), patch("ssr_chat.cli.serve") as serve:
            code = main(["serve", "--state-dir", str(self.root / "startup"), "--twitch-replay-config", str(binding_file)])
        self.assertEqual(code, 2); self.assertIn("twitch_replay_configuration_invalid", error.getvalue()); serve.assert_not_called()

    def test_continuity_missing_ledger_never_creates_an_empty_replacement(self):
        ledger = self.root / "replay.sqlite3"
        saved = self.root / "saved.sqlite3"
        self.assertEqual(ledger.resolve().parent, self.root.resolve())
        self.assertEqual(saved.resolve().parent, self.root.resolve())
        ledger.rename(saved)
        result = self.bridge.replay(self.notification())
        self.assertEqual(result["reason_code"], "replay_ledger_unavailable")
        self.assertFalse(ledger.exists(), "A missing dedup store must not be recreated while processing an arrival")
        self.assertTrue(saved.exists()); self.answer.answer.assert_not_called()

    def test_continuity_changed_binding_is_rechecked_before_each_entry_call(self):
        with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db:
            db.execute("UPDATE meta SET binding_sha256='different-authority-binding'")
            db.commit()
        result = self.bridge.replay(self.notification())
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["reason_code"], "replay_ledger_unavailable")
        self.answer.answer.assert_not_called()

    def test_continuity_same_binding_file_replacement_is_quarantined_even_if_restored(self):
        ledger, saved, substitute = (self.root / name for name in ("replay.sqlite3", "saved.sqlite3", "substitute.sqlite3"))
        self.assertTrue(all(p.resolve().parent == self.root.resolve() for p in (ledger, saved, substitute)))
        ledger.rename(saved); shutil.copyfile(saved, ledger)
        result = self.bridge.replay(self.notification())
        self.assertEqual(result["reason_code"], "replay_ledger_unavailable")
        ledger.rename(substitute); saved.rename(ledger)
        self.assertEqual(self.bridge.replay(self.notification())["reason_code"], "replay_ledger_unavailable")
        self.answer.answer.assert_not_called()
        reviewed = self.rebuild(); self.control("connect", bridge=reviewed)
        self.assertEqual(reviewed.replay(self.notification())["status"], "answered")
        self.answer.answer.assert_called_once()

    def test_continuity_owner_and_schema_marker_changes_latch_until_restart(self):
        for change, restore in (("PRAGMA application_id=0", "PRAGMA application_id=1213485906"), ("PRAGMA user_version=99", "PRAGMA user_version=1")):
            bridge = self.rebuild(); self.control("connect", bridge=bridge)
            with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db: db.execute(change)
            self.assertEqual(bridge.replay(self.notification())["reason_code"], "replay_ledger_unavailable")
            with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db: db.execute(restore)
            self.assertEqual(bridge.replay(self.notification())["reason_code"], "replay_ledger_unavailable")
        self.answer.answer.assert_not_called()

    def test_continuity_change_after_generation_holds_result_and_pending_redelivery(self):
        def generate_then_change(payload):
            with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db:
                db.execute("UPDATE meta SET binding_sha256='changed-after-generation'"); db.commit()
            return self.respond(payload)
        self.answer.answer.side_effect = generate_then_change
        result = self.bridge.replay(self.notification())
        self.assertEqual(result["reason_code"], "entry_outcome_uncertain")
        self.assertIsNone(result["answer"]); self.assertIsNone(result["model_invoked"])
        self.assertFalse(result["send_result"]["attempted"])
        with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db:
            db.execute("UPDATE meta SET binding_sha256=?", (self.bridge.binding_sha256,)); db.commit()
        reviewed = self.rebuild(); self.control("connect", bridge=reviewed)
        self.assertEqual(reviewed.replay(self.notification())["reason_code"], "event_outcome_uncertain")
        self.answer.answer.assert_called_once()

    def test_continuity_corrupt_stored_phase_fails_closed(self):
        self.bridge.replay(self.notification())
        with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db:
            db.execute("UPDATE messages SET phase='unrecognized-phase'"); db.commit()
        result = self.bridge.replay(self.notification())
        self.assertEqual(result["reason_code"], "replay_ledger_unavailable")
        self.assertFalse(result["model_invoked"]); self.assertIsNone(result["answer"])
        self.answer.answer.assert_called_once()

    def test_continuity_two_bridge_instances_share_one_reservation(self):
        second = self.rebuild(); self.control("connect", bridge=second)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(bridge.replay, self.notification()) for bridge in (self.bridge, second)]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(value["status"] for value in results), ["answered", "held"])
        self.answer.answer.assert_called_once()

    def test_continuity_normal_v1_writes_and_restart_preserve_existing_history(self):
        first = self.notification(); self.bridge.replay(first)
        second = self.notification(delivery="second-arrival")
        second["envelope"]["payload"]["event"]["message_id"] = "00000000-0000-4000-8000-000000000002"
        self.assertEqual(self.bridge.replay(second)["status"], "answered")
        with closing(sqlite3.connect(self.root / "replay.sqlite3")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
        reviewed = self.rebuild(); self.control("connect", bridge=reviewed)
        self.assertEqual(reviewed.replay(first)["reason_code"], "duplicate_event")
        self.assertEqual(reviewed.replay(second)["reason_code"], "duplicate_event")
        self.assertEqual(self.answer.answer.call_count, 2)

    def test_foreign_ledger_is_not_reinitialized_and_audit_failure_does_not_query(self):
        foreign = self.root / "foreign.sqlite3"; foreign.write_bytes(b"not a SQLite database")
        before = foreign.read_bytes()
        with self.assertRaises(sqlite3.Error): TwitchReplayBridge(self.binding, self.entry, foreign, [])
        self.assertEqual(foreign.read_bytes(), before)
        failed_log = Mock(); failed_log.append.side_effect = OSError("PRIVATE_PATH")
        bridge = self.rebuild(failed_log); self.control("connect", bridge=bridge)
        self.assertEqual(bridge.replay(self.notification())["reason_code"], "replay_audit_unavailable")
        self.assertEqual(self.bridge.replay(self.notification())["reason_code"], "event_outcome_uncertain")
        self.answer.answer.assert_not_called()


if __name__ == "__main__": unittest.main()
