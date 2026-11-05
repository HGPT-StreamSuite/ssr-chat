from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess,sys,tempfile,unittest
from unittest.mock import patch

from ssr_chat.activity_counts import ActivityCountsGateway
from ssr_chat.chat_ingest import ChatIngestPipeline,RECORD,encoded,file_sha
from ssr_chat.cli import main
from ssr_chat.modular_api import RetrievalInputError
from ssr_chat.modular_retrieval import PipelineError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/activity_counts/contract.synthetic.json"


class ActivityCountsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.fixture=json.loads(FIXTURE.read_text(encoding="utf-8"));self.calls=[];self.archive_override=None;self.counter_override=None
        for name in ("normalizer/source/hgpt_ssr/__main__.py","store/hgpt_ssr.py","counter/source/hgpt_ssr/__main__.py"):
            path=self.root/name;path.parent.mkdir(parents=True);path.write_text("# Unit stand-in only\n",encoding="utf-8")
        binding=self.fixture["binding"];visibility=self.fixture["records"][0]["record"]["access_scope"];writeonly={"read_scopes":[],"write_scopes":visibility["write_scopes"]}
        writer_context={"schema_version":"1.0.1","adapter_kind":"fixture","source_kind":"synthetic","caller_access_scope":writeonly,"sources":{binding["source_id"]:{"access_scope":visibility,"write_allowed":True,"read_allowed":False,"policy":{"uses":{"local_cache":{"status":"allowed","basis":"unit-synthetic"}}}}}}
        writer_policy={**deepcopy(self.fixture["reader_policy"]),"access_scope":writeonly,"operations":["ingest"]}
        writer={"schema_version":"1.0.1","mode":"synthetic_chat_ingest_only","python":sys._base_executable,"normalizer_repo":str(self.root/"normalizer"),"store_repo":str(self.root/"store"),"normalizer_context":self.write("writer-context.json",writer_context),"store_policy":self.write("writer-policy.json",writer_policy),"state_root":str(self.root/"source-root"),"binding":binding}
        self.writer_path=self.write("writer.json",writer);self.source_state=self.root/"source-root/owned";self.source=ChatIngestPipeline(self.writer_path,self.source_state);self.source_state.mkdir(parents=True)
        self.source.database.write_bytes(b"unit snapshot stand-in; real SQLite integration is separate")
        self.write_owner()
        self.cfg={"schema_version":"1.0.1","mode":"synthetic_activity_counts_only","python":sys._base_executable,"counts_repo":str(self.root/"counter"),"source_pipeline":self.writer_path,"source_state_dir":str(self.source_state),"source_snapshot_sha256":file_sha(self.source.database),
                  **{k:self.write(k+".json",self.fixture[k]) for k in ("reader_policy","counts_context","purpose_policy")},"state_root":str(self.root/"counts-root"),"records":deepcopy(self.fixture["records"])}
        self.path=self.write("counts.json",self.cfg);self.state=self.root/"counts-root/owned";self.gateway=self.build()

    def write(self,name,value):
        path=self.root/name;path.write_bytes(encoded(value));return str(path)

    def write_owner(self):
        (self.source_state/"synthetic-ingest-owner.json").write_bytes(encoded({"schema_version":"1.0.1","mode":"synthetic_chat_ingest_only","binding_sha256":self.source.fingerprint,"store_file_id":self.source._file_identity()}))

    def build(self):self.write("counts.json",self.cfg);return ActivityCountsGateway(self.path,self.state,runner=self.runner)
    def payload(self,**kwargs):return {"request_id":"synthetic-unit-count","synthetic":True,"metrics":["speech"],**kwargs}

    @staticmethod
    def evidence(record):return {**{k:record[k] for k in RECORD-{"schema_version","record_kind","access_scope"}},"source_kind":"synthetic","layer":"raw","match_reasons":["unit"],"rank":None}

    def result(self,request,evidence):return {"schema_version":"1.0.1","request_id":request,"domain":"chat_history","status":"ok","reason_code":"counts_computed","evidence":evidence}

    def runner(self,args,**kwargs):
        archive="--db" in args;self.calls.append({"stage":"archive" if archive else "counter","cwd":kwargs["cwd"]});query=json.loads(Path(args[args.index("--query")+1]).read_bytes())
        if archive:
            value=self.result(query["request_id"],[self.evidence(item["record"]) for item in self.cfg["records"]])
            if self.archive_override:self.archive_override(value)
            code=0 if value["status"] in {"ok","no_match"} else 3
        else:
            self.calls[-1]["view"]=json.loads(Path(args[args.index("--view")+1]).read_bytes());latest={}
            for item in self.cfg["records"]:
                r=item["record"];key=r["source_event_id"]
                if key not in latest or latest[key]["revision"]<r["revision"]:latest[key]=r
            records=list(latest.values());value={"result":self.result(query["request_id"],[self.evidence(r) for r in records[:query["limit"]]]),"statistics":{"counts":{"speech":{"count":len(records),"sources":[]}},"evidence_total":len(records),"evidence_returned":min(len(records),query["limit"]),"evidence_truncated":len(records)>query["limit"]}}
            if self.counter_override:self.counter_override(value)
            code=0 if value["result"]["status"] in {"ok","no_match"} else 2
        return subprocess.CompletedProcess(args,code,stdout=encoded(value),stderr=b"")

    def test_authorized_complete_snapshot_counts_logical_events_and_preserves_original_records(self):
        source_hash=file_sha(self.source.database);body=self.gateway.count(self.payload());self.assertEqual(body["status"],"counted");self.assertEqual(body["message_count"],2)
        self.assertEqual(self.calls[1]["view"]["records"],self.cfg["records"]);self.assertEqual(file_sha(self.source.database),source_hash);self.assertNotEqual(self.calls[0]["cwd"],self.calls[1]["cwd"])

    def test_repeated_read_does_not_accumulate_counts(self):
        for _ in range(2):self.assertEqual(self.gateway.count(self.payload())["message_count"],2)
        self.assertEqual(len(self.calls),4)

    def test_read_grant_is_not_statistics_or_cache_permission(self):
        for purpose in ("activity_counts","local_cache"):
            policy=deepcopy(self.fixture["purpose_policy"]);policy[purpose]["status"]="unknown";self.write("purpose_policy.json",policy);self.gateway=self.build()
            body=self.gateway.count(self.payload());self.assertEqual(body["reason_code"],purpose+"_not_allowed");self.assertIsNone(body["message_count"])
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_source_read_decision_missing_or_denied_prevents_snapshot_read(self):
        for decision in (None,False):
            context=deepcopy(self.fixture["counts_context"])
            if decision is None:context["source_read_decisions"]={}
            else:context["source_read_decisions"][self.fixture["binding"]["source_id"]]["allowed"]=False
            self.write("counts_context.json",context);self.gateway=self.build();self.assertEqual(self.gateway.count(self.payload())["reason_code"],"activity_source_read_not_allowed")
        self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_unsupported_metrics_and_scope_actor_source_overrides_are_rejected(self):
        for metrics in (["question"],["visit"],["speech","question"]):
            with self.assertRaises(RetrievalInputError):self.gateway.count(self.payload(metrics=metrics))
        for key in ("scope","actor_account_id","source_state_dir","source_id","records"):
            value=self.payload();value[key]="SECRET_OVERRIDE"
            with self.assertRaises(RetrievalInputError):self.gateway.count(value)
        self.assertFalse(self.calls)

    def test_question_marks_never_create_question_or_visit_activity_tags(self):
        body=self.gateway.count(self.payload());self.assertEqual(body["message_count"],2)
        self.assertTrue(any("？" in i["record"]["text"] for i in self.calls[1]["view"]["records"]))
        self.assertTrue(all(i["kinds"]==["speech"] for i in self.calls[1]["view"]["records"]))

    def test_archive_denial_is_not_zero_or_a_count(self):
        self.archive_override=lambda v:v.update(status="denied",reason_code="source_read_denied",evidence=[])
        body=self.gateway.count(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["message_count"]);self.assertEqual(len(self.calls),1)

    def test_incomplete_or_changed_archive_evidence_cannot_supply_fabricated_record_acl(self):
        for transform in (lambda v:v["evidence"].pop(),lambda v:v["evidence"][0].update(text="CHANGED_SECRET"),lambda v:v["evidence"][0].update(access_scope={})):
            self.archive_override=transform;body=self.gateway.count(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["message_count"])
        self.assertTrue(all(c["stage"]=="archive" for c in self.calls))

    def test_archive_limit_boundary_fails_closed(self):
        self.archive_override=lambda v:v.update(evidence=v["evidence"]*34)
        self.assertEqual(self.gateway.count(self.payload())["reason_code"],"activity_counts_archive_may_be_truncated");self.assertEqual(len(self.calls),1)

    def test_source_changes_are_not_automatically_adopted(self):
        self.source.database.write_bytes(b"changed owned synthetic source")
        body=self.gateway.count(self.payload());self.assertEqual(body["reason_code"],"activity_counts_source_snapshot_changed");self.assertFalse(self.calls);self.assertFalse(self.state.exists())

    def test_unattested_source_is_not_opened_as_valid_snapshot(self):
        marker=self.source_state/"synthetic-ingest-owner.json";owner=json.loads(marker.read_bytes());owner["binding_sha256"]="0"*64;marker.write_bytes(encoded(owner))
        self.assertEqual(self.gateway.count(self.payload())["reason_code"],"activity_counts_source_not_attested");self.assertFalse(self.calls)

    def test_counter_failure_has_no_partial_number_or_raw_error_leak(self):
        self.counter_override=lambda v:v.update(result={**v["result"],"status":"denied","reason_code":"source_policy_denied","evidence":[]},statistics=None)
        body=self.gateway.count(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["message_count"]);self.assertEqual(body["reason_code"],"source_policy_denied")

    def test_malformed_or_impossible_statistics_are_not_returned(self):
        self.counter_override=lambda v:v["statistics"]["counts"]["speech"].update(count=True)
        self.assertEqual(self.gateway.count(self.payload())["reason_code"],"activity_counts_statistics_invalid")
        self.counter_override=lambda v:v["statistics"]["counts"]["speech"].update(count=999)
        self.assertIsNone(self.gateway.count(self.payload())["message_count"])

    def test_changed_source_during_count_withholds_number(self):
        def mutate(v):self.source.database.write_bytes(b"changed while computing")
        self.counter_override=mutate;body=self.gateway.count(self.payload());self.assertEqual(body["status"],"held");self.assertIsNone(body["message_count"]);self.assertEqual(body["reason_code"],"activity_counts_source_snapshot_changed")

    def test_foreign_cache_or_modified_attested_files_are_not_overwritten(self):
        self.state.mkdir(parents=True);(self.state/"foreign.txt").write_text("keep",encoding="utf-8")
        self.assertEqual(self.gateway.count(self.payload())["reason_code"],"activity_counts_state_not_owned");self.assertEqual((self.state/"foreign.txt").read_text(),"keep")

    def test_cached_result_continuity_is_checked_before_next_invocation(self):
        self.gateway.count(self.payload());path=next((self.state/"imports").glob("*/view.json"));path.write_bytes(b"changed");self.calls.clear()
        self.assertEqual(self.gateway.count(self.payload())["reason_code"],"activity_counts_cache_continuity_failed");self.assertFalse(self.calls)

    def test_summary_and_purpose_receipt_do_not_expose_text_actor_or_filesystem_paths(self):
        body=self.gateway.count(self.payload());combined=json.dumps(body,ensure_ascii=False)+(self.state/"counts-receipts.jsonl").read_text(encoding="utf-8")
        for secret in (self.fixture["binding"]["actor_account_id"],self.fixture["records"][0]["record"]["text"],str(self.source.database)):self.assertNotIn(secret,combined)

    def test_request_id_never_becomes_a_path_component(self):
        body=self.gateway.count(self.payload(request_id=".."));self.assertEqual(body["status"],"counted")
        self.assertFalse((self.state/"view.json").exists());self.assertTrue(all(p.name.startswith("request-") for p in (self.state/"imports").iterdir()))

    def test_write_capabilities_or_person_link_resolution_are_not_enabled(self):
        context=deepcopy(self.fixture["counts_context"]);context["allow_personlinks"]=True;self.write("counts_context.json",context)
        with self.assertRaises(PipelineError):self.build()

    def test_cli_does_not_build_chat_runtime_or_open_network(self):
        path=self.write("input.json",self.payload());output=StringIO()
        with patch("ssr_chat.cli.build_runtime",side_effect=AssertionError("runtime forbidden")),patch("socket.socket.connect",side_effect=AssertionError("network forbidden")),patch("ssr_chat.activity_counts.ActivityCountsGateway.count",return_value={"status":"counted","message_count":2}),redirect_stdout(output):
            self.assertEqual(main(["count-activity","--config",self.path,"--state-dir",str(self.state),"--input",path]),0)
