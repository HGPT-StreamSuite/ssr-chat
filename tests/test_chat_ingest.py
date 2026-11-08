from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ssr_chat.chat_ingest import ChatIngestPipeline
from ssr_chat.cli import main
from ssr_chat.modular_retrieval import PipelineError

FIXTURE=Path(__file__).resolve().parents[1]/"fixtures/chat_storage/contract.synthetic.json"
read=lambda p:json.loads(Path(p).read_text(encoding="utf-8-sig"))


class ChatIngestTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[1]/".test-temp"; parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); fixture=read(FIXTURE)
        self.payload=deepcopy(fixture["input"]); self.binding=fixture["binding"]
        self.context=fixture["normalizer_context"]; self.policy=fixture["store_policy"]
        for relative in ("normalizer/source/hgpt_ssr/__main__.py","store/hgpt_ssr.py"):
            p=self.root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("# Unit-test stand-in; not executed\n",encoding="utf-8")
        self.config={"schema_version":"1.0.1","mode":"synthetic_chat_ingest_only","python":sys._base_executable,
            "normalizer_repo":str(self.root/"normalizer"),"store_repo":str(self.root/"store"),"normalizer_context":str(self.root/"context.json"),
            "store_policy":str(self.root/"policy.json"),"state_root":str(self.root/"sandbox"),"binding":self.binding}
        self.calls=[];self.normalizer_status="ok";self.store_reason="stored";self.store_evidence=[];self.ack_lost=False;self.timeout=False;self.rewrite=False;self.drift=False
        self.save_config();self.pipeline=self.build()

    def save_config(self):
        for name,value in (("pipeline.json",self.config),("context.json",self.context),("policy.json",self.policy)):
            (self.root/name).write_text(json.dumps(value),encoding="utf-8")

    def build(self,state="normal"):
        return ChatIngestPipeline(self.root/"pipeline.json",self.root/"sandbox"/state,runner=self.runner)

    def runner(self,args,**kwargs):
        self.calls.append((args,kwargs))
        request=args[args.index("--request-id")+1]
        result={"schema_version":"1.0.1","request_id":request,"domain":"chat_history","status":"ok","reason_code":None,"evidence":[]}
        if "--emit" in args:
            envelope=read(args[args.index("--input")+1]); result["status"]=self.normalizer_status
            if self.normalizer_status!="ok":result["reason_code"]="source_write_policy_denied"
            if self.rewrite:envelope["record"]["text"]="PRIVATE_REWRITTEN_TEXT"
            output={**envelope,"result":result}
            if self.drift:
                changed=deepcopy(self.policy);changed["operations"]=[]
                (self.root/"policy.json").write_text(json.dumps(changed),encoding="utf-8")
            return SimpleNamespace(returncode=0 if self.normalizer_status=="ok" else 2,stdout=json.dumps(output).encode(),stderr=b"")
        database=Path(args[args.index("--db")+1])
        if not database.exists():database.write_bytes(b"unit-test synthetic store placeholder")
        if self.timeout:raise subprocess.TimeoutExpired(args,20)
        result.update(reason_code=self.store_reason,evidence=self.store_evidence)
        return SimpleNamespace(returncode=0,stdout=b"{bad" if self.ack_lost else json.dumps(result).encode(),stderr=b"")

    def test_two_native_contracts_keep_record_exact_and_minimize_output_audit_and_spool(self):
        response=self.pipeline.ingest(self.payload)
        self.assertEqual(response["counts"],{"stored":1,"duplicate":0,"held":0,"uncertain":0})
        self.assertEqual(len(self.calls),2)
        stored=json.loads(self.calls[1][1]["input"])
        self.assertEqual(stored,self.payload["events"][0]["record"])
        self.assertEqual(stored["text"],self.payload["events"][0]["record"]["text"])
        self.assertEqual(self.calls[0][1]["cwd"],self.config["normalizer_repo"])
        self.assertEqual(self.calls[1][1]["cwd"],self.config["store_repo"])
        self.assertEqual(list((self.pipeline.state/"work").iterdir()),[])
        audit=(self.pipeline.state/"ingest-receipts.jsonl").read_text(encoding="utf-8")
        for text in (json.dumps(response,ensure_ascii=False),audit):
            self.assertNotIn(stored["text"],text);self.assertNotIn(stored["actor_account_id"],text)
        self.assertFalse(response["model_invoked"]);self.assertEqual(response["public_output"],"not_attempted")

    def test_actor_channel_source_and_incoming_acl_cannot_self_authorize(self):
        variants=[]
        for key,value in (("actor_account_id","another-account"),("source_id","another-source"),("access_scope",{"read_scopes":[],"write_scopes":[]})):
            data=deepcopy(self.payload);data["events"][0]["record"][key]=value;variants.append(data)
        data=deepcopy(self.payload);data["events"][0]["record"]["scope"]["channel_id"]="another-channel";variants.append(data)
        for data in variants:self.assertEqual(self.pipeline.ingest(data)["status"],"held")
        self.assertEqual(self.calls,[]);self.assertFalse(self.pipeline.state.exists())

    def test_cache_denied_unknown_or_missing_never_spools_or_writes(self):
        for status in ("denied","unknown",None):
            source=self.context["sources"][self.binding["source_id"]]
            if status is None:source["policy"]["uses"].pop("local_cache",None)
            else:source["policy"]["uses"]["local_cache"]["status"]=status
            self.save_config();pipeline=self.build("cache-"+str(status))
            result=pipeline.ingest(self.payload)
            self.assertEqual(result["items"][0]["reason_code"],"local_cache_not_allowed")
            self.assertFalse(pipeline.state.exists())
        self.assertEqual(self.calls,[])

    def test_unneeded_metadata_source_objects_and_extensions_are_not_retained(self):
        for target,key in (("record","display_name"),("record","extensions"),("envelope","ip_address"),("envelope","scope")):
            data=deepcopy(self.payload)
            (data["events"][0]["record"] if target=="record" else data["events"][0])[key]="UNNEEDED_PRIVATE_MARKER"
            self.assertEqual(self.pipeline.ingest(data)["status"],"held")
        data=deepcopy(self.payload);data["events"][0]["source"]={"display_name":"UNNEEDED_PRIVATE_MARKER","write_allowed":True}
        self.assertEqual(self.pipeline.ingest(data)["status"],"held")
        self.assertEqual(self.calls,[]);self.assertFalse(self.pipeline.state.exists())

    def test_normalizer_denial_is_not_forwarded_to_store(self):
        self.normalizer_status="denied"
        item=self.pipeline.ingest(self.payload)["items"][0]
        self.assertEqual((item["status"],item["stage"],item["reason_code"]),("held","normalizer","source_write_policy_denied"))
        self.assertEqual(len(self.calls),1);self.assertFalse(self.pipeline.database.exists())

    def test_normalizer_cannot_silently_rewrite_record(self):
        self.rewrite=True
        item=self.pipeline.ingest(self.payload)["items"][0]
        self.assertEqual(item["reason_code"],"chat_ingest_normalized_record_changed")
        self.assertEqual(len(self.calls),1);self.assertFalse(self.pipeline.database.exists())

    def test_unexpected_store_readback_is_withheld_after_write(self):
        self.store_evidence=[{"text":"PRIVATE_UNEXPECTED_READBACK"}]
        response=self.pipeline.ingest(self.payload)
        self.assertEqual(response["items"][0]["status"],"uncertain")
        self.assertNotIn("PRIVATE_UNEXPECTED_READBACK",json.dumps(response))
        self.assertTrue(self.pipeline.database.exists())

    def test_ack_loss_has_no_automatic_retry_and_restart_can_confirm_idempotent(self):
        self.ack_lost=True
        result=self.pipeline.ingest(self.payload)
        self.assertEqual(result["items"][0]["status"],"uncertain");self.assertEqual(len(self.calls),2)
        self.ack_lost=False;self.store_reason="idempotent"
        result=self.build().ingest(self.payload)
        self.assertEqual(result["counts"]["duplicate"],1);self.assertEqual(len(self.calls),4)

    def test_partial_batch_keeps_success_and_reports_rejected_item(self):
        data=deepcopy(self.payload);bad=deepcopy(data["events"][0]);bad["record"]["actor_account_id"]="not-bound"
        data["events"].append(bad)
        result=self.pipeline.ingest(data)
        self.assertEqual(result["status"],"partial");self.assertEqual(result["counts"]["stored"],1);self.assertEqual(result["counts"]["held"],1)
        self.assertEqual(len(self.calls),2)

    def test_store_timeout_is_uncertain_and_never_auto_retried(self):
        self.timeout=True
        result=self.pipeline.ingest(self.payload)
        self.assertEqual(result["items"][0]["status"],"uncertain");self.assertEqual(len(self.calls),2)

    def test_existing_unowned_state_is_not_read_or_overwritten(self):
        self.pipeline.state.mkdir(parents=True);foreign=self.pipeline.state/"private.json";foreign.write_bytes(b"synthetic do-not-touch marker")
        result=self.pipeline.ingest(self.payload)
        self.assertEqual(result["items"][0]["reason_code"],"chat_ingest_state_not_owned")
        self.assertEqual(foreign.read_bytes(),b"synthetic do-not-touch marker");self.assertEqual(self.calls,[])

    def test_state_outside_explicit_root_is_rejected_before_creation(self):
        target=self.root/"outside"
        with self.assertRaises(PipelineError):ChatIngestPipeline(self.root/"pipeline.json",target,runner=self.runner)
        self.assertFalse(target.exists())

    def test_configuration_change_between_steps_stops_before_store(self):
        self.drift=True
        item=self.pipeline.ingest(self.payload)["items"][0]
        self.assertEqual(item["reason_code"],"chat_ingest_configuration_changed")
        self.assertEqual(len(self.calls),1);self.assertFalse(self.pipeline.database.exists())

    def test_missing_previously_owned_store_is_not_recreated_on_restart(self):
        self.pipeline.ingest(self.payload)
        source=self.pipeline.database;target=source.with_name("saved-synthetic-chat.sqlite3")
        self.assertEqual(source.resolve().parent,target.resolve().parent);self.assertTrue(source.resolve().is_relative_to(self.root.resolve()))
        source.rename(target);self.calls.clear()
        item=self.build().ingest(self.payload)["items"][0]
        self.assertEqual(item["reason_code"],"chat_ingest_store_continuity_failed")
        self.assertFalse(source.exists());self.assertTrue(target.exists());self.assertEqual(self.calls,[])

    def test_empty_batch_is_explicit_and_has_no_io(self):
        data=deepcopy(self.payload);data["events"]=[]
        result=self.pipeline.ingest(data)
        self.assertEqual(result["status"],"empty");self.assertEqual(sum(result["counts"].values()),0)
        self.assertEqual(self.calls,[]);self.assertFalse(self.pipeline.state.exists())

    def test_reparse_work_path_is_rejected_before_spooling_or_native_calls(self):
        original=Path.is_symlink
        def symlink(path):return path==self.pipeline.state/"work" or original(path)
        with patch.object(Path,"is_symlink",symlink):
            item=self.pipeline.ingest(self.payload)["items"][0]
        self.assertEqual(item["reason_code"],"chat_ingest_state_path_invalid")
        self.assertEqual(self.calls,[]);self.assertFalse(self.pipeline.state.exists())

    def test_cli_strict_input_has_no_legacy_runtime_or_model_fallback(self):
        input_path=self.root/"bad.json";input_path.write_bytes(b'{"request_id":"a","request_id":"b"}')
        output=StringIO()
        with redirect_stdout(output),patch("ssr_chat.cli.build_runtime") as runtime:
            code=main(["ingest-chat","--config",str(self.root/"pipeline.json"),"--state-dir",str(self.pipeline.state),"--input",str(input_path)])
        self.assertEqual(code,2);runtime.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["status"],"held");self.assertFalse(self.pipeline.state.exists())


if __name__=="__main__":unittest.main()
