from contextlib import closing,redirect_stdout
from copy import deepcopy
from io import StringIO
import json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from ssr_chat.identity_records import IdentityRecordGateway
from ssr_chat.chat_ingest import RECORD,digest,encoded,file_sha
from ssr_chat.cli import main
from ssr_chat.modular_api import RetrievalInputError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/identity_records/contract.synthetic.json"


class IdentityRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.fail_after=False;self.read_override=None;self.after_read=None
        for n in ("native/source/ssr_identity/__main__.py","native/source/ssr_identity/store.py"):
            p=self.root/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("# Unit native stand-in; real native integration is separate\n")
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_identity_records_only","python":sys._base_executable,"identity_repo":str(self.root/"native"),**{k:self.write(k+".json",self.fixture[k]) for k in ("writer_access","reader_access","purpose_policy")},"state_root":str(self.root/"states"),**{k:deepcopy(self.fixture[k]) for k in ("binding","record_visibility","events","queries")}}
        self.path=self.write("config.json",self.cfg);self.state=self.root/"states/owned";self.gateway=self.build()
    def write(self,n,v):
        p=self.root/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(encoded(v));return str(p)
    def build(self):self.write("config.json",self.cfg);return IdentityRecordGateway(self.path,self.state,runner=self.runner)
    def ingest(self,*aliases):return self.gateway.run({"request_id":"synthetic-unit-ingest","synthetic":True,"operation":"ingest","events":list(aliases)})
    def read(self,operation="query",alias="person1"):return self.gateway.run({"request_id":"synthetic-unit-read","synthetic":True,"operation":operation,"query":alias})
    def runner(self,args,**kwargs):
        command=args[args.index("--db")+2];self.calls.append(command);db=Path(args[args.index("--db")+1]);code=0
        if command=="init":
            with closing(sqlite3.connect(db)) as conn,conn:
                conn.execute("CREATE TABLE sources (unit TEXT)");conn.execute("CREATE TABLE identity_events (unit TEXT)");conn.execute("CREATE TABLE record_revisions (identity TEXT PRIMARY KEY, kind TEXT, entity TEXT, payload TEXT)");conn.execute("PRAGMA application_id=0x53535249");conn.execute("PRAGMA user_version=3")
            data={"path":str(db.resolve()),"schema_version":3}
        elif command=="source-configure":data={"scope":json.loads(Path(args[args.index("--scope-file")+1]).read_bytes()),"source_id":args[args.index("--source-id")+1],"source_kind":"synthetic"}
        elif command in {"person-ingest","account-ingest"}:
            r=json.loads(Path(args[args.index("--record-file")+1]).read_bytes());kind="person" if command=="person-ingest" else "platform_account";entity=args[args.index("--person-id")+1] if kind=="person" else r["actor_account_id"];key=r["source_event_id"]+"-"+str(r["revision"])
            with closing(sqlite3.connect(db)) as conn,conn:
                exists=conn.execute("SELECT 1 FROM record_revisions WHERE identity=?",(key,)).fetchone()
                if not exists:conn.execute("INSERT INTO record_revisions VALUES (?,?,?,?)",(key,kind,entity,json.dumps(r,ensure_ascii=False)))
            if self.fail_after:raise subprocess.TimeoutExpired(args,25,output=b"PRIVATE_COMMITTED_ACK")
            data={"stored":not bool(exists),"record_id":r["record_id"],"revision":r["revision"]}
        else:
            rows=[]
            if db.exists():
                with closing(sqlite3.connect(db)) as conn:rows=[(k,e,json.loads(r)) for k,e,r in conn.execute("SELECT kind,entity,payload FROM record_revisions ORDER BY rowid")]
            if command=="query":
                q=json.loads(Path(args[args.index("--query-file")+1]).read_bytes());kind="person" if q["person_id"] is not None else "platform_account";entity=q["person_id"] if kind=="person" else q["actor_account_id"];records=[r for k,e,r in rows if k==kind and e==entity]
                evidence=[{**{key:r[key] for key in RECORD-{"schema_version","record_kind","access_scope"}},
                           **({"extensions":r["extensions"]} if "extensions" in r else {}),
                           "layer":"raw","source_kind":"synthetic","rank":None,"match_reasons":["unit"]} for r in records]
                value={"schema_version":"1.0.1","request_id":q["request_id"],"domain":"person_profile","status":"ok" if evidence else "no_match","reason_code":None,"evidence":evidence}
                if self.read_override:self.read_override(value)
                if self.after_read:self.after_read()
                return subprocess.CompletedProcess(args,0 if value["status"] in {"ok","no_match"} else 5,stdout=encoded(value),stderr=b"")
            event=args[args.index("--source-event-id")+1];records=[r for _,_,r in rows if r["source_event_id"]==event];records.sort(key=lambda r:r["revision"])
            data=records if command=="record-history" else records[-1] if records else None
        return subprocess.CompletedProcess(args,code,stdout=encoded({"command":command,"data":data}),stderr=b"")

    def test_native_bootstrap_and_raw_ingest_are_separate_from_linking(self):
        body=self.ingest("person1","account-main");self.assertEqual(body["counts"]["stored"],2);self.assertEqual(self.calls[0],"init");self.assertEqual(self.calls.count("source-configure"),2);self.assertNotIn("link",self.calls)
    def test_fresh_process_redelivery_delegates_durable_idempotency(self):
        self.ingest("account-main");self.gateway=self.build();self.assertEqual(self.ingest("account-main")["counts"]["duplicate"],1)
    def test_unconfirmed_annotation_is_not_guessed_from_display_name(self):
        self.cfg["events"]["person1"]["declaration"]["status"]="unknown";self.gateway=self.build();self.assertEqual(self.ingest("person1")["counts"]["held"],1);self.assertFalse(self.calls)
    def test_undeclared_account_or_person_scope_is_held(self):
        self.cfg["events"]["account-main"]["record"]["actor_account_id"]="synthetic-account-unlisted";self.gateway=self.build();self.assertEqual(self.ingest("account-main")["counts"]["held"],1);self.assertFalse(self.calls)
    def test_changed_annotation_hash_cannot_rebind_person(self):
        self.cfg["events"]["person1"]["person_id"]="synthetic-person-2";self.gateway=self.build();self.assertEqual(self.ingest("person1")["items"][0]["reason_code"],"identity_records_declaration_not_confirmed");self.assertFalse(self.calls)
    def test_current_source_write_revocation_stops_raw_ingest(self):
        p=deepcopy(self.fixture["purpose_policy"]);p["write_decision"]["allowed"]=False;self.write("purpose_policy.json",p);self.gateway=self.build();self.assertEqual(self.ingest("person1")["counts"]["held"],1);self.assertFalse(self.state.exists())
    def test_current_source_read_revocation_never_returns_old_data(self):
        self.ingest("person1");p=deepcopy(self.fixture["purpose_policy"]);p["read_decision"]=None;self.write("purpose_policy.json",p);self.gateway=self.build();self.calls.clear();body=self.read();self.assertEqual(body["status"],"held");self.assertIsNone(body["record_count"]);self.assertFalse(self.calls)
    def test_cache_grant_is_not_borrowed_from_caller_write(self):
        p=deepcopy(self.fixture["purpose_policy"]);p["uses"]["local_cache"]["status"]="denied";self.write("purpose_policy.json",p);self.gateway=self.build();self.assertEqual(self.ingest("person1")["counts"]["held"],1);self.assertFalse(self.state.exists())
    def test_read_use_grant_is_independent_of_cache_permission(self):
        self.ingest("person1");p=deepcopy(self.fixture["purpose_policy"]);p["uses"]["search_index"]["status"]="unknown";self.write("purpose_policy.json",p);self.gateway=self.build();self.calls.clear();self.assertEqual(self.read()["status"],"held");self.assertFalse(self.calls)
    def test_readback_matches_original_records_without_acl_reconstruction(self):
        self.ingest("account-main");before=file_sha(self.gateway.database);body=self.read("history","main-history");self.assertEqual(body["record_count"],1);self.assertEqual(file_sha(self.gateway.database),before)
    def test_unregistered_native_evidence_is_withheld(self):
        self.ingest("person1");self.read_override=lambda r:r["evidence"][0].update(text="PRIVATE_UNREGISTERED_TEXT");body=self.read();self.assertEqual(body["status"],"held");self.assertIsNone(body["record_count"])
    def test_native_denial_is_not_reported_as_empty_success(self):
        self.ingest("person1");self.read_override=lambda r:r.update(status="denied",reason_code="scope_not_allowed",evidence=[]);self.assertIsNone(self.read()["record_count"])
    def test_source_change_during_read_is_held(self):
        self.ingest("person1");self.after_read=lambda:self.gateway.database.write_bytes(b"unit corruption");self.assertEqual(self.read()["status"],"held")
    def test_lost_raw_write_ack_is_uncertain_without_automatic_retry(self):
        self.fail_after=True;body=self.ingest("account-main");self.assertEqual(body["counts"]["uncertain"],1);self.assertEqual(self.calls.count("account-ingest"),1);self.fail_after=False;self.gateway=self.build();self.assertEqual(self.ingest("account-main")["counts"]["duplicate"],1)
    def test_missing_owned_database_is_not_reinitialized(self):
        self.ingest("person1");self.gateway.database.unlink();self.calls.clear();self.assertEqual(self.ingest("person1")["counts"]["held"],1);self.assertFalse(self.calls)
    def test_truncated_existing_database_is_not_adopted_as_new(self):
        self.ingest("person1");self.gateway.database.write_bytes(b"");self.calls.clear();self.assertEqual(self.ingest("person1")["counts"]["held"],1);self.assertFalse(self.calls)
    def test_foreign_state_and_changed_handoff_are_preserved(self):
        self.state.mkdir(parents=True);p=self.state/"sentinel";p.write_text("preserve");self.assertEqual(self.ingest("person1")["counts"]["held"],1);self.assertEqual(p.read_text(),"preserve")
    def test_policy_update_keeps_data_identity_and_requires_fresh_authorization(self):
        self.ingest("person1");identity=self.gateway.identity;p=deepcopy(self.fixture["purpose_policy"]);p["read_decision"]["allowed"]=False;self.write("purpose_policy.json",p);self.assertEqual(self.read()["status"],"held");self.gateway=self.build();self.assertEqual(self.gateway.identity,identity)
    def test_request_cannot_inject_ids_links_approval_or_paths(self):
        payload={"request_id":"synthetic-unit","synthetic":True,"operation":"query","query":"person1"}
        for key in ("person_id","actor_account_id","link","human_confirmed","scope","source_id","policy"):
            with self.assertRaises(RetrievalInputError):self.gateway.run({**payload,key:"PRIVATE_OVERRIDE"})
        self.assertFalse(self.calls)
    def test_cli_does_not_build_chat_model_or_manual_link_runtime(self):
        path=self.write("input.json",{"request_id":"synthetic-unit-cli","synthetic":True,"operation":"ingest","events":["person1"]});out=StringIO()
        with patch("ssr_chat.identity_records.IdentityRecordGateway",return_value=self.gateway),patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("no chat runtime")),redirect_stdout(out):code=main(["identity-records","--config",self.path,"--state-dir",str(self.state),"--input",path])
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())["counts"]["stored"],1)

    def declare_extensions(self,value):
        event=self.cfg["events"]["person1"];event["record"]["extensions"]=value
        event["declaration"]["payload_sha256"]=digest({k:event[k] for k in ("entity_kind","person_id","record")})
        self.cfg["queries"]["person1-get"]={"operation":"get","source_event_id":event["record"]["source_event_id"],"revision":event["record"]["revision"]}
        self.gateway=self.build()

    def test_native_optional_empty_extensions_survive_declared_ingest_query_and_get(self):
        self.declare_extensions({});self.assertEqual(self.ingest("person1")["counts"]["stored"],1)
        self.assertEqual(self.read()["record_count"],1)
        query=list((self.state/"imports").rglob("native-result.json"))[-1]
        self.assertEqual(json.loads(query.read_bytes())["evidence"][0]["extensions"],{})
        self.assertEqual(self.read("get","person1-get")["record_count"],1)

    def test_optional_extensions_do_not_bypass_original_declaration(self):
        self.cfg["events"]["person1"]["record"]["extensions"]={};self.gateway=self.build()
        self.assertEqual(self.ingest("person1")["items"][0]["reason_code"],"identity_records_declaration_not_confirmed")
        self.assertFalse(self.calls)

    def test_nonempty_or_nonobject_identity_extensions_remain_rejected(self):
        for value in ({"person_id":"synthetic-person-injected"},None,[],True):
            with self.subTest(value=value):
                self.declare_extensions(value)
                self.assertEqual(self.ingest("person1")["items"][0]["reason_code"],"identity_records_synthetic_record_not_bound")
        self.assertFalse(self.calls)

    def test_query_cannot_drop_declared_empty_extensions(self):
        self.declare_extensions({});self.ingest("person1")
        self.read_override=lambda r:r["evidence"][0].pop("extensions")
        self.assertEqual(self.read()["reason_code"],"identity_records_evidence_not_declared_original")

    def test_query_cannot_insert_undeclared_empty_extensions(self):
        self.ingest("person1");self.read_override=lambda r:r["evidence"][0].update(extensions={})
        self.assertEqual(self.read()["reason_code"],"identity_records_evidence_not_declared_original")


if __name__=="__main__":unittest.main()
