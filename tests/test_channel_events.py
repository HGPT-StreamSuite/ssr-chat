from contextlib import redirect_stdout,closing
from copy import deepcopy
from io import StringIO
import json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from ssr_chat.channel_events import ChannelEventGateway
from ssr_chat.chat_ingest import RECORD,digest,encoded,file_sha
from ssr_chat.cli import main
from ssr_chat.modular_api import RetrievalInputError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/channel_events/contract.synthetic.json"


class ChannelEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.fail_before=False;self.fail_after=False;self.read_override=None;self.after_read=None
        for n in ("native/run.py","native/src/hgpt_ssr/store.py"):
            p=self.root/n;p.parent.mkdir(parents=True);p.write_text("# Unit native stand-in; actual candidate integration is separate\n")
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_channel_events_only","python":sys._base_executable,"events_repo":str(self.root/"native"),"writer_policy":self.write("writer.json",self.fixture["writer_policy"]),"reader_policy":self.write("reader.json",self.fixture["reader_policy"]),"state_root":str(self.root/"states"),**{k:deepcopy(self.fixture[k]) for k in ("binding","record_visibility","events","queries")}}
        self.path=self.write("config.json",self.cfg);self.state=self.root/"states/owned";self.gateway=self.build()

    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(encoded(value));return str(p)
    def build(self):self.write("config.json",self.cfg);return ChannelEventGateway(self.path,self.state,runner=self.runner)
    def ingest(self,*aliases):return self.gateway.run({"request_id":"synthetic-unit-ingest","synthetic":True,"operation":"ingest","events":list(aliases)})
    def read(self,operation="get",alias="all"):return self.gateway.run({"request_id":"synthetic-unit-read","synthetic":True,"operation":operation,"query":alias})
    @staticmethod
    def evidence(value):
        r=value["record"];facts=value["facts"];reasons=["explicit_event_type:"+facts["event_type"]]
        if facts["event_type"]=="relationship":reasons.append("relationship_status:"+facts["status"])
        return {**{k:r[k] for k in RECORD-{"schema_version","record_kind","access_scope"}},"layer":"raw","source_kind":"synthetic","rank":None,"match_reasons":reasons}

    def runner(self,args,**kwargs):
        ingest="ingest" in args;export="export" in args;self.calls.append("ingest" if ingest else "export" if export else "get");db=Path(args[args.index("--db")+1])
        if self.fail_before:raise subprocess.TimeoutExpired(args,25,output=b"PRIVATE_FAILURE_TEXT")
        if ingest:
            r=json.loads(Path(args[args.index("--input")+1]).read_bytes());key=r["record"]["source_event_id"]+"-"+str(r["record"]["revision"])
            with closing(sqlite3.connect(db)) as conn,conn:
                conn.execute("CREATE TABLE IF NOT EXISTS raw_events (identity TEXT PRIMARY KEY, payload TEXT)");conn.execute("PRAGMA application_id=0x48535352");conn.execute("PRAGMA user_version=2")
                exists=conn.execute("SELECT 1 FROM raw_events WHERE identity=?",(key,)).fetchone()
                if not exists:conn.execute("INSERT INTO raw_events VALUES (?,?)",(key,json.dumps(r,ensure_ascii=False)))
            if self.fail_after:raise subprocess.TimeoutExpired(args,25,output=b"PRIVATE_COMMITTED_ACK_LOSS")
            request=args[args.index("--request-id")+1];status="ok";reason="idempotent_replay" if exists else "ingested";evidence=[]
        else:
            q=json.loads(Path(args[args.index("--query")+1]).read_bytes());request=q["request_id"]
            if db.exists():
                with closing(sqlite3.connect(db)) as conn:records=[json.loads(r[0]) for r in conn.execute("SELECT payload FROM raw_events ORDER BY rowid")]
            else:records=[]
            if export and records:Path(args[args.index("--output")+1]).write_bytes(b"".join(encoded(r)+b"\n" for r in records))
            status="ok" if records else "not_configured";reason="matched" if records else "source_not_collected";evidence=[self.evidence(r) for r in records]
        value={"schema_version":"1.0.1","request_id":request,"domain":"person_profile","status":status,"reason_code":reason,"evidence":evidence}
        if not ingest and self.read_override:self.read_override(value)
        if not ingest and self.after_read:self.after_read()
        return subprocess.CompletedProcess(args,0 if value["status"]=="ok" else 3,stdout=encoded(value),stderr=b"")

    def test_registered_explicit_events_reach_owned_ledger_without_raw_receipt(self):
        body=self.ingest("active","visit");self.assertEqual(body["counts"]["stored"],2);self.assertEqual(self.calls,["ingest","ingest"]);self.assertTrue(self.gateway.database.exists());self.assertNotIn(self.fixture["events"]["active"]["envelope"]["record"]["text"],json.dumps(body,ensure_ascii=False))

    def test_redelivery_and_restart_delegate_persistent_idempotency(self):
        self.ingest("active");self.gateway=self.build();body=self.ingest("active");self.assertEqual(body["counts"]["duplicate"],1)

    def test_unregistered_or_unconfirmed_declaration_never_calls_native(self):
        self.cfg["events"]["active"]["declaration"]["status"]="unknown";self.gateway=self.build();body=self.ingest("active","unknown");self.assertEqual(body["counts"]["held"],2);self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_changed_declaration_payload_hash_is_not_silently_accepted(self):
        self.cfg["events"]["active"]["envelope"]["facts"]["status"]="revoked";self.gateway=self.build();self.assertEqual(self.ingest("active")["items"][0]["reason_code"],"channel_events_source_declaration_not_confirmed");self.assertFalse(self.calls)

    def test_text_does_not_supply_missing_facts_or_generate_visit(self):
        value=self.cfg["events"]["active"];value["envelope"].pop("facts");value["envelope"]["record"]["text"]="visit active linked person";self.gateway=self.build();self.assertEqual(self.ingest("active")["items"][0]["reason_code"],"channel_events_explicit_facts_required");self.assertFalse(self.calls)

    def test_cache_denial_stops_plaintext_handoff_and_store_creation(self):
        p=deepcopy(self.fixture["writer_policy"]);p["sources"][self.cfg["binding"]["source_id"]]["policy"]["uses"]["local_cache"]["status"]="denied";self.write("writer.json",p);self.gateway=self.build();self.assertEqual(self.ingest("active")["counts"]["held"],1);self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_malformed_cache_uses_is_fail_closed(self):
        p=deepcopy(self.fixture["writer_policy"]);p["sources"][self.cfg["binding"]["source_id"]]["policy"]["uses"]=None;self.write("writer.json",p);self.gateway=self.build();self.assertEqual(self.ingest("active")["counts"]["held"],1);self.assertFalse(self.calls)

    def test_read_evidence_and_export_keep_declared_raw_envelopes(self):
        self.ingest("active","visit");before=file_sha(self.gateway.database);self.assertEqual(self.read()["event_count"],2);self.assertEqual(self.read("export","history")["event_count"],2);self.assertEqual(file_sha(self.gateway.database),before)

    def test_native_denial_is_not_no_match_or_zero_count(self):
        self.ingest("active");self.read_override=lambda r:r.update(status="denied",reason_code="source_purpose_denied",evidence=[]);body=self.read();self.assertEqual(body["status"],"held");self.assertIsNone(body["event_count"])

    def test_unregistered_native_text_or_facts_are_withheld(self):
        self.ingest("active");self.read_override=lambda r:r["evidence"][0].update(text="PRIVATE_UNREGISTERED_TEXT");self.assertEqual(self.read()["status"],"held")

    def test_source_change_during_native_read_is_withheld(self):
        self.ingest("active");self.after_read=lambda:self.gateway.database.write_bytes(b"unit corruption");body=self.read();self.assertEqual(body["status"],"held");self.assertIsNone(body["event_count"])

    def test_ack_loss_is_uncertain_and_explicit_redelivery_confirms_duplicate(self):
        self.fail_after=True;body=self.ingest("active");self.assertEqual(body["counts"]["uncertain"],1);self.assertEqual(len(self.calls),1);self.fail_after=False;self.gateway=self.build();self.assertEqual(self.ingest("active")["counts"]["duplicate"],1)

    def test_missing_owned_database_is_not_recreated(self):
        self.ingest("active");self.gateway.database.unlink();self.calls.clear();self.assertEqual(self.ingest("active")["counts"]["held"],1);self.assertFalse(self.calls);self.assertFalse(self.gateway.database.exists())

    def test_same_file_lost_schema_is_not_adopted_as_empty(self):
        self.ingest("active");self.gateway.database.write_bytes(b"");self.calls.clear();self.assertEqual(self.ingest("active")["counts"]["held"],1);self.assertFalse(self.calls)

    def test_foreign_nonempty_state_is_preserved(self):
        self.state.mkdir(parents=True);p=self.state/"sentinel";p.write_text("preserve");self.assertEqual(self.ingest("active")["counts"]["held"],1);self.assertEqual(p.read_text(),"preserve")

    def test_cached_handoff_tampering_is_held_on_restart(self):
        self.ingest("active");next((self.state/"imports").glob("*/event.json")).write_text("{}");self.calls.clear();self.gateway=self.build();self.assertEqual(self.ingest("active")["counts"]["held"],1);self.assertFalse(self.calls)

    def test_policy_updates_preserve_data_owner_but_are_rechecked_per_operation(self):
        self.ingest("active");identity=self.gateway.identity;p=deepcopy(self.fixture["reader_policy"]);p["sources"][self.cfg["binding"]["source_id"]]["policy"]["uses"]["search_index"]["status"]="denied";self.write("reader.json",p);self.assertEqual(self.read()["status"],"held");self.gateway=self.build();self.assertEqual(self.gateway.identity,identity)

    def test_request_cannot_supply_manual_link_approval_paths_or_authority(self):
        base={"request_id":"synthetic-unit","synthetic":True,"operation":"get","query":"all"}
        for key in ("scope","actor_account_id","person_id","policy","facts","human_confirmed","source_id"):
            with self.assertRaises(RetrievalInputError):self.gateway.run({**base,key:"PRIVATE_OVERRIDE"})
        with self.assertRaises(RetrievalInputError):self.gateway.run({**base,"query":{}})
        self.assertFalse(self.calls)

    def test_query_operation_mismatch_cannot_switch_to_export(self):
        body=self.read("get","history");self.assertEqual(body["status"],"held");self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_cli_runs_without_chat_model_or_identity_runtime(self):
        path=self.write("input.json",{"request_id":"synthetic-cli","synthetic":True,"operation":"ingest","events":["active"]});out=StringIO()
        with patch("ssr_chat.channel_events.ChannelEventGateway",return_value=self.gateway),patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("no chat runtime")),redirect_stdout(out):code=main(["channel-events","--config",self.path,"--state-dir",str(self.state),"--input",path])
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())["counts"]["stored"],1)


if __name__=="__main__":unittest.main()
