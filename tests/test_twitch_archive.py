from contextlib import redirect_stderr
from copy import deepcopy
from datetime import datetime,timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from ssr_chat.api import dispatch
from ssr_chat.chat_ingest import digest
from ssr_chat.cli import main
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.twitch_archive import TwitchArchiveGateway

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/twitch/archive.synthetic.json"
NOW=datetime(2026,11,3,23,20,0,tzinfo=timezone.utc)


class TwitchArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.config={"schema_version":"1.0.1","mode":"synthetic_twitch_archive_only",**{k:deepcopy(self.fixture[k]) for k in ("source_binding","storage_binding","fixture_receipts")},
                     "storage_pipeline":"unit-pipeline.json","storage_state_dir":"unit-state"}
        self.pipeline=Mock();self.pipeline.binding=deepcopy(self.config["storage_binding"])
        scope=self.pipeline.binding["scope"];selector={"domain":"chat_history",**{k:{"mode":"exact","value":v} for k,v in scope.items() if k!="domain"}}
        self.pipeline.context={"sources":{self.pipeline.binding["source_id"]:{"access_scope":{"read_scopes":[selector],"write_scopes":[selector]}}}}
        self.captured=[];self.pipeline.ingest.side_effect=self.store
        self.gateway=self.build();self.control("connect")
        self.runtime=Mock();self.runtime.execution_profile={"knowledge":"fixture","model":"fixture","weather":"fixture","output":"dry_run"}

    def build(self,config=None,watched=None):
        return TwitchArchiveGateway(config or self.config,self.pipeline,clock=lambda:NOW,watched=watched)

    def control(self,operation,session="synthetic-session-1",gateway=None):
        return (gateway or self.gateway).archive({"operation":operation,"session_id":session,"synthetic":True})

    def notification(self,session="synthetic-session-1"):
        value=deepcopy(self.fixture["notifications"][0]);value["session_id"]=session
        value["envelope"]["metadata"]["message_timestamp"]=NOW.isoformat().replace("+00:00","Z")
        value["envelope"]["payload"]["subscription"]["transport"]["session_id"]=session
        return value

    def store(self,payload):
        self.captured.append(deepcopy(payload))
        return {"request_id":payload["request_id"],"mode":"synthetic_chat_ingest_only","public_output":"not_attempted","model_invoked":False,
                "items":[{"index":1,"status":"stored","stage":"store","storage_outcome":"confirmed","record_sha256":digest(payload["events"][0]["record"]),
                          "normalizer_status":"ok","normalizer_exit":0,"store_status":"ok","store_exit":0,"reason_code":"stored"}]}

    def test_projection_uses_explicit_identity_scope_and_host_clock_receipt(self):
        notification=self.notification();result=self.gateway.archive(notification)
        self.assertEqual(result["status"],"stored")
        envelope=self.captured[0]["events"][0];record=envelope["record"]
        event=notification["envelope"]["payload"]["event"];receipt=self.config["fixture_receipts"][event["message_id"]]
        self.assertEqual(record["source_event_id"],event["message_id"])
        self.assertEqual(record["actor_account_id"],self.config["storage_binding"]["actor_account_id"])
        self.assertEqual(record["scope"],self.config["storage_binding"]["scope"])
        self.assertEqual(record["event_time"],receipt["event_time"]);self.assertEqual(record["received_at"],receipt["received_at"])
        self.assertNotEqual(record["event_time"],notification["envelope"]["metadata"]["message_timestamp"])
        self.assertEqual(record["text"],event["message"]["text"])
        self.assertTrue(envelope["raw_reference"].startswith("synthetic://twitch-replay/1001/"))
        self.assertEqual(record["source_refs"][0]["locator"],envelope["raw_reference"])
        self.assertNotIn("extensions",record)

    def test_disconnect_reconnect_changes_transport_not_storage_stream_or_record(self):
        first=self.gateway.archive(self.notification());old=self.captured[0]
        self.control("disconnect")
        self.assertEqual(self.gateway.archive(self.notification())["reason_code"],"session_not_connected")
        self.control("connect","synthetic-session-2")
        repeated=self.notification("synthetic-session-2");repeated["envelope"]["metadata"]["message_id"]="another-delivery"
        self.gateway.archive(repeated)
        self.assertEqual(old,self.captured[1]);self.assertNotEqual(old["events"][0]["record"]["scope"]["session_id"],"synthetic-session-2")
        self.assertFalse(first["query_invoked"]);self.assertFalse(first["model_invoked"])

    def test_unknown_event_never_invents_clock_or_reuses_other_receipt(self):
        value=self.notification();value["envelope"]["payload"]["event"]["message_id"]="00000000-0000-4000-8000-000000000099"
        result=self.gateway.archive(value)
        self.assertEqual(result["reason_code"],"synthetic_clock_receipt_required");self.pipeline.ingest.assert_not_called()

    def test_changed_text_same_event_id_is_rejected_before_storage(self):
        value=self.notification();message=value["envelope"]["payload"]["event"]["message"]
        message["text"]="altered content";message["fragments"][0]["text"]=message["text"]
        self.assertEqual(self.gateway.archive(value)["reason_code"],"synthetic_receipt_content_mismatch")
        self.pipeline.ingest.assert_not_called()

    def test_display_names_and_badges_are_not_projected_into_record_or_response(self):
        value=self.notification();event=value["envelope"]["payload"]["event"]
        event.update(chatter_user_name="PRIVATE_DISPLAY_MARKER",badges=[{"set_id":"moderator","id":"1","info":""}])
        result=self.gateway.archive(value)
        self.assertEqual(result["status"],"stored")
        self.assertNotIn("PRIVATE_DISPLAY_MARKER",json.dumps(self.captured)+json.dumps(result))
        for secret in (event["message"]["text"],self.config["storage_binding"]["actor_account_id"]):
            self.assertNotIn(secret,json.dumps(result,ensure_ascii=False))

    def test_foreign_identity_channel_shared_origin_self_and_real_flag_fail_closed(self):
        cases=[]
        for key,value in (("chatter_user_id","2002"),("broadcaster_user_id","1002"),("chatter_user_id","9001"),("source_broadcaster_user_id","1002")):
            payload=self.notification();payload["envelope"]["payload"]["event"][key]=value;cases.append(payload)
        value=self.notification();value["synthetic"]=False;cases.append(value)
        for value in cases:self.assertEqual(self.gateway.archive(value)["status"],"held")
        self.pipeline.ingest.assert_not_called()

    def test_source_namespace_and_storage_mapping_mismatch_reject_at_construction(self):
        for field,value in (("source_id","synthetic-unrelated"),("actor_account_id","another-actor")):
            cfg=deepcopy(self.config);cfg["storage_binding"][field]=value
            with self.assertRaises(PipelineError):self.build(cfg)
        cfg=deepcopy(self.config);cfg["source_binding"]["chatter_user_id"]="2002"
        with self.assertRaises(PipelineError):self.build(cfg)

    def test_naive_reversed_or_unbound_clock_receipt_is_invalid(self):
        event_id=next(iter(self.config["fixture_receipts"]))
        for key,value in (("event_time","2026-11-03T22:00:00"),("received_at","2026-11-03T00:00:00Z"),("semantics_sha256","not-a-hash")):
            cfg=deepcopy(self.config);cfg["fixture_receipts"][event_id][key]=value
            with self.assertRaises(PipelineError):self.build(cfg)

    def test_local_cache_and_native_denials_are_not_converted_to_success(self):
        def deny(payload):
            result=self.store(payload);result["items"][0].update(status="held",stage="input",storage_outcome="not_attempted",reason_code="local_cache_not_allowed")
            return result
        self.pipeline.ingest.side_effect=deny
        result=self.gateway.archive(self.notification())
        self.assertEqual(result["status"],"held");self.assertEqual(result["reason_code"],"local_cache_not_allowed")
        self.assertFalse(result["send_result"]["attempted"])

    def test_uncertain_commit_is_not_retried_or_claimed_stored(self):
        self.pipeline.ingest.side_effect=RuntimeError("PRIVATE_BACKEND_ERROR")
        result=self.gateway.archive(self.notification())
        self.assertEqual(result["status"],"uncertain");self.assertEqual(result["reason_code"],"archive_storage_outcome_uncertain")
        self.assertNotIn("PRIVATE_BACKEND_ERROR",json.dumps(result));self.pipeline.ingest.assert_called_once()

    def test_backend_raw_additions_are_withheld_and_bad_summary_is_rejected(self):
        def extras(payload):
            result=self.store(payload);result["private_text"]="PRIVATE_BACKEND_CONTENT";result["items"][0]["raw_record"]={"text":"PRIVATE_BACKEND_CONTENT"}
            return result
        self.pipeline.ingest.side_effect=extras
        result=self.gateway.archive(self.notification());self.assertNotIn("PRIVATE_BACKEND_CONTENT",json.dumps(result))
        def bad(payload):
            result=self.store(payload);result["items"][0]["record_sha256"]="PRIVATE_BACKEND_CONTENT";return result
        self.pipeline.ingest.side_effect=bad
        result=self.gateway.archive(self.notification())
        self.assertEqual(result["status"],"uncertain");self.assertNotIn("PRIVATE_BACKEND_CONTENT",json.dumps(result))

    def test_http_has_no_legacy_query_or_model_fallback_and_rejects_authority(self):
        entry=Mock()
        for field in ("scope","actor_account_id","event_time","storage_pipeline","authenticated"):
            value=self.notification();value[field]="untrusted"
            code,_=dispatch(self.runtime,"POST","/twitch/archive",json.dumps(value).encode(),entry_gateway=entry,twitch_archive_gateway=self.gateway)
            self.assertEqual(code,400)
        self.runtime.handle.assert_not_called();entry.query.assert_not_called();self.pipeline.ingest.assert_not_called()

    def test_disabled_route_and_strict_json_cannot_trigger_storage(self):
        code,body=dispatch(self.runtime,"POST","/twitch/archive",b"{}")
        self.assertEqual((code,body["reason_code"]),(503,"twitch_archive_not_configured"))
        for raw in (b'{"operation":"connect","operation":"disconnect"}',b'[]',b'{"x":NaN}',b'{"operation":{}}'):
            code,_=dispatch(self.runtime,"POST","/twitch/archive",raw,twitch_archive_gateway=self.gateway)
            self.assertEqual(code,400)
        self.pipeline.ingest.assert_not_called()

    def test_reconnect_url_is_never_followed_and_no_model_is_required(self):
        value=self.notification();value["envelope"]={"metadata":{"message_type":"session_reconnect"},"payload":{"session":{"reconnect_url":"wss://unrequested.invalid"}}}
        with patch("socket.socket.connect",side_effect=AssertionError("No network")):
            result=self.gateway.archive(value)
        self.assertEqual(result["status"],"held");self.pipeline.ingest.assert_not_called()

    def test_profile_has_no_private_mapping_paths_or_clock_receipt_data(self):
        profile=json.dumps(self.gateway.profile)
        for private in ("2001","1001","synthetic-account","synthetic-stream","event_time\"","semantics_sha256","unit-state"):
            self.assertNotIn(private,profile)
        self.assertFalse(self.gateway.profile["transport_authenticated"]);self.assertFalse(self.gateway.profile["model_enabled"])

    def test_changed_host_config_stops_before_storage(self):
        parent=Path(__file__).resolve().parents[1]/".test-temp";parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            path=Path(directory)/"archive.json";path.write_text("changed",encoding="utf-8")
            gateway=self.build(watched={str(path):"0"*64})
            result=self.control("connect",gateway=gateway)
            self.assertEqual(result["reason_code"],"twitch_archive_configuration_changed")
        self.pipeline.ingest.assert_not_called()

    def test_invalid_cli_archive_configuration_never_starts_listener(self):
        parent=Path(__file__).resolve().parents[1]/".test-temp";parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            path=Path(directory)/"bad.json";path.write_text('{"mode":"live"}',encoding="utf-8")
            stderr=StringIO()
            with redirect_stderr(stderr),patch("ssr_chat.cli.serve") as serve:
                code=main(["serve","--state-dir",str(Path(directory)/"legacy-state"),"--twitch-archive-config",str(path)])
            self.assertEqual(code,2);serve.assert_not_called();self.assertIn("twitch_archive_configuration_invalid",stderr.getvalue())


if __name__=="__main__":unittest.main()
