"""Registered synthetic channel exports -> task-19 Evidence -> original task-4 Record."""
from contextlib import closing,contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
from stat import S_ISREG
import sqlite3
import subprocess
from uuid import uuid4

from .chat_ingest import SCOPE,RECORD,FLAGS,access,digest,encoded,file_sha,token,local
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

STATUSES={"ok","no_match","skipped","error","missing_input","denied","not_configured"}
EVIDENCE=(RECORD-{"schema_version","record_kind","access_scope"})|{"layer","source_kind","match_reasons","rank"}
TABLES={"configuration","records","withdrawals"}


class ChannelIngestPipeline:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        fields={"schema_version","mode","python","importer_repo","store_repo","importer_config","store_context","purpose_policy","state_root","binding","exports"}
        if set(cfg)!=fields or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_channel_ingest_only":raise PipelineError("channel_ingest_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.importer=local(cfg["importer_repo"],path.parent);self.store=local(cfg["store_repo"],path.parent)
        root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("channel_ingest_state_outside_dedicated_root")
        self.state=state.resolve();self.database=self.state/"synthetic-channel.sqlite3"
        if not self.python.is_file() or not (self.importer/"source/hgpt_ssr.py").is_file() or not (self.store/"source/ssr.py").is_file():raise PipelineError("channel_ingest_component_missing")
        paths={k:local(cfg[k],path.parent) for k in ("importer_config","store_context","purpose_policy")}
        self.import_config=load_object(paths["importer_config"].read_bytes());self.context=load_object(paths["store_context"].read_bytes());self.purpose=load_object(paths["purpose_policy"].read_bytes())
        self.binding=deepcopy(cfg["binding"]);self.exports=deepcopy(cfg["exports"]);binding=self.binding
        if (not isinstance(binding,dict) or set(binding)!={"source_id","actor_account_id","scope"} or not token(binding["source_id"]) or not binding["source_id"].startswith("synthetic-")
                or binding["actor_account_id"] is not None and (not token(binding["actor_account_id"]) or not binding["actor_account_id"].startswith("synthetic-"))):raise PipelineError("channel_ingest_binding_invalid")
        scope=binding["scope"]
        if (not isinstance(scope,dict) or set(scope)!=set(SCOPE) or scope["domain"]!="channel_knowledge"
                or any(not isinstance(scope[k],str) or not scope[k].startswith("synthetic-") for k in ("platform","channel_id"))
                or any(scope[k] is not None and (not isinstance(scope[k],str) or not scope[k]) for k in ("game_id","session_id","content_version"))):raise PipelineError("channel_ingest_scope_invalid")
        selector={"domain":"channel_knowledge",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        config=self.import_config
        if (set(config)!={"schema_version","access_scope","sources"} or config["schema_version"]!="1.0.1" or not access(config["access_scope"],selector) or config["access_scope"]["write_scopes"]
                or not isinstance(config["sources"],list) or len(config["sources"])!=1):raise PipelineError("channel_ingest_importer_authority_invalid")
        source=config["sources"][0]
        if (not isinstance(source,dict) or set(source)!={"source_id","scope","source_kind","modes","export_paths","source_access_scope","read_policy_allowed"}
                or source["source_id"]!=binding["source_id"] or source["scope"]!=scope or source["source_kind"]!="synthetic" or source["modes"]!=["manual"]
                or source["export_paths"]!=["export.json"] or not access(source["source_access_scope"],selector)
                or source["read_policy_allowed"] is not None and type(source["read_policy_allowed"]) is not bool):raise PipelineError("channel_ingest_importer_source_invalid")
        self.visibility=deepcopy(source["source_access_scope"])
        context=self.context
        if (set(context)!={"principal_account_id","access_scope","read_layers","write_layers","operations","source_policies"}
                or not token(context["principal_account_id"]) or not context["principal_account_id"].startswith("synthetic-")
                or not access(context["access_scope"],selector) or context["access_scope"]["read_scopes"] or context["read_layers"]!=[]
                or context["write_layers"] not in ([],["raw"]) or context["operations"] not in ([],["ingest"])
                or not isinstance(context["source_policies"],list) or len(context["source_policies"])!=1):raise PipelineError("channel_ingest_store_authority_invalid")
        source=context["source_policies"][0]
        if (not isinstance(source,dict) or set(source)!={"source_id","source_kind","allow_ingest","allow_query","allow_published","policy_ref","access_scope"}
                or source["source_id"]!=binding["source_id"] or source["source_kind"]!="synthetic" or type(source["allow_ingest"]) is not bool
                or source["allow_query"] is not False or source["allow_published"] is not False or not access(source["access_scope"],selector)
                or not isinstance(source["policy_ref"],str) or not source["policy_ref"].startswith("synthetic://")):raise PipelineError("channel_ingest_store_source_invalid")
        if (set(self.purpose)!={"source_id","scope","uses"} or self.purpose["source_id"]!=binding["source_id"] or self.purpose["scope"]!=scope or not isinstance(self.purpose["uses"],dict)):raise PipelineError("channel_ingest_purpose_policy_invalid")
        if (not isinstance(self.exports,dict) or not 1<=len(self.exports)<=100 or any(not token(a) or a in {".",".."} for a in self.exports)
                or len({a.casefold() for a in self.exports})!=len(self.exports)):raise PipelineError("channel_ingest_exports_invalid")
        self.code_files=self._code_files();self.watched={str(p):file_sha(p) for p in [path,self.python,*paths.values(),*map(Path,self.code_files)]}
        self.fingerprint=digest({"config":cfg,"files":self.watched});self.runner=runner or subprocess.run

    def _code_files(self):
        return {str(p) for repo in (self.importer,self.store) for p in (repo/"source").rglob("*") if p.is_file() and p.suffix in {".py",".json"}}

    def _store_id(self):
        identity=self._file_id(self.database)
        try:
            with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True,timeout=.1)) as db:
                if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}!=TABLES:raise PipelineError("channel_ingest_store_continuity_failed")
                rows=db.execute("SELECT contract_version,storage_version,scope_json FROM configuration WHERE singleton=1").fetchall()
                if len(rows)!=1 or rows[0][:2]!=("1.0.1",2) or load_object(rows[0][2].encode("utf-8"))!=self.binding["scope"]:raise PipelineError("channel_ingest_store_continuity_failed")
        except sqlite3.Error as error:
            code=getattr(error,"sqlite_errorcode",0)&255
            raise PipelineError("channel_ingest_store_busy" if code in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} else "channel_ingest_store_continuity_failed") from None
        if identity!=self._file_id(self.database):raise PipelineError("channel_ingest_store_continuity_failed")
        return identity

    def _bundle(self,bundle):
        if (not isinstance(bundle,dict) or set(bundle)!={"schema_version","source_id","scope","events"} or bundle["schema_version"]!="1.0.1"
                or bundle["source_id"]!=self.binding["source_id"] or bundle["scope"]!=self.binding["scope"]
                or not isinstance(bundle["events"],list) or len(bundle["events"])>20):return "bound_synthetic_channel_export_required"
        for record in bundle["events"]:
            if not isinstance(record,dict) or set(record)!=RECORD:return "minimal_channel_record_fields_required"
            if record["schema_version"]!="1.0.1" or record["record_kind"]!="raw_event" or record["source_id"]!=self.binding["source_id"]:return "synthetic_raw_channel_source_required"
            if record["scope"]!=self.binding["scope"] or record["actor_account_id"]!=self.binding["actor_account_id"]:return "channel_identity_or_scope_not_bound"
            if record["access_scope"]!=self.visibility:return "incoming_channel_visibility_mismatch"
            if any(not token(record[k]) or not record[k].startswith("synthetic-") for k in ("source_event_id","record_id")):return "synthetic_channel_identity_required"
            if type(record["revision"]) is not int or record["revision"]<1:return "explicit_channel_revision_required"
            if not isinstance(record["text"],str) or len(record["text"])>16_384 or not isinstance(record["quality_flags"],list) or "synthetic" not in record["quality_flags"]:return "synthetic_channel_text_required"
            refs=record["source_refs"]
            if (not isinstance(refs,list) or len(refs)!=1 or not isinstance(refs[0],dict) or set(refs[0])!={"source_id","record_id","revision","locator","transformation_version"}
                    or any(refs[0][k]!=record[k] for k in ("source_id","record_id","revision")) or refs[0]["transformation_version"] is not None
                    or not isinstance(refs[0]["locator"],str) or not refs[0]["locator"].startswith("synthetic://")):return "synthetic_channel_provenance_required"
        try:encoded(bundle)
        except (ValueError,TypeError,UnicodeError,RecursionError):return "channel_export_json_invalid"
        return None

    @staticmethod
    def _result(value,request_id,*,read=False):
        if (not isinstance(value,dict) or value.get("schema_version")!="1.0.1" or value.get("request_id")!=request_id or value.get("domain")!="channel_knowledge"
                or value.get("status") not in STATUSES or not isinstance(value.get("evidence"),list) or not read and value["evidence"]):raise PipelineError("channel_ingest_native_result_invalid")
        reason=value.get("reason_code")
        if reason is not None and (not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None):raise PipelineError("channel_ingest_native_result_invalid")
        return value["status"],reason

    @staticmethod
    def _key(record):return tuple(record[k] for k in ("source_id","source_event_id","revision","record_id"))

    def _selected(self,value,bundle):
        originals={}
        for record in bundle["events"]:originals.setdefault(self._key(record),record)
        selected=[];seen=set()
        for evidence in value["evidence"]:
            if not isinstance(evidence,dict) or set(evidence)!=EVIDENCE:raise PipelineError("channel_ingest_evidence_invalid")
            key=self._key(evidence);record=originals.get(key)
            if (record is None or key in seen or evidence["layer"]!="raw" or evidence["source_kind"]!="synthetic" or evidence["rank"] is not None
                    or not isinstance(evidence["match_reasons"],list) or any(evidence[k]!=record[k] for k in RECORD-{"schema_version","record_kind","access_scope"})):raise PipelineError("channel_ingest_evidence_not_original")
            seen.add(key);selected.append(deepcopy(record))
        if seen!=set(originals):raise PipelineError("channel_ingest_evidence_incomplete")
        return selected

    def ingest(self,payload):
        if (not isinstance(payload,dict) or set(payload)!={"request_id","synthetic","exports"} or not token(payload["request_id"]) or payload["synthetic"] is not True
                or not isinstance(payload["exports"],list) or len(payload["exports"])>20 or any(not token(a) or a in {".",".."} for a in payload["exports"])):raise RetrievalInputError("synthetic_channel_batch_invalid")
        report={"request_id":payload["request_id"],"mode":"synthetic_channel_ingest_only","items":[],"model_invoked":False,"network_used":False,"public_output":"not_attempted"}
        grant=self.purpose["uses"].get("local_cache");source=self.import_config["sources"][0]
        for bundle_index,alias in enumerate(payload["exports"],1):
            item={"bundle_index":bundle_index,"export":alias,"status":"held","stage":"input","storage_outcome":"not_attempted","reason_code":None}
            bundle=self.exports.get(alias);reason="registered_synthetic_export_required" if bundle is None else self._bundle(bundle)
            if not reason and (not isinstance(grant,dict) or grant.get("status")!="allowed" or not isinstance(grant.get("basis"),str) or not grant["basis"].strip()):reason="local_cache_not_allowed"
            if not reason and (source["read_policy_allowed"] is not True or not self.import_config["access_scope"]["read_scopes"] or not source["source_access_scope"]["read_scopes"]):reason="channel_source_read_not_allowed"
            if reason:item["reason_code"]=reason;report["items"].append(item);continue
            request_id=payload["request_id"]+"-b"+str(bundle_index)
            try:
                self._unchanged()
                with self._owned():
                    job=self.state/"imports"/alias;job.mkdir(parents=True,exist_ok=True)
                    query={"schema_version":"1.0.1","request_id":request_id,"text":"synthetic exact channel export handoff","intent":"handoff","scope":self.binding["scope"],"actor_account_id":self.binding["actor_account_id"],"person_id":None,"time_from":None,"time_to":None,"exclude_event_ids":[],"limit":100,"layer":"raw","conditions":{},"access_scope":self.import_config["access_scope"]}
                    for name,value in (("export.json",bundle),("config.json",self.import_config),("query.json",query)):(job/name).write_bytes(encoded(value))
                    item["stage"]="importer"
                    child=self._call([str(self.python),"-B","-X","utf8","-m","source.hgpt_ssr","--config",str(job/"config.json"),"--query",str(job/"query.json"),"--mode","manual"],self.importer)
                    if len(child.stdout)>2_000_000:raise PipelineError("channel_ingest_native_output_oversized")
                    value=load_object(child.stdout);status,reason=self._result(value,request_id,read=True);item.update(importer_status=status,importer_exit=child.returncode,reason_code=reason)
                    (job/"source-result.json").write_bytes(encoded(value));self._unchanged()
                    selected=self._selected(value,bundle) if status=="ok" and child.returncode==0 else []
                if not selected:report["items"].append(item);continue
            except (PipelineError,OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:
                item["reason_code"]=getattr(error,"code","channel_ingest_local_operation_failed");report["items"].append(item);continue
            for index,record in enumerate(selected,1):
                outcome={"bundle_index":bundle_index,"export":alias,"record_index":index,"status":"held","stage":"input","storage_outcome":"not_attempted","record_sha256":digest(record),"reason_code":None,"importer_status":"ok"};report["items"].append(outcome)
                try:
                    self._unchanged()
                    with self._owned():
                        job=self.state/"imports"/alias;event_file=job/("record-"+str(index)+".json")
                        for name,body in ((event_file.name,record),("scope.json",self.binding["scope"]),("context.json",self.context)):(job/name).write_bytes(encoded(body))
                        outcome.update(stage="store",storage_outcome="unknown")
                        native_id=request_id+"-r"+str(index)
                        child=self._call([str(self.python),"-B","-X","utf8","source/ssr.py","--db",str(self.database),"--scope-file",str(job/"scope.json"),"--trusted-context",str(job/"context.json"),"ingest","--request-id",native_id,"--layer","raw","--event-file",str(event_file)],self.store)
                        if len(child.stdout)>2_000_000:raise PipelineError("channel_ingest_native_output_oversized")
                        status,reason=self._result(load_object(child.stdout),native_id);outcome.update(store_status=status,store_exit=child.returncode,reason_code=reason)
                        if status=="ok" and child.returncode==0:
                            if reason in (None,"idempotent"):outcome.update(status="stored" if reason is None else "duplicate",storage_outcome="confirmed")
                            else:outcome.update(status="uncertain",reason_code="channel_ingest_store_ack_unrecognized")
                        self._unchanged()
                    with self._owned():JsonlLog(self.state/"ingest-receipts.jsonl").append({"request_id":native_id,**outcome,"binding_sha256":self.fingerprint,"local_cache_grant_sha256":digest(grant),"ai_input":"not_used","ai_train":"not_used","public_output":"not_attempted"})
                except (PipelineError,OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:
                    outcome.update(status="uncertain" if outcome["stage"]=="store" else "held",reason_code=getattr(error,"code","channel_ingest_local_operation_failed"))
        counts={k:sum(i["status"]==k for i in report["items"]) for k in ("stored","duplicate","held","uncertain")};successful=counts["stored"]+counts["duplicate"]
        report.update(counts=counts,status="empty" if not report["items"] else "complete" if successful==len(report["items"]) else "partial" if successful else "held")
        return report

    def _unchanged(self):
        if self.code_files!=self._code_files() or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("channel_ingest_configuration_changed")


    @staticmethod
    def _file_id(path):
        info=path.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("channel_ingest_file_identity_unavailable")
        return [str(info.st_dev),str(info.st_ino)]


    def _job_files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("channel_ingest_state_path_invalid")
            for name in files:
                path=Path(folder)/name;result[str(path.relative_to(self.state))]={"identity":self._file_id(path),"sha256":file_sha(path)}
        return result


    def _save_owner(self,owner):
        temp=self.state/("owner-"+uuid4().hex+".tmp");temp.write_bytes(encoded(owner));os.replace(temp,self.state/"synthetic-channel-owner.json")


    @contextmanager
    def _owned(self):
        marker=self.state/"synthetic-channel-owner.json"
        for name in ("synthetic-channel-owner.json","pipeline.lock","ingest-receipts.jsonl","imports"):
            selected=self.state/name
            if selected.is_symlink() or getattr(selected,"is_junction",lambda:False)() or selected.exists() and name!="imports" and not S_ISREG(selected.lstat().st_mode):raise PipelineError("channel_ingest_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("channel_ingest_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file:file.write(encoded({"mode":"synthetic_channel_ingest_only","binding_sha256":self.fingerprint,"store_file_id":None,"files":{}}))
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
            except OSError:raise PipelineError("channel_ingest_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","binding_sha256","store_file_id","files"} or owner["mode"]!="synthetic_channel_ingest_only" or owner["binding_sha256"]!=self.fingerprint:raise PipelineError("channel_ingest_state_binding_mismatch")
                if self._job_files()!=owner["files"]:raise PipelineError("channel_ingest_handoff_continuity_failed")
                if owner["store_file_id"] is None and self.database.exists():raise PipelineError("channel_ingest_unattested_store")
                if owner["store_file_id"] is not None and (not self.database.exists() or self._store_id()!=owner["store_file_id"]):raise PipelineError("channel_ingest_store_continuity_failed")
                try:yield owner
                finally:
                    owner["files"]=self._job_files()
                    if self.database.exists():
                        identity=self._store_id()
                        if owner["store_file_id"] not in (None,identity):raise PipelineError("channel_ingest_store_continuity_failed")
                        owner["store_file_id"]=identity
                    self._save_owner(owner)
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


    def _call(self,args,cwd,*,data=None):
        return self.runner(args,cwd=str(cwd),input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)

