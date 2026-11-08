from contextlib import closing,redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ssr_chat.chat_ingest import digest,encoded,file_sha
from ssr_chat.cli import main
from ssr_chat.game_ingest import GameIngestPipeline
from ssr_chat.modular_api import RetrievalInputError
from ssr_chat.modular_retrieval import PipelineError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/game_storage/contract.synthetic.json"


class GameIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.import_override=None;self.store_override=None
        for name in ("importer/source/hgpt_ssr_cli.py","store/src/hgpt_ssr/__main__.py"):
            path=self.root/name;path.parent.mkdir(parents=True);path.write_text("# Unit native stand-in only\n",encoding="utf-8")
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_game_ingest_only","python":sys._base_executable,"importer_repo":str(self.root/"importer"),"store_repo":str(self.root/"store"),"store_policy":self.write("policy.json",fixture["store_policy"]),
                  "state_root":str(self.root/"sandbox"),**{k:deepcopy(fixture[k]) for k in ("binding","importer_caller","snapshots")}}
        self.state=self.root/"sandbox/normal";self.path=self.write("pipeline.json",self.cfg);self.pipeline=self.build()

    def write(self,name,value):
        path=self.root/name;path.write_bytes(encoded(value));return str(path)

    def build(self):
        self.write("pipeline.json",self.cfg);return GameIngestPipeline(self.path,self.state,runner=self.runner)

    def payload(self,aliases=None):return {"request_id":"synthetic-unit-game","synthetic":True,"snapshots":["first"] if aliases is None else aliases}

    def native(self,request,status,reason,evidence=None):return {"schema_version":"1.0.1","request_id":request,"domain":"game_knowledge","status":status,"reason_code":reason,"evidence":evidence or []}

    def fake_database(self):
        with closing(sqlite3.connect(self.pipeline.database)) as db:
            db.executescript("CREATE TABLE IF NOT EXISTS events(unit_marker TEXT); CREATE INDEX IF NOT EXISTS events_lookup ON events(unit_marker); CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT 1; END; CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT 1; END; CREATE TRIGGER IF NOT EXISTS events_no_replace BEFORE INSERT ON events BEGIN SELECT 1; END; PRAGMA application_id=1213420370; PRAGMA user_version=1;")

    def runner(self,args,**kwargs):
        importing="source/hgpt_ssr_cli.py" in args;self.calls.append({"stage":"importer" if importing else "store","cwd":kwargs["cwd"]})
        if importing:
            query_path=Path(args[args.index("--query")+1]);query=json.loads(query_path.read_bytes());job=query_path.parent;alias=job.name;snapshot=self.cfg["snapshots"][alias];record=self.pipeline._expected(snapshot)
            stable={k:v for k,v in record.items() if k not in {"received_at","access_scope"}}
            handoff={"schema_version":"1.0.1","record":record,"source_locator":self.cfg["binding"]["source_locator"],"source_kind":"synthetic","content_sha256":file_sha(job/"source.txt"),"payload_sha256":digest(stable),"handoff_at":"2026-11-03T23:59:00Z","acquisition":{**deepcopy(snapshot["source"]["settings"]["acquisition"]),"media_type":"text/plain"}}
            if self.import_override:self.import_override(handoff)
            path=job/"handoff/handoff.jsonl";path.parent.mkdir(exist_ok=True);path.write_bytes(encoded(handoff)+b"\n")
            evidence={k:v for k,v in record.items() if k not in {"schema_version","record_kind","access_scope"}};evidence["source_kind"]="synthetic"
            value=self.native(query["request_id"],"ok","handoff_created",[evidence]);code=0
        else:
            self.calls[-1]["record"]=json.loads(kwargs["input"]);request=args[args.index("--request-id")+1]
            value=self.store_override(request) if self.store_override else self.native(request,"ok","inserted");code=0 if value["status"]=="ok" else 1
            if value["status"]=="ok":self.fake_database()
        return subprocess.CompletedProcess(args,code,stdout=encoded(value),stderr=b"")

    def test_explicit_snapshot_to_handoff_to_record_preserves_bytes_and_provenance(self):
        body=self.pipeline.ingest(self.payload());self.assertEqual(body["counts"]["stored"],1)
        self.assertEqual(self.calls[1]["record"],self.pipeline._expected(self.cfg["snapshots"]["first"]))
        self.assertNotEqual(self.calls[0]["cwd"],self.calls[1]["cwd"]);self.assertFalse(body["network_used"]);self.assertFalse(body["model_invoked"])

    def test_unknown_snapshot_and_empty_batch_have_no_state_io(self):
        self.assertEqual(self.pipeline.ingest(self.payload(["unknown"]))["items"][0]["reason_code"],"registered_synthetic_snapshot_required")
        self.assertEqual(self.pipeline.ingest(self.payload([]))["status"],"empty");self.assertFalse(self.state.exists());self.assertFalse(self.calls)

    def test_only_task5_inserted_and_idempotent_are_confirmed_acknowledgements(self):
        self.store_override=lambda request:self.native(request,"ok","idempotent")
        self.assertEqual(self.pipeline.ingest(self.payload())["counts"]["duplicate"],1)
        self.store_override=lambda request:self.native(request,"ok","stored")
        item=self.pipeline.ingest(self.payload())["items"][0]
        self.assertEqual(item["status"],"uncertain");self.assertEqual(item["reason_code"],"game_ingest_store_ack_unrecognized")
        self.assertEqual(item["storage_outcome"],"unknown")

    def test_authority_text_url_and_scope_overrides_are_not_accepted(self):
        for field in ("text","url","scope","registry","source_id","authorization"):
            value=self.payload();value[field]="UNTRUSTED_SECRET"
            with self.assertRaises(RetrievalInputError):self.pipeline.ingest(value)
        with self.assertRaises(RetrievalInputError):self.pipeline.ingest(self.payload(["../outside"]))
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_real_url_collector_and_path_templates_are_rejected(self):
        for field,value in (("source_kind","real"),("source_locator","https://example.invalid/"),("adapter","collector_export")):
            cfg=deepcopy(self.cfg);cfg["snapshots"]["first"]["source"][field]=value;self.write("pipeline.json",cfg)
            with self.assertRaises(PipelineError):GameIngestPipeline(self.path,self.state)
        cfg=deepcopy(self.cfg);cfg["snapshots"]["first"]["source"]["settings"]["path"]="../private.txt";self.write("pipeline.json",cfg)
        with self.assertRaises(PipelineError):GameIngestPipeline(self.path,self.state)

    def test_game_and_version_are_explicit_without_latest_fallback(self):
        for key in ("game_id","content_version"):
            cfg=deepcopy(self.cfg);cfg["binding"]["scope"][key]=None;self.write("pipeline.json",cfg)
            with self.assertRaises(PipelineError):GameIngestPipeline(self.path,self.state)

    def test_unknown_missing_and_denied_cache_purpose_stop_before_plaintext_files(self):
        original=deepcopy(self.cfg)
        for status in ("unknown","denied","prohibited",None):
            self.cfg=deepcopy(original);uses=self.cfg["snapshots"]["first"]["source"]["settings"]["acquisition"]["policy"]["uses"]
            if status is None:del uses["local_cache"]
            else:uses["local_cache"]["status"]=status
            self.pipeline=self.build();self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"local_cache_not_allowed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_source_approval_and_robots_refusal_have_no_plaintext_spool(self):
        original=deepcopy(self.cfg)
        for field in ("source_policy","robots"):
            self.cfg=deepcopy(original);self.cfg["snapshots"]["first"]["source"]["settings"]["acquisition"][field]["decision"]="denied";self.pipeline=self.build()
            self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["stage"],"input")
        self.assertFalse(self.state.exists());self.assertFalse(self.calls)

    def test_importer_requires_both_read_and_write_independently(self):
        original=deepcopy(self.cfg)
        for operation in ("read","write"):
            self.cfg=deepcopy(original);self.cfg["importer_caller"][operation+"_scopes"]=[];self.pipeline=self.build()
            self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"importer_"+operation+"_not_allowed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_handoff_changed_content_hash_or_policy_is_not_forwarded(self):
        for field,value in (("content_sha256","0"*64),("source_kind","real"),("acquisition",{})):
            self.import_override=lambda handoff:handoff.update({field:value})
            result=self.pipeline.ingest(self.payload());self.assertEqual(result["items"][0]["reason_code"],"game_ingest_handoff_invalid")
        self.assertTrue(all(c["stage"]=="importer" for c in self.calls));self.assertFalse(self.pipeline.database.exists())

    def test_store_readback_does_not_leak_to_write_only_summary(self):
        self.store_override=lambda request:self.native(request,"ok","stored",[{"text":"SECRET_READBACK"}])
        body=self.pipeline.ingest(self.payload());self.assertEqual(body["items"][0]["status"],"uncertain");self.assertNotIn("SECRET_READBACK",json.dumps(body))

    def test_lost_store_ack_is_uncertain_and_not_automatically_retried(self):
        def broken(args,**kwargs):
            if "source/hgpt_ssr_cli.py" in args:return self.runner(args,**kwargs)
            self.calls.append({"stage":"store"});self.fake_database();return subprocess.CompletedProcess(args,0,stdout=b"{SECRET_BACKEND_ACK",stderr=b"SECRET_ERROR")
        self.pipeline.runner=broken;body=self.pipeline.ingest(self.payload())
        self.assertEqual(body["items"][0]["status"],"uncertain");self.assertEqual(len(self.calls),2);self.assertNotIn("SECRET",json.dumps(body))

    def test_prior_handoff_cannot_disappear_or_change_before_redelivery(self):
        self.pipeline.ingest(self.payload());path=self.state/"imports/first/handoff/handoff.jsonl";path.write_bytes(b"changed");self.calls.clear()
        item=self.pipeline.ingest(self.payload())["items"][0]
        self.assertEqual(item["reason_code"],"game_ingest_handoff_continuity_failed");self.assertFalse(self.calls);self.assertEqual(path.read_bytes(),b"changed")

    def test_owned_database_cannot_be_truncated_and_adopted_as_empty(self):
        self.pipeline.ingest(self.payload());self.pipeline.database.write_bytes(b"");self.calls.clear()
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"game_ingest_store_continuity_failed")
        self.assertFalse(self.calls);self.assertEqual(self.pipeline.database.stat().st_size,0)

    def test_foreign_state_is_not_overwritten(self):
        self.state.mkdir(parents=True);path=self.state/"foreign.txt";path.write_text("keep",encoding="utf-8")
        self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"game_ingest_state_not_owned");self.assertEqual(path.read_text(),"keep");self.assertFalse(self.calls)

    def test_configuration_changes_stop_before_import_or_store(self):
        self.write("policy.json",{});self.assertEqual(self.pipeline.ingest(self.payload())["items"][0]["reason_code"],"game_ingest_configuration_changed");self.assertFalse(self.calls)

    def test_mixed_batch_is_explicitly_partial(self):
        body=self.pipeline.ingest(self.payload(["first","unknown","equal-text-other-event"]))
        self.assertEqual(body["status"],"partial");self.assertEqual(body["counts"],{"stored":2,"duplicate":0,"held":1,"uncertain":0})

    def test_summaries_and_purpose_receipts_do_not_contain_snapshot_content(self):
        body=self.pipeline.ingest(self.payload());text=json.dumps(body,ensure_ascii=False)+(self.state/"ingest-receipts.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(self.cfg["snapshots"]["first"]["content"],text);self.assertNotIn(str(self.state),text)

    def test_cli_never_builds_chat_runtime_or_opens_network(self):
        path=self.write("input.json",self.payload());output=StringIO()
        with patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("chat runtime forbidden")),patch("socket.socket.connect",side_effect=AssertionError("network forbidden")),patch("ssr_chat.game_ingest.GameIngestPipeline.ingest",return_value={"status":"complete"}),redirect_stdout(output):
            self.assertEqual(main(["ingest-game","--config",self.path,"--state-dir",str(self.state),"--input",path]),0)
