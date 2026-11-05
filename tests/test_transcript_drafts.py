from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json,re,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from ssr_chat.cli import main
from ssr_chat.chat_ingest import encoded,file_sha
from ssr_chat.stt_ingest import SttIngestPipeline,FIELDS
from ssr_chat.transcript_drafts import TranscriptDraftGateway
from ssr_chat.modular_api import RetrievalInputError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/transcript_drafts/contract.synthetic.json"


class TranscriptDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.archive_override=None;self.draft_override=None;self.trace_override=None;self.ack_override=None;self.fail=False;self.after_draft=None
        for name in ("normalizer/source/cli.py","store/hgpt_ssr/__main__.py","drafter/source/hgpt_ssr/__main__.py"):
            p=self.root/name;p.parent.mkdir(parents=True);p.write_text("# Unit stand-in only\n",encoding="utf-8")
        contract=self.fixture["source_contract"]
        writer={"schema_version":"1.0.1","mode":"synthetic_stt_ingest_only","python":sys._base_executable,"normalizer_repo":str(self.root/"normalizer"),"store_repo":str(self.root/"store"),
                **{k:self.write(k+".json",contract[k]) for k in ("normalizer_policy","store_sources","store_caller","purpose_policy")},"state_root":str(self.root/"source-root"),"binding":self.fixture["binding"]}
        self.writer=self.write("writer.json",writer);self.source_state=self.root/"source-root/owned";self.source=SttIngestPipeline(self.writer,self.source_state);self.source_state.mkdir(parents=True)
        self.source.database.write_bytes(b"unit snapshot stand-in; real SQLite/native integration is separate")
        self.write("source-root/owned/synthetic-stt-owner.json",{"schema_version":"1.0.1","mode":"synthetic_stt_ingest_only","binding_sha256":self.source.fingerprint,"store_file_id":self.source._file_identity()})
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_transcript_drafts_only","python":sys._base_executable,"draft_repo":str(self.root/"drafter"),"source_pipeline":self.writer,"source_state_dir":str(self.source_state),"source_snapshot_sha256":file_sha(self.source.database),
                  **{k:self.write("draft-"+k+".json",self.fixture[k]) for k in ("reader_sources","reader_caller","draft_context","purpose_policy")},"hook":"whitespace-v1","state_root":str(self.root/"draft-root"),"records":deepcopy(self.fixture["records"])}
        self.path=self.write("drafts.json",self.cfg);self.state=self.root/"draft-root/owned";self.gateway=self.build()

    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(encoded(value));return str(p)

    def build(self):self.write("drafts.json",self.cfg);return TranscriptDraftGateway(self.path,self.state,runner=self.runner)
    def payload(self,**kwargs):return {"request_id":"synthetic-unit-draft","synthetic":True,**kwargs}

    @staticmethod
    def evidence(r):return {**{k:r[k] for k in FIELDS-{"schema_version","record_kind","access_scope"}},"layer":"raw","source_kind":"synthetic","rank":None,"match_reasons":["unit"]}
    def result(self,request,evidence):return {"schema_version":"1.0.1","request_id":request,"domain":"stt_transcript","status":"ok","reason_code":"draft_created","evidence":evidence}

    def runner(self,args,**kwargs):
        archive="--db" in args;self.calls.append("archive" if archive else "draft")
        if self.fail:raise subprocess.TimeoutExpired(args,25,output=b"PRIVATE_TIMEOUT_CONTENT")
        if archive:
            query=json.loads(Path(args[args.index("--input")+1]).read_bytes());value=self.result(query["request_id"],[self.evidence(r) for r in self.cfg["records"]])
            if self.archive_override:self.archive_override(value)
            return subprocess.CompletedProcess(args,0 if value["status"]=="ok" else 3,stdout=encoded(value),stderr=b"")
        path=Path(args[args.index("--input")+1]);records=json.loads(path.read_bytes());query=json.loads(Path(args[args.index("--query")+1]).read_bytes());current={}
        for r in records:
            if r["source_event_id"] not in current or current[r["source_event_id"]]["revision"]<r["revision"]:current[r["source_event_id"]]=r
        selected=list(current.values()) if query["conditions"]["revision_view"]=="current" else records;version="unit-whitespace-v1";evidence=[]
        for r in selected:
            e=self.evidence(r);e.update(layer="draft",record_id="draft-"+r["record_id"],source_id="hgpt-ssr-draft/unit",text="\n".join(re.sub(r"[ \t]+"," ",s).strip() for s in r["text"].splitlines()).strip(),source_refs=[*r["source_refs"],{"source_id":r["source_id"],"record_id":r["record_id"],"revision":r["revision"],"locator":"sha256:"+file_sha(path)+"#/"+str(records.index(r)),"transformation_version":version}]);evidence.append(e)
        value=self.result(query["request_id"],evidence);trace={"input_hash":file_sha(path),"draft_count":len(evidence),"transformation_version":version}
        if not query["access_scope"]["write_scopes"]:value.update(status="denied",reason_code="write_scope_not_granted",evidence=[]);trace["draft_count"]=0
        if self.draft_override:self.draft_override(value)
        if self.trace_override:self.trace_override(trace)
        Path(args[args.index("--output")+1]).write_bytes(encoded(value));Path(args[args.index("--trace-output")+1]).write_bytes(encoded(trace))
        if self.after_draft:self.after_draft()
        ack={"status":value["status"],"reason_code":value["reason_code"]}
        if self.ack_override:self.ack_override(ack)
        return subprocess.CompletedProcess(args,0 if value["status"]=="ok" else 1,stdout=encoded(ack),stderr=b"")

    def test_authorized_originals_become_separate_drafts_with_provenance(self):
        body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"drafted");self.assertEqual(body["draft_count"],2);self.assertEqual(self.calls,["archive","draft"])
        originals=next((self.state/"imports").glob("*/originals.json"));self.assertEqual(json.loads(originals.read_bytes()),self.cfg["records"])

    def test_archive_denial_never_becomes_empty_or_success(self):
        self.archive_override=lambda r:r.update(status="denied",reason_code="source_read_denied",evidence=[]);body=self.gateway.draft(self.payload());self.assertIsNone(body["draft_count"]);self.assertEqual(body["status"],"held");self.assertEqual(self.calls,["archive"])

    def test_incomplete_or_changed_evidence_never_supplies_reconstructed_acl(self):
        for fn in (lambda r:r["evidence"].pop(),lambda r:r["evidence"][0].update(text="WRONG_PRIVATE_TEXT"),lambda r:r["evidence"][0].update(access_scope={})):
            self.archive_override=fn;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["draft_count"])
        self.assertTrue(all(x=="archive" for x in self.calls))

    def test_possible_archive_truncation_is_held(self):
        self.archive_override=lambda r:r.update(evidence=r["evidence"]*34);self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"transcript_drafts_archive_may_be_truncated")

    def test_cache_and_cleanup_grants_are_independent_of_read_scope(self):
        for name in ("local_cache","transcript_cleanup"):
            policy=deepcopy(self.fixture["purpose_policy"]);policy[name]["status"]="unknown";self.write("draft-purpose_policy.json",policy);self.gateway=self.build();body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],name+"_not_allowed");self.assertIsNone(body["draft_count"])
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_missing_or_denied_source_read_decision_stops_before_cache(self):
        for decision in (None,{"allowed":False,"basis":"unit-refusal"}):
            policy=deepcopy(self.fixture["purpose_policy"]);policy["source_read_decision"]=decision;self.write("draft-purpose_policy.json",policy);self.gateway=self.build();self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"transcript_source_read_not_allowed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_current_source_and_caller_read_permissions_do_not_borrow_draft_write(self):
        context=deepcopy(self.fixture["draft_context"]);context["access_scope"]["read_scopes"]=[];self.write("draft-draft_context.json",context);self.gateway=self.build();self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"transcript_source_read_not_allowed");self.assertFalse(self.calls)

    def test_read_does_not_grant_native_draft_write(self):
        context=deepcopy(self.fixture["draft_context"]);context["access_scope"]["write_scopes"]=[];self.write("draft-draft_context.json",context);self.gateway=self.build();body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],"write_scope_not_granted");self.assertIsNone(body["draft_count"])

    def test_source_hash_change_stops_before_native_reader(self):
        self.source.database.write_bytes(b"owned unit source changed");self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"transcript_drafts_source_snapshot_changed");self.assertFalse(self.calls)

    def test_source_change_during_draft_withholds_output(self):
        self.after_draft=lambda:self.source.database.write_bytes(b"owned unit source changed mid-draft");body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],"transcript_drafts_source_snapshot_changed");self.assertIsNone(body["draft_count"])

    def test_derived_text_or_metadata_cannot_silently_change_original_content(self):
        for fn in (lambda r:r["evidence"][0].update(text="ADDED_PRIVATE_CONTENT"),lambda r:r["evidence"][0].update(actor_account_id="synthetic-other"),lambda r:r["evidence"][0].update(layer="published")):
            self.draft_override=fn;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["draft_count"])

    def test_trace_or_source_ref_mismatch_is_not_a_confirmed_draft(self):
        self.trace_override=lambda t:t.update(input_hash="0"*64);self.assertEqual(self.gateway.draft(self.payload())["status"],"held");self.trace_override=None
        self.draft_override=lambda r:r["evidence"][0]["source_refs"][-1].update(revision=99);self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"transcript_drafts_provenance_changed")

    def test_native_ack_must_match_persisted_result(self):
        self.ack_override=lambda a:a.update(status="error");body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],"transcript_drafts_ack_not_confirmed");self.assertIsNone(body["draft_count"])

    def test_native_exception_is_bounded_without_retry_or_private_error(self):
        self.fail=True;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertEqual(len(self.calls),1);self.assertNotIn("PRIVATE_TIMEOUT_CONTENT",json.dumps(body))

    def test_caller_cannot_supply_authority_paths_hook_or_summary(self):
        for key in ("scope","actor_account_id","person_id","layer","source_id","hook","kind","draft_repo"):
            with self.assertRaises(RetrievalInputError):self.gateway.draft(self.payload(**{key:"PRIVATE_OVERRIDE"}))
        for value in ("latest",None):
            with self.assertRaises(RetrievalInputError):self.gateway.draft(self.payload(revision_view=value))
        self.assertFalse(self.calls)

    def test_request_id_cannot_escape_hashed_cache_directory(self):
        body=self.gateway.draft(self.payload(request_id=".."));self.assertEqual(body["status"],"drafted");self.assertTrue(all(p.name.startswith("request-") for p in (self.state/"imports").iterdir()))

    def test_foreign_cache_is_preserved_without_adoption(self):
        self.state.mkdir(parents=True);sentinel=self.state/"foreign.txt";sentinel.write_text("preserve");body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],"transcript_drafts_state_not_owned");self.assertEqual(sentinel.read_text(),"preserve")

    def test_cached_source_changes_are_held_on_restart(self):
        self.assertEqual(self.gateway.draft(self.payload())["status"],"drafted");path=next((self.state/"imports").glob("*/originals.json"));path.write_text("[]");self.calls.clear();self.gateway=self.build();self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"transcript_drafts_cache_continuity_failed");self.assertFalse(self.calls)

    def test_configuration_change_is_rechecked_before_native_call(self):
        Path(self.cfg["purpose_policy"]).write_text("{}");body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertFalse(self.calls)

    def test_cli_runs_without_building_chat_model_or_network_runtime(self):
        path=self.write("input.json",self.payload());out=StringIO()
        with patch("ssr_chat.transcript_drafts.TranscriptDraftGateway",return_value=self.gateway),patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("no conversation runtime")),redirect_stdout(out):code=main(["draft-transcript","--config",self.path,"--state-dir",str(self.state),"--input",path])
        self.assertEqual(code,0);body=json.loads(out.getvalue());self.assertEqual(body["draft_count"],2);self.assertFalse(body["model_invoked"]);self.assertFalse(body["audio_used"])


if __name__=="__main__":unittest.main()
