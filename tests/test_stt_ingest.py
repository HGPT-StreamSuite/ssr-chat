from contextlib import closing,redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from ssr_chat.cli import main
from ssr_chat.chat_ingest import encoded
from ssr_chat.modular_api import RetrievalInputError
from ssr_chat.modular_retrieval import PipelineError
from ssr_chat.stt_ingest import SttIngestPipeline

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/stt_storage/contract.synthetic.json"


class SttIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.record=deepcopy(self.fixture["input"]["events"][0])
        for name in ("normalizer/source/cli.py","store/hgpt_ssr/__main__.py"):
            target=self.root/name;target.parent.mkdir(parents=True);target.write_text("# Unit stand-in, not a real native program\n",encoding="utf-8")
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_stt_ingest_only","python":sys._base_executable,"normalizer_repo":str(self.root/"normalizer"),"store_repo":str(self.root/"store"),
                  **{k:self.write(k+".json",self.fixture[k]) for k in ("normalizer_policy","store_sources","store_caller","purpose_policy")},
                  "state_root":str(self.root/"sandbox"),"binding":deepcopy(self.fixture["binding"])}
        self.path=self.write("pipeline.json",self.cfg);self.state=self.root/"sandbox/normal"
        self.normalizer_override=None;self.store_override=None
        self.pipeline=self.build()

    def write(self,name,value):
        path=self.root/name;path.write_bytes(encoded(value));return str(path)

    def build(self):
        self.write("pipeline.json",self.cfg)
        return SttIngestPipeline(self.path,self.state,runner=self.runner)

    def payload(self,*records):
        return {"request_id":"synthetic-unit-stt","synthetic":True,"events":list(records or [deepcopy(self.record)])}

    def native_result(self,command,status,reason):
        return {"schema_version":"1.0.1","request_id":command[command.index("--request-id")+1],"domain":"stt_transcript","status":status,"reason_code":reason,"evidence":[]}

    def fake_store_metadata(self):
        # Unit metadata stand-in only; actual task-8 integration is separate.
        with closing(sqlite3.connect(self.pipeline.database)) as database:
            database.execute("CREATE TABLE IF NOT EXISTS raw_events(unit_marker TEXT)")
            database.execute("CREATE TABLE IF NOT EXISTS ingest_attempts(unit_marker TEXT)")
            database.execute("PRAGMA application_id=1213420370")
            database.execute("PRAGMA user_version=3")

    def runner(self,command,**kwargs):
        record=json.loads(kwargs["input"]);normalizer="source.cli" in command
        self.calls.append({"stage":"normalizer" if normalizer else "store","record":record,"cwd":kwargs["cwd"]})
        if normalizer:
            value=self.normalizer_override(record) if self.normalizer_override else self.pipeline._normalized(record)
            code=0 if set(value)==set(self.record) else 3
        else:
            value=self.store_override(command) if self.store_override else self.native_result(command,"ok","stored");code=0 if value.get("status")=="ok" else 3
            if value.get("status")=="ok":self.fake_store_metadata()
        return subprocess.CompletedProcess(command,code,stdout=encoded(value),stderr=b"")

    def test_typed_stt_handoff_preserves_text_time_speaker_identity_and_source(self):
        body=self.pipeline.ingest(self.payload());self.assertEqual(body["counts"]["stored"],1)
        forwarded=self.calls[1]["record"]
        for key in set(self.record)-{"quality_flags"}:self.assertEqual(forwarded[key],self.record[key])
        self.assertEqual(forwarded["quality_flags"],["synthetic","partial","speaker_unknown","low_confidence"])
        self.assertNotEqual(self.calls[0]["cwd"],self.calls[1]["cwd"])
        self.assertFalse(body["audio_used"]);self.assertFalse(body["model_invoked"])
        self.assertNotIn(self.record["text"],json.dumps(body,ensure_ascii=False))

    def test_identity_source_channel_and_session_cannot_override_binding(self):
        for field,value in (("actor_account_id","synthetic-other"),("source_id","synthetic-other"),("scope",{**self.record["scope"],"channel_id":"other"}),("scope",{**self.record["scope"],"session_id":"other"})):
            record=deepcopy(self.record);record[field]=value
            self.assertEqual(self.pipeline.ingest(self.payload(record))["items"][0]["stage"],"input")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_unknown_or_mismatching_revision_never_defaults_to_one(self):
        for target,value in (("revision",None),("revision",True),("segment_revision",None),("segment_revision",2)):
            record=deepcopy(self.record)
            if target=="revision":record[target]=value
            else:record["extensions"]["stt"][target]=value
            self.assertEqual(self.pipeline.ingest(self.payload(record))["items"][0]["status"],"held")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_cache_unknown_denied_and_missing_do_not_borrow_scope_permission(self):
        for grant in ("unknown","denied",None):
            purpose=deepcopy(self.fixture["purpose_policy"])
            if grant:purpose["uses"]["local_cache"]["status"]=grant
            else:del purpose["uses"]["local_cache"]
            self.write("purpose_policy.json",purpose);self.pipeline=self.build()
            self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"local_cache_not_allowed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_incoming_visibility_cannot_grant_new_write_or_read(self):
        record=deepcopy(self.record);record["access_scope"]={"read_scopes":[],"write_scopes":[]}
        self.assertEqual(self.pipeline.ingest(self.payload(record))["items"][0]["reason_code"],"incoming_stt_source_acl_mismatch")
        self.assertFalse(self.calls)

    def test_unnecessary_fields_extensions_and_non_synthetic_refs_are_rejected(self):
        records=[]
        for field in ("display_name","ip_address","authorization"):
            value=deepcopy(self.record);value[field]="SECRET_MARKER";records.append(value)
        value=deepcopy(self.record);value["extensions"]["private"]="SECRET_MARKER";records.append(value)
        value=deepcopy(self.record);value["source_refs"][0]["locator"]="https://example.invalid/SECRET_MARKER";records.append(value)
        for value in records:self.assertEqual(self.pipeline.ingest(self.payload(value))["items"][0]["status"],"held")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_native_normalizer_rejection_is_not_forwarded_as_record(self):
        self.normalizer_override=lambda record:{"schema_version":"1.0.1","request_id":"synthetic-unit-stt-1","domain":"stt_transcript","status":"denied","reason_code":"source_write_policy_denied","evidence":[]}
        body=self.pipeline.ingest(self.payload());self.assertEqual(body["items"][0]["normalizer_status"],"denied")
        self.assertEqual(len(self.calls),1);self.assertFalse(self.pipeline.database.exists())

    def test_native_cannot_change_text_actor_clock_or_stt_metadata(self):
        for field,value in (("text","CHANGED_SECRET"),("actor_account_id","synthetic-other"),("event_time","2000-01-01T00:00:00Z"),("extensions",{"stt":{}})):
            def altered(record):
                normalized=self.pipeline._normalized(record);normalized[field]=value;return normalized
            self.normalizer_override=altered
            body=self.pipeline.ingest(self.payload());self.assertEqual(body["items"][0]["reason_code"],"stt_ingest_normalized_record_changed")
        self.assertTrue(all(c["stage"]=="normalizer" for c in self.calls));self.assertFalse(self.pipeline.database.exists())

    def test_mixed_batch_commits_per_item_and_does_not_retry_bad_item(self):
        bad=deepcopy(self.record);bad["scope"]["channel_id"]="other"
        body=self.pipeline.ingest(self.payload(self.record,bad,self.record))
        self.assertEqual(body["status"],"partial");self.assertEqual(body["counts"],{"stored":2,"duplicate":0,"held":1,"uncertain":0})
        self.assertEqual(len(self.calls),4)

    def test_unknown_store_ack_is_uncertain_with_no_automatic_retry_or_error_leak(self):
        def broken(command,**kwargs):
            if "source.cli" in command:return self.runner(command,**kwargs)
            self.calls.append({"stage":"store"});self.fake_store_metadata()
            return subprocess.CompletedProcess(command,0,stdout=b"{SECRET_ACK_FRAGMENT",stderr=b"SECRET_BACKEND_ERROR")
        self.pipeline.runner=broken;body=self.pipeline.ingest(self.payload())
        self.assertEqual(body["items"][0]["status"],"uncertain");self.assertEqual(len(self.calls),2)
        self.assertNotIn("SECRET",json.dumps(body));self.assertNotIn("stored",body["items"][0]["reason_code"])

    def test_store_write_denial_has_no_false_confirmed_outcome(self):
        self.store_override=lambda cmd:self.native_result(cmd,"denied","caller_scope_denied")
        item=self.pipeline.ingest(self.payload())["items"][0]
        self.assertEqual(item["status"],"held");self.assertEqual(item["storage_outcome"],"unknown");self.assertFalse(self.pipeline.database.exists())

    def test_store_evidence_is_rejected_and_never_returned_to_write_only_caller(self):
        def leaked(command):
            value=self.native_result(command,"ok","stored");value["evidence"]=[{"text":"SECRET_READBACK"}];return value
        self.store_override=leaked;body=self.pipeline.ingest(self.payload())
        self.assertEqual(body["items"][0]["status"],"uncertain");self.assertNotIn("SECRET_READBACK",json.dumps(body))

    def test_code_addition_or_host_config_change_stops_before_child(self):
        (self.root/"normalizer/source/added.py").write_text("# modified",encoding="utf-8")
        body=self.pipeline.ingest(self.payload());self.assertEqual(body["items"][0]["reason_code"],"stt_ingest_configuration_changed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_unowned_state_is_not_adopted(self):
        self.state.mkdir(parents=True);(self.state/"foreign.txt").write_text("keep",encoding="utf-8")
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"stt_ingest_state_not_owned")
        self.assertFalse(self.calls);self.assertEqual((self.state/"foreign.txt").read_text(),"keep")

    def test_missing_owned_database_is_not_silently_recreated(self):
        self.pipeline.ingest(self.payload());saved=self.pipeline.database.with_suffix(".saved");self.pipeline.database.rename(saved)
        self.calls.clear();body=self.pipeline.ingest(self.payload())
        self.assertEqual(body["items"][0]["reason_code"],"stt_ingest_store_continuity_failed");self.assertFalse(self.calls)
        self.assertTrue(saved.exists());self.assertFalse(self.pipeline.database.exists())

    def test_same_file_truncated_store_cannot_be_reinitialized_as_empty(self):
        self.pipeline.ingest(self.payload());identity=self.pipeline._file_identity();self.pipeline.database.write_bytes(b"");self.calls.clear()
        self.assertEqual(self.pipeline._file_identity(),identity)
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"stt_ingest_store_continuity_failed")
        self.assertFalse(self.calls);self.assertEqual(self.pipeline.database.stat().st_size,0)

    def test_purpose_receipts_and_temporary_spool_contain_no_unnecessary_plaintext(self):
        body=self.pipeline.ingest(self.payload());receipts=(self.state/"ingest-receipts.jsonl").read_text(encoding="utf-8")
        for secret in (self.record["text"],self.record["actor_account_id"]):self.assertNotIn(secret,receipts+json.dumps(body,ensure_ascii=False))
        self.assertFalse(list((self.state/"work").rglob("*.json")))
        self.assertEqual(json.loads(receipts)["ai_input"],"not_used");self.assertEqual(json.loads(receipts)["audio"],"not_used")

    def test_explicit_unknown_actor_is_preserved_independently_of_speaker_label(self):
        self.cfg["binding"]["actor_account_id"]=None;self.pipeline=self.build();record=deepcopy(self.record);record["actor_account_id"]=None;record["extensions"]["stt"]["speaker_id"]="speaker-synthetic-1"
        self.assertEqual(self.pipeline.ingest(self.payload(record))["counts"]["stored"],1)
        self.assertIsNone(self.calls[1]["record"]["actor_account_id"])

    def test_empty_batch_has_no_io_and_authority_overrides_are_invalid(self):
        payload=self.payload();payload["events"]=[];self.assertEqual(self.pipeline.ingest(payload)["status"],"empty");self.assertFalse(self.state.exists())
        payload["scope"]={}
        with self.assertRaises(RetrievalInputError):self.pipeline.ingest(payload)

    def test_broad_or_real_source_and_read_enabled_caller_are_not_accepted(self):
        for change in ("real","broad","read"):
            self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"))
            if change=="real":self.fixture["normalizer_policy"]["grants"][0]["source_kind"]="real"
            elif change=="broad":self.fixture["normalizer_policy"]["caller_access_scope"]["write_scopes"][0]["session_id"]={"mode":"any"}
            else:self.fixture["store_caller"]["read_scopes"]=deepcopy(self.fixture["normalizer_policy"]["grants"][0]["access_scope"]["read_scopes"])
            for k in ("normalizer_policy","store_sources","store_caller","purpose_policy"):self.write(k+".json",self.fixture[k])
            with self.assertRaises(PipelineError):self.build()

    def test_cli_ingest_does_not_build_chat_runtime_or_access_microphone(self):
        input_path=self.write("input.json",self.payload());output=StringIO()
        with patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("runtime must not be built")),patch("ssr_chat.stt_ingest.SttIngestPipeline.ingest",return_value={"status":"complete","counts":{"held":0,"uncertain":0}}),redirect_stdout(output):
            self.assertEqual(main(["ingest-stt","--config",self.path,"--state-dir",str(self.state),"--input",input_path]),0)
