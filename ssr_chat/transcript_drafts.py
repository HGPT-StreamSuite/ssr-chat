"""Owned synthetic STT snapshot -> authorized native history -> separate cleanup drafts."""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
from stat import S_ISREG
import subprocess
from uuid import uuid4

from .chat_ingest import SCOPE,FLAGS,access,digest,encoded,file_sha,token,local
from .stt_ingest import SttIngestPipeline,FIELDS
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

STATUSES={"ok","no_match","skipped","error","missing_input","denied","not_configured"}
EVIDENCE=(FIELDS-{"schema_version","record_kind","access_scope"})|{"layer","source_kind","match_reasons","rank"}


class TranscriptDraftGateway:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        fields={"schema_version","mode","python","draft_repo","source_pipeline","source_state_dir","source_snapshot_sha256","reader_sources","reader_caller","draft_context","purpose_policy","hook","state_root","records"}
        if set(cfg)!=fields or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_transcript_drafts_only" or cfg["hook"] not in (None,"whitespace-v1"):raise PipelineError("transcript_drafts_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.repo=local(cfg["draft_repo"],path.parent);root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("transcript_drafts_state_outside_dedicated_root")
        self.state=state.resolve();self.source=SttIngestPipeline(local(cfg["source_pipeline"],path.parent),local(cfg["source_state_dir"],path.parent));self.binding=deepcopy(self.source.binding);scope=self.binding["scope"];source=self.binding["source_id"]
        if self.python!=self.source.python or not (self.repo/"source/hgpt_ssr/__main__.py").is_file():raise PipelineError("transcript_drafts_component_missing")
        self.snapshot=cfg["source_snapshot_sha256"];self.hook=cfg["hook"]
        if not isinstance(self.snapshot,str) or re.fullmatch(r"[0-9a-f]{64}",self.snapshot) is None:raise PipelineError("transcript_drafts_snapshot_pin_required")
        paths={k:local(cfg[k],path.parent) for k in ("reader_sources","reader_caller","draft_context","purpose_policy")}
        self.reader=load_object(paths["reader_sources"].read_bytes());self.caller=load_object(paths["reader_caller"].read_bytes());self.context=load_object(paths["draft_context"].read_bytes());self.purpose=load_object(paths["purpose_policy"].read_bytes())
        selector={"domain":"stt_transcript",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        if (set(self.reader)!={"sources"} or not isinstance(self.reader["sources"],list) or len(self.reader["sources"])!=1 or not access(self.caller,selector) or self.caller["write_scopes"]):raise PipelineError("transcript_drafts_reader_invalid")
        current=self.reader["sources"][0]
        if (not isinstance(current,dict) or set(current)!={"source_id","source_kind","scopes","operations","policy_ref","access_scope"}
                or current["source_id"]!=source or current["source_kind"]!="synthetic" or current["scopes"]!=[scope] or current["operations"] not in ([],["read"])
                or not access(current["access_scope"],selector) or current["access_scope"]["write_scopes"] or not isinstance(current["policy_ref"],str) or not current["policy_ref"].startswith("synthetic:")):raise PipelineError("transcript_drafts_source_registry_invalid")
        if set(self.context)!={"scope","access_scope"} or self.context["scope"]!=scope or not access(self.context["access_scope"],selector):raise PipelineError("transcript_drafts_context_invalid")
        if set(self.purpose)!={"source_id","scope","source_read_decision","local_cache","transcript_cleanup"} or self.purpose["source_id"]!=source or self.purpose["scope"]!=scope:raise PipelineError("transcript_drafts_purpose_binding_invalid")
        decision=self.purpose["source_read_decision"]
        if decision is not None and (not isinstance(decision,dict) or set(decision)!={"allowed","basis"} or type(decision["allowed"]) is not bool or not isinstance(decision["basis"],str) or not decision["basis"].strip()):raise PipelineError("transcript_drafts_read_decision_invalid")
        self.records=deepcopy(cfg["records"]);self.roster={}
        if not isinstance(self.records,list) or not 1<=len(self.records)<=50:raise PipelineError("transcript_drafts_registered_roster_required")
        for record in self.records:
            if self.source._input(record) or self.source._normalized(record)!=record:raise PipelineError("transcript_drafts_original_not_source_bound")
            key=self._key(record)
            if key in self.roster:raise PipelineError("transcript_drafts_roster_duplicate")
            self.roster[key]=record
        self.code_files=self._code_files();watched=[path,self.python,*paths.values(),*map(Path,self.code_files)]
        if any(p.is_symlink() for p in watched):raise PipelineError("transcript_drafts_symlink_not_allowed")
        self.watched={**self.source.watched,**{str(p):file_sha(p) for p in watched}};self.fingerprint=digest({"config":cfg,"files":self.watched});self.runner=runner or subprocess.run

    @staticmethod
    def _key(record):return tuple(record[k] for k in ("source_id","source_event_id","record_id","revision"))

    def _code_files(self):return {str(p) for p in (self.repo/"source").rglob("*") if p.is_file() and p.suffix in {".py",".json"}}

    def _unchanged(self):
        self.source._unchanged()
        if self.code_files!=self._code_files() or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("transcript_drafts_configuration_changed")

    def _source_ready(self):
        marker=self.source.state/"synthetic-stt-owner.json";db=self.source.database
        if marker.is_symlink() or not marker.is_file() or not db.is_file():raise PipelineError("transcript_drafts_source_not_collected")
        owner=load_object(marker.read_bytes())
        if (set(owner)!={"schema_version","mode","binding_sha256","store_file_id"} or owner["schema_version"]!="1.0.1" or owner["mode"]!="synthetic_stt_ingest_only"
                or owner["binding_sha256"]!=self.source.fingerprint or owner["store_file_id"]!=self.source._file_identity()):raise PipelineError("transcript_drafts_source_not_attested")
        if db.stat().st_size>8_000_000 or any(db.parent.glob(db.name+"-*")) or file_sha(db)!=self.snapshot:raise PipelineError("transcript_drafts_source_snapshot_changed")

    def _files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("transcript_drafts_state_path_invalid")
            for name in files:
                path=Path(folder)/name;info=path.lstat()
                if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("transcript_drafts_state_path_invalid")
                result[str(path.relative_to(self.state))]={"identity":[str(info.st_dev),str(info.st_ino)],"sha256":file_sha(path)}
        return result

    @contextmanager
    def _owned(self):
        marker=self.state/"synthetic-drafts-owner.json"
        for name in ("synthetic-drafts-owner.json","pipeline.lock","draft-receipts.jsonl","imports"):
            selected=self.state/name
            if selected.is_symlink() or getattr(selected,"is_junction",lambda:False)() or selected.exists() and name!="imports" and not S_ISREG(selected.lstat().st_mode):raise PipelineError("transcript_drafts_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("transcript_drafts_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file:file.write(encoded({"mode":"synthetic_transcript_drafts_only","binding_sha256":self.fingerprint,"files":{}}))
            except FileExistsError:pass
        with (self.state/"pipeline.lock").open("a+b") as lock:
            if lock.tell()==0:lock.write(b"0");lock.flush()
            lock.seek(0)
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise PipelineError("transcript_drafts_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","binding_sha256","files"} or owner["mode"]!="synthetic_transcript_drafts_only" or owner["binding_sha256"]!=self.fingerprint:raise PipelineError("transcript_drafts_state_binding_mismatch")
                if self._files()!=owner["files"]:raise PipelineError("transcript_drafts_cache_continuity_failed")
                try:yield
                finally:
                    owner["files"]=self._files();temp=self.state/("owner-"+uuid4().hex+".tmp");temp.write_bytes(encoded(owner));os.replace(temp,marker)
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _call(self,args,cwd):return self.runner(args,cwd=str(cwd),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)

    @staticmethod
    def _result(value,request_id):
        if (not isinstance(value,dict) or value.get("schema_version")!="1.0.1" or value.get("request_id")!=request_id or value.get("domain")!="stt_transcript"
                or value.get("status") not in STATUSES or not isinstance(value.get("evidence"),list)):raise PipelineError("transcript_drafts_native_result_invalid")
        reason=value.get("reason_code")
        if reason is not None and (not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None):raise PipelineError("transcript_drafts_native_result_invalid")
        return value["status"],reason

    def _outputs(self,job,request_id):
        outputs=[]
        for name in ("draft-result.json","draft-trace.json"):
            path=job/name
            if path.is_symlink() or not S_ISREG(path.lstat().st_mode) or path.stat().st_size>2_000_000:raise PipelineError("transcript_drafts_output_invalid")
            outputs.append(load_object(path.read_bytes()))
        result,trace=outputs;self._result(result,request_id)
        if trace.get("input_hash")!=file_sha(job/"originals.json") or type(trace.get("draft_count")) is not int or trace["draft_count"]!=len(result["evidence"]):raise PipelineError("transcript_drafts_trace_invalid")
        if result["status"]!="ok" and result["evidence"]:raise PipelineError("transcript_drafts_partial_output_invalid")
        selected=set()
        for item in result["evidence"]:
            if not isinstance(item,dict) or set(item)!=EVIDENCE:raise PipelineError("transcript_drafts_evidence_invalid")
            raw=next((r for r in self.records if r["source_event_id"]==item["source_event_id"] and r["revision"]==item["revision"]),None)
            if (raw is None or (item["source_event_id"],item["revision"]) in selected or item["layer"]!="draft" or item["source_kind"]!="synthetic"
                    or not item["source_id"].startswith("hgpt-ssr-draft/") or not item["record_id"].startswith("draft-")
                    or any(item[k]!=raw[k] for k in ("event_time","received_at","actor_account_id","scope","extensions"))
                    or not set(raw["quality_flags"])<=set(item["quality_flags"]) or not isinstance(item["text"],str)
                    or re.sub(r"\s","",item["text"])!=re.sub(r"\s","",raw["text"])):raise PipelineError("transcript_drafts_original_metadata_changed")
            refs=item["source_refs"];index=self.records.index(raw)
            if (not isinstance(refs,list) or len(refs)!=len(raw["source_refs"])+1 or refs[:-1]!=raw["source_refs"]
                    or refs[-1]!={"source_id":raw["source_id"],"record_id":raw["record_id"],"revision":raw["revision"],"locator":"sha256:"+trace["input_hash"]+"#/"+str(index),"transformation_version":trace.get("transformation_version")}):raise PipelineError("transcript_drafts_provenance_changed")
            selected.add((item["source_event_id"],item["revision"]))
        return result,trace

    def draft(self,payload):
        fields={"request_id","synthetic"};optional={"revision_view","time_from","time_to","exclude_event_ids","limit"}
        if (not isinstance(payload,dict) or not fields<=set(payload) or set(payload)-fields-optional or not token(payload["request_id"]) or payload["synthetic"] is not True
                or payload.get("revision_view","current") not in ("current","history") or type(payload.get("limit",100)) is not int or not 1<=payload.get("limit",100)<=100):raise RetrievalInputError("synthetic_transcript_draft_request_invalid")
        output={"request_id":payload["request_id"],"status":"held","mode":"synthetic_transcript_drafts_only","stage":"permission","draft_count":None,"model_invoked":False,"network_used":False,"audio_used":False,"public_output":"not_attempted","reason_code":None}
        for name in ("local_cache","transcript_cleanup"):
            grant=self.purpose[name]
            if not isinstance(grant,dict) or grant.get("status")!="allowed" or not isinstance(grant.get("basis"),str) or not grant["basis"].strip():output["reason_code"]=name+"_not_allowed";return output
        decision=self.purpose["source_read_decision"] or {};current=self.reader["sources"][0]
        if (decision.get("allowed") is not True or not self.caller["read_scopes"] or not current["access_scope"]["read_scopes"] or current["operations"]!=["read"]
                or not self.context["access_scope"]["read_scopes"]):output["reason_code"]="transcript_source_read_not_allowed";return output
        try:
            self._unchanged();self._source_ready()
            with self._owned():
                job=self.state/"imports"/("request-"+digest(payload)[:32]);job.mkdir(parents=True,exist_ok=True)
                query={"schema_version":"1.0.1","request_id":payload["request_id"]+"-source","text":"synthetic complete raw transcript history","intent":"query","scope":self.binding["scope"],"actor_account_id":None,"person_id":None,"time_from":None,"time_to":None,"exclude_event_ids":[],"limit":100,"layer":"raw","conditions":{"revision_view":"history"},"access_scope":self.caller}
                for name,value in (("reader-sources.json",self.reader),("reader-caller.json",self.caller),("source-query.json",query)):(job/name).write_bytes(encoded(value))
                output["stage"]="archive"
                child=self._call([str(self.python),"-B","-X","utf8","-m","hgpt_ssr","--db",str(self.source.database),"--sources",str(job/"reader-sources.json"),"--caller-access",str(job/"reader-caller.json"),"get","--input",str(job/"source-query.json")],self.source.store)
                if len(child.stdout)>2_000_000:raise PipelineError("transcript_drafts_native_output_oversized")
                archive=load_object(child.stdout);status,reason=self._result(archive,query["request_id"]);output.update(archive_status=status,archive_exit=child.returncode)
                if child.returncode!=0 or status not in {"ok","no_match"}:output["reason_code"]=reason or "transcript_archive_not_authorized";return output
                if len(archive["evidence"])>=100:raise PipelineError("transcript_drafts_archive_may_be_truncated")
                seen=set()
                for item in archive["evidence"]:
                    if not isinstance(item,dict) or set(item)!=EVIDENCE:raise PipelineError("transcript_drafts_archive_evidence_invalid")
                    key=self._key(item);record=self.roster.get(key)
                    if record is None or key in seen or item["source_kind"]!="synthetic" or item["layer"]!="raw" or any(item[k]!=record[k] for k in FIELDS-{"schema_version","record_kind","access_scope"}):raise PipelineError("transcript_drafts_archive_not_registered_original")
                    seen.add(key)
                if seen!=set(self.roster):raise PipelineError("transcript_drafts_snapshot_incomplete")
                self._source_ready();(job/"source-result.json").write_bytes(encoded(archive))
                draft_query={**query,"request_id":payload["request_id"],"text":"synthetic whitespace-only transcript cleanup","intent":"cleanup","actor_account_id":self.binding["actor_account_id"],"conditions":{"revision_view":payload.get("revision_view","current")},"time_from":payload.get("time_from"),"time_to":payload.get("time_to"),"exclude_event_ids":payload.get("exclude_event_ids",[]),"limit":payload.get("limit",100),"access_scope":self.context["access_scope"]}
                for name,value in (("originals.json",self.records),("draft-query.json",draft_query),("draft-context.json",self.context),("source-kinds.json",{self.binding["source_id"]:"synthetic"})):(job/name).write_bytes(encoded(value))
                args=[str(self.python),"-B","-X","utf8","-m","source.hgpt_ssr","--input",str(job/"originals.json"),"--query",str(job/"draft-query.json"),"--trusted-context",str(job/"draft-context.json"),"--source-kinds",str(job/"source-kinds.json"),"--kind","cleanup","--output",str(job/"draft-result.json"),"--trace-output",str(job/"draft-trace.json")]
                if self.hook:args.extend(["--hook",self.hook])
                output["stage"]="draft";child=self._call(args,self.repo)
                if len(child.stdout)>100_000:raise PipelineError("transcript_drafts_native_output_oversized")
                ack=load_object(child.stdout);result,trace=self._outputs(job,payload["request_id"]);status,reason=self._result(result,payload["request_id"])
                if set(ack)!={"status","reason_code"} or ack["status"]!=status or ack["reason_code"]!=reason:raise PipelineError("transcript_drafts_ack_not_confirmed")
                output.update(draft_status=status,draft_exit=child.returncode,reason_code=reason);self._unchanged();self._source_ready()
                if child.returncode!=0 or status not in {"ok","no_match"}:return output
                output.update(status="drafted" if status=="ok" else "no_match",draft_count=len(result["evidence"]),source_snapshot_sha256=self.snapshot,result_sha256=file_sha(job/"draft-result.json"),trace_sha256=file_sha(job/"draft-trace.json"),snapshot_records=len(self.roster))
            with self._owned():JsonlLog(self.state/"draft-receipts.jsonl").append({**output,"binding_sha256":self.fingerprint,"purpose_sha256":digest(self.purpose),"ai_input":"not_used","ai_train":"not_used"})
            return output
        except (PipelineError,OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError) as error:
            output.update(status="held",draft_count=None,reason_code=getattr(error,"code","transcript_drafts_local_operation_failed"));return output
