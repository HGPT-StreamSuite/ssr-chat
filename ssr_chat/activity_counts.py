"""Pinned synthetic chat archive -> native authorized export -> native speech counts."""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
from stat import S_ISREG
import subprocess
from uuid import uuid4

from .chat_ingest import ChatIngestPipeline,RECORD,SCOPE,FLAGS,access,digest,encoded,file_sha,token,local
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

STATUSES={"ok","no_match","skipped","error","missing_input","denied","not_configured"}
EVIDENCE=(RECORD-{"schema_version","record_kind","access_scope"})|{"layer","source_kind","match_reasons","rank"}


class ActivityCountsGateway:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        fields={"schema_version","mode","python","counts_repo","source_pipeline","source_state_dir","source_snapshot_sha256","reader_policy","counts_context","purpose_policy","state_root","records"}
        if set(cfg)!=fields or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_activity_counts_only":raise PipelineError("activity_counts_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.repo=local(cfg["counts_repo"],path.parent);root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("activity_counts_state_outside_dedicated_root")
        self.state=state.resolve();self.source=ChatIngestPipeline(local(cfg["source_pipeline"],path.parent),local(cfg["source_state_dir"],path.parent))
        self.binding=deepcopy(self.source.binding);scope=self.binding["scope"];source=self.binding["source_id"]
        if (self.python!=self.source.python or not source.startswith("synthetic-") or not self.binding["actor_account_id"].startswith("synthetic-")
                or any(not scope[k].startswith("synthetic-") for k in ("platform","channel_id","session_id")) or not (self.repo/"source/hgpt_ssr/__main__.py").is_file()):raise PipelineError("activity_counts_synthetic_source_required")
        self.snapshot=cfg["source_snapshot_sha256"]
        if not isinstance(self.snapshot,str) or re.fullmatch(r"[0-9a-f]{64}",self.snapshot) is None:raise PipelineError("activity_counts_snapshot_pin_required")
        paths={k:local(cfg[k],path.parent) for k in ("reader_policy","counts_context","purpose_policy")}
        self.reader=load_object(paths["reader_policy"].read_bytes());self.context=load_object(paths["counts_context"].read_bytes());self.purpose=load_object(paths["purpose_policy"].read_bytes())
        selector={"domain":"chat_history",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        if (set(self.reader)!={"access_scope","operations","sources","source_access_scopes"} or not access(self.reader["access_scope"],selector) or self.reader["access_scope"]["write_scopes"]
                or self.reader["operations"]!=["export"] or self.reader["sources"]!={source:"synthetic"} or not isinstance(self.reader["source_access_scopes"],dict)
                or set(self.reader["source_access_scopes"])!={source} or not access(self.reader["source_access_scopes"][source],selector)):raise PipelineError("activity_counts_reader_authority_invalid")
        context=self.context
        if (set(context)!={"principal_id","access_scope","layers","allow_personlinks","source_read_decisions"} or not token(context["principal_id"]) or not context["principal_id"].startswith("synthetic-")
                or not access(context["access_scope"],selector) or context["access_scope"]["write_scopes"] or context["layers"] not in ([],["raw"]) or context["allow_personlinks"] is not False
                or not isinstance(context["source_read_decisions"],dict) or set(context["source_read_decisions"])-{source}):raise PipelineError("activity_counts_context_invalid")
        decision=context["source_read_decisions"].get(source)
        if decision is not None and (not isinstance(decision,dict) or set(decision)!={"allowed","basis"} or type(decision["allowed"]) is not bool or not isinstance(decision["basis"],str) or not decision["basis"].strip()):raise PipelineError("activity_counts_source_decision_invalid")
        if (set(self.purpose)!={"source_id","scope","local_cache","activity_counts"} or self.purpose["source_id"]!=source or self.purpose["scope"]!=scope):raise PipelineError("activity_counts_purpose_binding_invalid")
        self.records=deepcopy(cfg["records"]);self.roster={}
        if not isinstance(self.records,list) or not 1<=len(self.records)<=50:raise PipelineError("activity_counts_registered_roster_required")
        for item in self.records:
            if not isinstance(item,dict) or set(item)!={"record","kinds"} or item["kinds"]!=["speech"]:raise PipelineError("activity_counts_explicit_speech_marks_required")
            record=item["record"]
            envelope={"record":record,"synthetic":True,"source":source,"raw_reference":record.get("source_refs",[{}])[0].get("locator") if isinstance(record,dict) and record.get("source_refs") else None}
            if self.source._input(envelope) or not record["source_event_id"].startswith("synthetic-") or not record["record_id"].startswith("synthetic-"):raise PipelineError("activity_counts_roster_not_source_bound")
            key=self._key(record)
            if key in self.roster:raise PipelineError("activity_counts_roster_revision_duplicate")
            self.roster[key]=record
        self.code_files=self._code_files();self.watched={**self.source.watched,**{str(p):file_sha(p) for p in [path,self.python,*paths.values(),*map(Path,self.code_files)]}}
        self.fingerprint=digest({"config":cfg,"files":self.watched});self.runner=runner or subprocess.run

    @staticmethod
    def _key(record):return tuple(record[k] for k in ("source_id","source_event_id","record_id","revision"))

    def _code_files(self):return {str(p) for p in (self.repo/"source").rglob("*") if p.is_file() and p.suffix in {".py",".json"}}

    def _unchanged(self):
        self.source._unchanged()
        if self.code_files!=self._code_files() or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("activity_counts_configuration_changed")

    def _source_ready(self):
        marker=self.source.state/"synthetic-ingest-owner.json";db=self.source.database
        if marker.is_symlink() or not marker.is_file() or not db.is_file():raise PipelineError("activity_counts_source_not_collected")
        owner=load_object(marker.read_bytes())
        if (set(owner)!={"schema_version","mode","binding_sha256","store_file_id"} or owner["schema_version"]!="1.0.1" or owner["mode"]!="synthetic_chat_ingest_only"
                or owner["binding_sha256"]!=self.source.fingerprint or owner["store_file_id"]!=self.source._file_identity()):raise PipelineError("activity_counts_source_not_attested")
        if db.stat().st_size>8_000_000 or any(db.parent.glob(db.name+"-*")) or file_sha(db)!=self.snapshot:raise PipelineError("activity_counts_source_snapshot_changed")

    def _files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("activity_counts_state_path_invalid")
            for name in files:
                path=Path(folder)/name;info=path.lstat()
                if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("activity_counts_state_path_invalid")
                result[str(path.relative_to(self.state))]={"identity":[str(info.st_dev),str(info.st_ino)],"sha256":file_sha(path)}
        return result

    @contextmanager
    def _owned(self):
        marker=self.state/"synthetic-counts-owner.json"
        for name in ("synthetic-counts-owner.json","pipeline.lock","counts-receipts.jsonl","imports"):
            selected=self.state/name
            if selected.is_symlink() or getattr(selected,"is_junction",lambda:False)() or selected.exists() and name!="imports" and not S_ISREG(selected.lstat().st_mode):raise PipelineError("activity_counts_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("activity_counts_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file:file.write(encoded({"mode":"synthetic_activity_counts_only","binding_sha256":self.fingerprint,"files":{}}))
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
            except OSError:raise PipelineError("activity_counts_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","binding_sha256","files"} or owner["mode"]!="synthetic_activity_counts_only" or owner["binding_sha256"]!=self.fingerprint:raise PipelineError("activity_counts_state_binding_mismatch")
                if self._files()!=owner["files"]:raise PipelineError("activity_counts_cache_continuity_failed")
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
        if (not isinstance(value,dict) or value.get("schema_version")!="1.0.1" or value.get("request_id")!=request_id or value.get("domain")!="chat_history"
                or value.get("status") not in STATUSES or not isinstance(value.get("evidence"),list)):raise PipelineError("activity_counts_native_result_invalid")
        reason=value.get("reason_code")
        if reason is not None and (not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None):raise PipelineError("activity_counts_native_result_invalid")
        return value["status"],reason

    def count(self,payload):
        fields={"request_id","synthetic","metrics"};optional={"time_from","time_to","exclude_event_ids","limit"}
        if (not isinstance(payload,dict) or not fields<=set(payload) or set(payload)-fields-optional or not token(payload["request_id"]) or payload["synthetic"] is not True
                or payload["metrics"]!=["speech"] or type(payload.get("limit",1)) is not int or not 1<=payload.get("limit",1)<=100):raise RetrievalInputError("synthetic_speech_count_request_invalid")
        output={"request_id":payload["request_id"],"status":"held","mode":"synthetic_activity_counts_only","stage":"permission","message_count":None,"model_invoked":False,"network_used":False,"public_output":"not_attempted","reason_code":None}
        for name in ("local_cache","activity_counts"):
            grant=self.purpose[name]
            if not isinstance(grant,dict) or grant.get("status")!="allowed" or not isinstance(grant.get("basis"),str) or not grant["basis"].strip():output["reason_code"]=name+"_not_allowed";return output
        source=self.binding["source_id"];decision=self.context["source_read_decisions"].get(source,{})
        if (decision.get("allowed") is not True or not self.context["access_scope"]["read_scopes"] or self.context["layers"]!=["raw"]
                or not self.reader["access_scope"]["read_scopes"] or not self.reader["source_access_scopes"][source]["read_scopes"]
                or any(not r["access_scope"]["read_scopes"] for r in self.roster.values())):output["reason_code"]="activity_source_read_not_allowed";return output
        try:
            self._unchanged();self._source_ready()
            with self._owned():
                job=self.state/"imports"/("request-"+digest(payload)[:32]);job.mkdir(parents=True,exist_ok=True)
                read_query={"schema_version":"1.0.1","request_id":payload["request_id"]+"-source","text":"synthetic complete raw snapshot export","intent":"export","scope":self.binding["scope"],"actor_account_id":None,"person_id":None,"time_from":None,"time_to":None,"exclude_event_ids":[],"limit":100,"layer":"raw","conditions":{},"access_scope":self.reader["access_scope"]}
                for name,value in (("reader-policy.json",self.reader),("source-query.json",read_query)):(job/name).write_bytes(encoded(value))
                output["stage"]="archive"
                child=self._call([str(self.python),"-B","-X","utf8","hgpt_ssr.py","--db",str(self.source.database),"--policy",str(job/"reader-policy.json"),"export","--query",str(job/"source-query.json")],self.source.store)
                if len(child.stdout)>2_000_000:raise PipelineError("activity_counts_native_output_oversized")
                value=load_object(child.stdout);status,reason=self._result(value,read_query["request_id"]);output.update(archive_status=status,archive_exit=child.returncode)
                if child.returncode!=0 or status not in {"ok","no_match"}:output["reason_code"]=reason or "activity_archive_not_authorized";return output
                evidence=value["evidence"]
                if len(evidence)>=100:raise PipelineError("activity_counts_archive_may_be_truncated")
                seen=set()
                for item in evidence:
                    if not isinstance(item,dict) or set(item)!=EVIDENCE:raise PipelineError("activity_counts_archive_evidence_invalid")
                    key=self._key(item);record=self.roster.get(key)
                    if (record is None or key in seen or item["source_kind"]!="synthetic" or item["layer"]!="raw" or any(item[k]!=record[k] for k in RECORD-{"schema_version","record_kind","access_scope"})):raise PipelineError("activity_counts_archive_not_registered_original")
                    seen.add(key)
                if seen!=set(self.roster):raise PipelineError("activity_counts_snapshot_incomplete")
                self._source_ready();(job/"source-result.json").write_bytes(encoded(value))
                view={"schema_version":"1.0.1","synthetic":True,"sources":[{"source_id":source,"source_kind":"synthetic","scopes":[self.binding["scope"]],"layers":["raw"],"collected":True}],"records":self.records}
                query={**read_query,"request_id":payload["request_id"],"text":"synthetic explicitly marked speech event count","intent":"count","actor_account_id":self.binding["actor_account_id"],"time_from":payload.get("time_from"),"time_to":payload.get("time_to"),"exclude_event_ids":payload.get("exclude_event_ids",[]),"limit":payload.get("limit",1),"conditions":{"metrics":["speech"]},"access_scope":self.context["access_scope"]}
                for name,body in (("view.json",view),("query.json",query),("context.json",self.context)):(job/name).write_bytes(encoded(body))
                output["stage"]="counter"
                child=self._call([str(self.python),"-B","-X","utf8","-m","source.hgpt_ssr","--view",str(job/"view.json"),"--query",str(job/"query.json"),"--context",str(job/"context.json")],self.repo)
                if len(child.stdout)>2_000_000:raise PipelineError("activity_counts_native_output_oversized")
                response=load_object(child.stdout)
                if set(response)!={"result","statistics"}:raise PipelineError("activity_counts_envelope_invalid")
                status,reason=self._result(response["result"],payload["request_id"]);output.update(counter_status=status,counter_exit=child.returncode)
                self._unchanged();self._source_ready();(job/"counts-result.json").write_bytes(encoded(response))
                if child.returncode!=0 or status not in {"ok","no_match"}:output["reason_code"]=reason or "activity_counts_not_authorized";return output
                statistics=response["statistics"]
                if (not isinstance(statistics,dict) or not isinstance(statistics.get("counts"),dict) or set(statistics["counts"])!={"speech"}
                        or type(statistics["counts"]["speech"].get("count")) is not int or not 0<=statistics["counts"]["speech"]["count"]<=len(self.roster)):raise PipelineError("activity_counts_statistics_invalid")
                output.update(status="counted",message_count=statistics["counts"]["speech"]["count"],reason_code=reason,source_snapshot_sha256=self.snapshot,provenance_sha256=digest(response),snapshot_records=len(self.roster))
            with self._owned():JsonlLog(self.state/"counts-receipts.jsonl").append({**output,"binding_sha256":self.fingerprint,"purpose_sha256":digest(self.purpose),"ai_input":"not_used","ai_train":"not_used"})
            return output
        except (PipelineError,OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError) as error:
            output.update(status="held",message_count=None,reason_code=getattr(error,"code","activity_counts_local_operation_failed"));return output
