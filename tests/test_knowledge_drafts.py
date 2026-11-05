from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from ssr_chat.cli import main
from ssr_chat.chat_ingest import RECORD,encoded,file_sha
from ssr_chat.channel_ingest import ChannelIngestPipeline
from ssr_chat.knowledge_drafts import KnowledgeDraftGateway
from ssr_chat.modular_api import RetrievalInputError

FIXTURES=Path(__file__).resolve().parents[1]/"fixtures/knowledge_drafts"


class KnowledgeDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.fixture=json.loads((FIXTURES/"contract.synthetic.json").read_text(encoding="utf-8"));self.envelope=json.loads((FIXTURES/"native-envelope.synthetic.json").read_text(encoding="utf-8"))["envelope"];self.calls=[];self.archive_override=None;self.native_override=None;self.fail=False;self.after_native=None
        for name in ("importer/source/hgpt_ssr.py","store/source/ssr.py","drafter/source/hgpt_ssr/engine.py"):
            p=self.root/name;p.parent.mkdir(parents=True);p.write_text("# Unit stand-in only\n",encoding="utf-8")
        contract=self.fixture["source_contract"];binding=self.fixture["binding"]
        writer={"schema_version":"1.0.1","mode":"synthetic_channel_ingest_only","python":sys._base_executable,"importer_repo":str(self.root/"importer"),"store_repo":str(self.root/"store"),
                **{k:self.write("source-"+k+".json",contract[k]) for k in ("importer_config","store_context","purpose_policy")},"state_root":str(self.root/"source-root"),"binding":binding,"exports":{"registered":{"schema_version":"1.0.1","source_id":binding["source_id"],"scope":binding["scope"],"events":self.fixture["records"]}}}
        self.writer=self.write("writer.json",writer);self.source_state=self.root/"source-root/owned";self.source=ChannelIngestPipeline(self.writer,self.source_state);self.source_state.mkdir(parents=True);self.source.database.write_bytes(b"unit source stand-in; real SQLite/API checks are separate")
        self.write("source-root/owned/synthetic-channel-owner.json",{"mode":"synthetic_channel_ingest_only","binding_sha256":self.source.fingerprint,"store_file_id":self.source._file_id(self.source.database),"files":{}})
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_knowledge_drafts_only","python":sys._base_executable,"draft_repo":str(self.root/"drafter"),"source_pipeline":self.writer,"source_state_dir":str(self.source_state),"source_snapshot_sha256":file_sha(self.source.database),
                  **{k:self.write("draft-"+k+".json",self.fixture[k]) for k in ("reader_context","draft_access","purpose_policy")},"hook":{"name":"quote_fragments","separator":"\n"},"caller_marks":deepcopy(self.fixture["caller_marks"]),"state_root":str(self.root/"draft-root"),"records":deepcopy(self.fixture["records"])}
        self.path=self.write("drafts.json",self.cfg);self.state=self.root/"draft-root/owned";self.gateway=self.build()

    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(encoded(value));return str(p)
    def build(self):self.write("drafts.json",self.cfg);return KnowledgeDraftGateway(self.path,self.state,runner=self.runner)
    def payload(self,**kwargs):return {"request_id":"synthetic-unit-knowledge","synthetic":True,**kwargs}
    @staticmethod
    def evidence(r):return {**{k:r[k] for k in RECORD-{"schema_version","record_kind","access_scope"}},"layer":"raw","source_kind":"synthetic","rank":None,"match_reasons":["unit"]}

    def runner(self,args,**kwargs):
        archive="--db" in args;self.calls.append("archive" if archive else "draft")
        if self.fail:raise subprocess.TimeoutExpired(args,25,output=b"PRIVATE_TIMEOUT_OUTPUT")
        if archive:
            query=json.loads(Path(args[args.index("--query-file")+1]).read_bytes());value={"schema_version":"1.0.1","request_id":query["request_id"],"domain":"channel_knowledge","status":"ok","reason_code":None,"evidence":[self.evidence(r) for r in self.cfg["records"]]}
            if self.archive_override:self.archive_override(value)
            return subprocess.CompletedProcess(args,0 if value["status"]=="ok" else 3,stdout=encoded(value),stderr=b"")
        bundle=json.loads(Path(args[-1]).read_bytes());value=deepcopy(self.envelope);value["result"]["request_id"]=bundle["query"]["request_id"]
        if self.native_override:self.native_override(value)
        if self.after_native:self.after_native()
        return subprocess.CompletedProcess(args,0 if value["result"]["status"]=="ok" else 1,stdout=encoded(value),stderr=b"")

    def test_real_captured_envelope_stand_in_is_validated_and_stored_separately(self):
        body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"drafted");self.assertEqual(body["draft_count"],2);self.assertEqual(self.calls,["archive","draft"]);self.assertTrue(next((self.state/"imports").glob("*/completion.json")).is_file())

    def test_completed_redelivery_and_restart_do_not_invoke_native_again(self):
        self.gateway.draft(self.payload());self.calls.clear();self.gateway=self.build();body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"duplicate");self.assertTrue(body["replayed"]);self.assertFalse(self.calls)

    def test_cache_and_draft_grants_are_independent_of_archive_read(self):
        for grant in ("local_cache","knowledge_drafts"):
            p=deepcopy(self.fixture["purpose_policy"]);p[grant]["status"]="unknown";self.write("draft-purpose_policy.json",p);self.gateway=self.build();body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],grant+"_not_allowed");self.assertIsNone(body["draft_count"])
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_missing_archive_decision_prevents_cache_and_native_calls(self):
        p=deepcopy(self.fixture["purpose_policy"]);p["archive_read_decision"]=None;self.write("draft-purpose_policy.json",p);self.gateway=self.build();self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"knowledge_archive_read_not_allowed");self.assertFalse(self.calls)

    def test_archive_denial_is_not_empty_draft_success(self):
        self.archive_override=lambda v:v.update(status="denied",reason_code="source_read_denied",evidence=[]);body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["draft_count"]);self.assertEqual(self.calls,["archive"])

    def test_incomplete_changed_or_acl_augmented_evidence_never_reconstructs_originals(self):
        for fn in (lambda v:v["evidence"].pop(),lambda v:v["evidence"][0].update(text="WRONG_PRIVATE_TEXT"),lambda v:v["evidence"][0].update(access_scope={})):
            self.archive_override=fn;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["draft_count"])
        self.assertTrue(all(x=="archive" for x in self.calls))

    def test_potential_archive_truncation_is_held(self):
        self.archive_override=lambda v:v.update(evidence=v["evidence"]*50);self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"knowledge_drafts_archive_may_be_truncated")

    def test_changed_snapshot_cannot_return_old_completed_draft(self):
        self.gateway.draft(self.payload());self.source.database.write_bytes(b"owned synthetic source change");body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],"knowledge_drafts_source_snapshot_changed");self.assertIsNone(body["draft_count"])

    def test_source_change_during_native_api_withholds_result(self):
        self.after_native=lambda:self.source.database.write_bytes(b"owned source changed mid-transform");body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["draft_count"]);self.assertFalse(list((self.state/"imports").glob("*/completion.json")))

    def test_changed_literal_text_label_author_or_layer_is_held(self):
        for fn in (lambda v:v["result"]["evidence"][0].update(text="ADDED_PRIVATE_TEXT"),lambda v:v["result"]["evidence"][0].update(actor_account_id="synthetic-other"),lambda v:v["result"]["evidence"][0].update(layer="published"),lambda v:v["audit"]["draft_sources"][0].update(caller_label="inference")):
            self.native_override=fn;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["draft_count"])

    def test_false_policy_attestation_or_changed_input_hash_is_held(self):
        for fn in (lambda v:v["policy_decisions"][0].update(allowed=False),lambda v:v["audit"].update(input_hash_after="0"*64)):
            self.native_override=fn;self.assertEqual(self.gateway.draft(self.payload())["status"],"held")

    def test_original_audit_or_fragment_reference_changes_are_held(self):
        self.native_override=lambda v:v["audit"]["draft_sources"][0]["sources"][0]["record"].update(text="PRIVATE_AUDIT_TAMPER");self.assertEqual(self.gateway.draft(self.payload())["status"],"held")

    def test_lost_in_memory_native_response_is_held_without_automatic_retry(self):
        self.fail=True;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.assertEqual(len(self.calls),1);self.assertNotIn("PRIVATE_TIMEOUT_OUTPUT",json.dumps(body))

    def test_incomplete_persistence_is_not_overwritten_on_restart(self):
        def partial(job,envelope,summary):
            (job/"native-envelope.json").write_bytes(encoded(envelope));raise OSError("constructed local completion loss")
        self.gateway._persist=partial;body=self.gateway.draft(self.payload());self.assertEqual(body["status"],"held");self.calls.clear();self.gateway=self.build();body=self.gateway.draft(self.payload());self.assertEqual(body["reason_code"],"knowledge_drafts_completion_incomplete");self.assertFalse(self.calls)

    def test_cached_output_tampering_is_held_before_replay(self):
        self.gateway.draft(self.payload());next((self.state/"imports").glob("*/native-envelope.json")).write_text("{}");self.calls.clear();self.gateway=self.build();self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"knowledge_drafts_cache_continuity_failed");self.assertFalse(self.calls)

    def test_foreign_cache_is_not_adopted_or_deleted(self):
        self.state.mkdir(parents=True);p=self.state/"sentinel";p.write_text("preserve");self.assertEqual(self.gateway.draft(self.payload())["reason_code"],"knowledge_drafts_state_not_owned");self.assertEqual(p.read_text(),"preserve")

    def test_request_cannot_change_authority_marks_hook_or_paths(self):
        for key in ("scope","actor_account_id","person_id","layer","source_id","hook","caller_marks","source_pipeline","time_to"):
            with self.assertRaises(RetrievalInputError):self.gateway.draft(self.payload(**{key:"PRIVATE_OVERRIDE"}))
        self.assertFalse(self.calls)

    def test_request_id_is_hashed_instead_of_used_as_path(self):
        self.assertEqual(self.gateway.draft(self.payload(request_id=".."))["status"],"drafted");self.assertTrue(all(p.name.startswith("request-") for p in (self.state/"imports").iterdir()))

    def test_policy_config_change_invalidates_completed_cache(self):
        self.gateway.draft(self.payload());self.calls.clear();Path(self.cfg["purpose_policy"]).write_text("{}");self.assertEqual(self.gateway.draft(self.payload())["status"],"held");self.assertFalse(self.calls)

    def test_cli_does_not_build_chat_model_or_query_runtime(self):
        path=self.write("input.json",self.payload());out=StringIO()
        with patch("ssr_chat.knowledge_drafts.KnowledgeDraftGateway",return_value=self.gateway),patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("no chat runtime")),redirect_stdout(out):code=main(["draft-knowledge","--config",self.path,"--state-dir",str(self.state),"--input",path])
        self.assertEqual(code,0);body=json.loads(out.getvalue());self.assertEqual(body["draft_count"],2);self.assertFalse(body["model_invoked"]);self.assertFalse(body["network_used"])


if __name__=="__main__":unittest.main()
