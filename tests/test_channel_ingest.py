from contextlib import closing,redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3,subprocess,sys,tempfile,unittest
from unittest.mock import patch

from ssr_chat.channel_ingest import ChannelIngestPipeline
from ssr_chat.chat_ingest import RECORD,encoded
from ssr_chat.cli import main
from ssr_chat.modular_api import RetrievalInputError
from ssr_chat.modular_retrieval import PipelineError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/channel_storage/contract.synthetic.json"


class ChannelIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.import_override=None;self.store_override=None
        for name in ("importer/source/hgpt_ssr.py","store/source/ssr.py"):
            path=self.root/name;path.parent.mkdir(parents=True);path.write_text("# Unit stand-in only\n",encoding="utf-8")
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_channel_ingest_only","python":sys._base_executable,"importer_repo":str(self.root/"importer"),"store_repo":str(self.root/"store"),
                  **{k:self.write(k+".json",self.fixture[k]) for k in ("importer_config","store_context","purpose_policy")},"state_root":str(self.root/"sandbox"),"binding":deepcopy(self.fixture["binding"]),"exports":deepcopy(self.fixture["exports"])}
        self.path=self.write("pipeline.json",self.cfg);self.state=self.root/"sandbox/normal";self.pipeline=self.build()

    def write(self,name,value):
        path=self.root/name;path.write_bytes(encoded(value));return str(path)

    def build(self):self.write("pipeline.json",self.cfg);return ChannelIngestPipeline(self.path,self.state,runner=self.runner)
    def payload(self,aliases=None):return {"request_id":"synthetic-unit-channel","synthetic":True,"exports":["first"] if aliases is None else aliases}
    def native(self,request,status="ok",reason=None,evidence=None):return {"schema_version":"1.0.1","request_id":request,"domain":"channel_knowledge","status":status,"reason_code":reason,"evidence":evidence or []}

    def fake_database(self):
        with closing(sqlite3.connect(self.pipeline.database)) as db:
            db.executescript("CREATE TABLE IF NOT EXISTS configuration(singleton INTEGER PRIMARY KEY,contract_version TEXT,storage_version INTEGER,scope_json TEXT); CREATE TABLE IF NOT EXISTS records(unit_marker TEXT); CREATE TABLE IF NOT EXISTS withdrawals(unit_marker TEXT);")
            db.execute("INSERT OR IGNORE INTO configuration VALUES(1,'1.0.1',2,?)",(encoded(self.cfg["binding"]["scope"]).decode(),));db.commit()

    def runner(self,args,**kwargs):
        importing="source.hgpt_ssr" in args;self.calls.append({"stage":"importer" if importing else "store","cwd":kwargs["cwd"]})
        if importing:
            query= json.loads(Path(args[args.index("--query")+1]).read_bytes());job=Path(args[args.index("--query")+1]).parent;bundle=json.loads((job/"export.json").read_bytes());originals={}
            for record in bundle["events"]:originals.setdefault(self.pipeline._key(record),record)
            evidence=[{**{k:r[k] for k in RECORD-{"schema_version","record_kind","access_scope"}},"layer":"raw","source_kind":"synthetic","match_reasons":["unit"],"rank":None} for r in originals.values()]
            value=self.native(query["request_id"],evidence=evidence)
            if self.import_override:self.import_override(value)
            code=0 if value["status"] in {"ok","no_match"} else 1
        else:
            record=json.loads(Path(args[args.index("--event-file")+1]).read_bytes());self.calls[-1]["record"]=record;request=args[args.index("--request-id")+1]
            value=self.store_override(request) if self.store_override else self.native(request);code=0 if value["status"]=="ok" else 2
            if value["status"]=="ok":self.fake_database()
        return subprocess.CompletedProcess(args,code,stdout=encoded(value),stderr=b"")

    def test_original_record_is_forwarded_without_reconstructing_missing_evidence_fields(self):
        body=self.pipeline.ingest(self.payload());self.assertEqual(body["counts"]["stored"],1)
        self.assertEqual(self.calls[1]["record"],self.cfg["exports"]["first"]["events"][0]);self.assertNotEqual(self.calls[0]["cwd"],self.calls[1]["cwd"])

    def test_importer_deduplication_preserves_first_record_and_receipt_time(self):
        body=self.pipeline.ingest(self.payload(["duplicate-batch"]));self.assertEqual(body["counts"]["stored"],3)
        forwarded=[c["record"] for c in self.calls if c["stage"]=="store"]
        self.assertEqual(forwarded[0]["received_at"],self.cfg["exports"]["duplicate-batch"]["events"][0]["received_at"])

    def test_unknown_export_and_empty_request_create_no_state(self):
        self.assertEqual(self.pipeline.ingest(self.payload(["unknown"]))["items"][0]["reason_code"],"registered_synthetic_export_required")
        self.assertEqual(self.pipeline.ingest(self.payload([]))["status"],"empty");self.assertFalse(self.state.exists());self.assertFalse(self.calls)

    def test_body_scope_source_text_policy_or_layer_cannot_override_host(self):
        for key in ("scope","source_id","text","policy","layer","mode"):
            value=self.payload();value[key]="SECRET_OVERRIDE"
            with self.assertRaises(RetrievalInputError):self.pipeline.ingest(value)
        with self.assertRaises(RetrievalInputError):self.pipeline.ingest(self.payload(["../outside"]))
        self.assertFalse(self.calls)

    def test_wrong_domain_channel_actor_and_revision_stop_before_spooling(self):
        original=deepcopy(self.cfg)
        for key,value in (("scope",{**self.cfg["binding"]["scope"],"domain":"chat_history"}),("scope",{**self.cfg["binding"]["scope"],"channel_id":"other"}),("actor_account_id","synthetic-other"),("revision",None)):
            self.cfg=deepcopy(original);self.cfg["exports"]["first"]["events"][0][key]=value;self.pipeline=self.build()
            self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["stage"],"input")
        self.assertFalse(self.state.exists());self.assertFalse(self.calls)

    def test_unknown_denied_missing_purpose_and_read_policy_do_not_cache_text(self):
        for status in ("unknown","denied",None):
            purpose=deepcopy(self.fixture["purpose_policy"])
            if status is None:del purpose["uses"]["local_cache"]
            else:purpose["uses"]["local_cache"]["status"]=status
            self.write("purpose_policy.json",purpose);self.pipeline=self.build();self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"local_cache_not_allowed")
        self.write("purpose_policy.json",self.fixture["purpose_policy"])
        for allowed in (False,None):
            config=deepcopy(self.fixture["importer_config"]);config["sources"][0]["read_policy_allowed"]=allowed;self.write("importer_config.json",config);self.pipeline=self.build()
            self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"channel_source_read_not_allowed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_read_permissions_are_not_inferred_from_write_scope(self):
        config=deepcopy(self.fixture["importer_config"]);config["access_scope"]["read_scopes"]=[];self.write("importer_config.json",config);self.pipeline=self.build()
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"channel_source_read_not_allowed");self.assertFalse(self.calls)

    def test_changed_or_incomplete_evidence_never_becomes_a_store_record(self):
        for transform in (lambda v:v["evidence"][0].update(text="CHANGED_SECRET"),lambda v:v["evidence"].clear(),lambda v:v["evidence"][0].update(access_scope={})): 
            self.import_override=transform;body=self.pipeline.ingest(self.payload());self.assertEqual(body["items"][0]["status"],"held")
        self.assertTrue(all(c["stage"]=="importer" for c in self.calls));self.assertFalse(self.pipeline.database.exists())

    def test_native_no_match_is_preserved_and_never_converted_to_stored(self):
        self.import_override=lambda v:v.update(status="no_match",reason_code="no_events",evidence=[])
        body=self.pipeline.ingest(self.payload(["empty"]));self.assertEqual(body["items"][0]["importer_status"],"no_match");self.assertEqual(body["counts"]["stored"],0);self.assertFalse(self.pipeline.database.exists())

    def test_native_conflict_denies_entire_import_before_any_store_call(self):
        self.import_override=lambda v:v.update(status="error",reason_code="event_revision_conflict",evidence=[])
        item=self.pipeline.ingest(self.payload())["items"][0];self.assertEqual(item["reason_code"],"event_revision_conflict");self.assertEqual(len(self.calls),1)

    def test_task4_null_and_idempotent_success_codes_are_distinct_from_other_stores(self):
        self.store_override=lambda r:self.native(r,reason="idempotent");self.assertEqual(self.pipeline.ingest(self.payload())["counts"]["duplicate"],1)
        self.store_override=lambda r:self.native(r,reason="inserted");item=self.pipeline.ingest(self.payload())["items"][0];self.assertEqual(item["status"],"uncertain");self.assertEqual(item["reason_code"],"channel_ingest_store_ack_unrecognized")

    def test_store_readback_evidence_is_rejected_without_leak(self):
        self.store_override=lambda r:self.native(r,evidence=[{"text":"SECRET_READBACK"}]);body=self.pipeline.ingest(self.payload());self.assertEqual(body["items"][0]["status"],"uncertain");self.assertNotIn("SECRET_READBACK",json.dumps(body))

    def test_native_store_denial_is_not_claimed_committed(self):
        self.store_override=lambda r:self.native(r,status="denied",reason="source_policy_denied");body=self.pipeline.ingest(self.payload());self.assertEqual(body["counts"]["stored"],0);self.assertEqual(body["items"][0]["store_status"],"denied");self.assertFalse(self.pipeline.database.exists())

    def test_missing_attested_export_is_not_silently_regenerated(self):
        self.pipeline.ingest(self.payload());path=self.state/"imports/first/export.json";saved=path.with_suffix(".saved");path.rename(saved);self.calls.clear()
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"channel_ingest_handoff_continuity_failed");self.assertFalse(self.calls);self.assertFalse(path.exists());self.assertTrue(saved.exists())

    def test_owned_database_missing_or_truncated_fails_closed(self):
        self.pipeline.ingest(self.payload());self.pipeline.database.write_bytes(b"");self.calls.clear()
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"channel_ingest_store_continuity_failed");self.assertFalse(self.calls);self.assertEqual(self.pipeline.database.stat().st_size,0)

    def test_configuration_change_and_foreign_state_do_not_invoke_native_programs(self):
        self.write("purpose_policy.json",{});self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"channel_ingest_configuration_changed");self.assertFalse(self.calls)
        self.write("purpose_policy.json",self.fixture["purpose_policy"]);self.pipeline=self.build();self.state.mkdir(parents=True);(self.state/"foreign.txt").write_text("keep",encoding="utf-8")
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"channel_ingest_state_not_owned");self.assertEqual((self.state/"foreign.txt").read_text(),"keep")

    def test_published_or_chat_records_are_not_promoted_to_channel_raw(self):
        record=self.cfg["exports"]["first"]["events"][0];record["record_kind"]="published_statement";self.pipeline=self.build()
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"synthetic_raw_channel_source_required");self.assertFalse(self.state.exists())

    def test_cli_summary_and_receipts_have_no_original_text_actor_or_paths(self):
        body=self.pipeline.ingest(self.payload());combined=json.dumps(body,ensure_ascii=False)+(self.state/"ingest-receipts.jsonl").read_text(encoding="utf-8")
        for secret in (self.cfg["exports"]["first"]["events"][0]["text"],self.cfg["binding"]["actor_account_id"],str(self.state)):self.assertNotIn(secret,combined)

    def test_partial_bundle_commit_is_explicit(self):
        body=self.pipeline.ingest(self.payload(["first","unknown","revision2"]));self.assertEqual(body["status"],"partial");self.assertEqual(body["counts"],{"stored":2,"duplicate":0,"held":1,"uncertain":0})

    def test_cli_does_not_build_chat_runtime_or_connect_to_network(self):
        path=self.write("input.json",self.payload());output=StringIO()
        with patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("runtime forbidden")),patch("socket.socket.connect",side_effect=AssertionError("network forbidden")),patch("ssr_chat.channel_ingest.ChannelIngestPipeline.ingest",return_value={"status":"complete"}),redirect_stdout(output):
            self.assertEqual(main(["ingest-channel","--config",self.path,"--state-dir",str(self.state),"--input",path]),0)
