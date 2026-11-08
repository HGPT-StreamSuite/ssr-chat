"""Host-registered synthetic snapshots -> task-22 handoff -> task-5 archive."""
from contextlib import closing,contextmanager
from copy import deepcopy
from datetime import datetime
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
OBJECTS={("events","table"),("events_lookup","index"),("events_no_update","trigger"),("events_no_delete","trigger"),("events_no_replace","trigger")}


class GameIngestPipeline:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        fields={"schema_version","mode","python","importer_repo","store_repo","store_policy","state_root","binding","importer_caller","snapshots"}
        if set(cfg)!=fields or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_game_ingest_only":raise PipelineError("game_ingest_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.importer=local(cfg["importer_repo"],path.parent);self.store=local(cfg["store_repo"],path.parent)
        policy_path=local(cfg["store_policy"],path.parent);root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("game_ingest_state_outside_dedicated_root")
        self.state=state.resolve();self.database=self.state/"synthetic-game.sqlite3"
        if not self.python.is_file() or not (self.importer/"source/hgpt_ssr_cli.py").is_file() or not (self.store/"src/hgpt_ssr/__main__.py").is_file():raise PipelineError("game_ingest_component_missing")
        self.binding=deepcopy(cfg["binding"]);self.caller=deepcopy(cfg["importer_caller"]);self.snapshots=deepcopy(cfg["snapshots"]);self.policy=load_object(policy_path.read_bytes())
        binding=self.binding
        if (not isinstance(binding,dict) or set(binding)!={"source_id","source_locator","actor_account_id","scope"} or not token(binding["source_id"]) or not binding["source_id"].startswith("synthetic-")
                or not isinstance(binding["source_locator"],str) or not binding["source_locator"].startswith(("document:synthetic-","urn:synthetic:"))
                or binding["actor_account_id"] is not None and (not token(binding["actor_account_id"]) or not binding["actor_account_id"].startswith("synthetic-"))):raise PipelineError("game_ingest_binding_invalid")
        scope=binding["scope"]
        if (not isinstance(scope,dict) or set(scope)!=set(SCOPE) or scope["domain"]!="game_knowledge" or not isinstance(scope["game_id"],str) or not scope["game_id"].startswith("synthetic-")
                or not isinstance(scope["content_version"],str) or not scope["content_version"].startswith("synthetic-")
                or any(scope[k] is not None and (not isinstance(scope[k],str) or not scope[k]) for k in ("platform","channel_id","session_id"))
                or binding["actor_account_id"] is not None and scope["platform"] is None):raise PipelineError("game_ingest_scope_invalid")
        selector={"domain":"game_knowledge",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        if not access(self.caller,selector):raise PipelineError("game_ingest_importer_caller_invalid")
        if not isinstance(self.snapshots,dict) or not 1<=len(self.snapshots)<=100:raise PipelineError("game_ingest_snapshots_invalid")
        for alias,snapshot in self.snapshots.items():
            if (not token(alias) or alias in {".",".."} or not isinstance(snapshot,dict) or set(snapshot)!={"content","source"}
                    or not isinstance(snapshot["content"],str) or len(snapshot["content"].encode("utf-8"))>262_144):raise PipelineError("game_ingest_snapshot_invalid")
            source=snapshot["source"]
            if (not isinstance(source,dict) or set(source)!={"source_id","source_locator","source_kind","adapter","authorized","scope","settings","source_access_scope"}
                    or any(source[k]!=binding[k] for k in ("source_id","source_locator","scope")) or source["source_kind"]!="synthetic" or source["adapter"]!="local_file"
                    or type(source["authorized"]) is not bool or not access(source["source_access_scope"],selector)):raise PipelineError("game_ingest_source_invalid")
            settings=source["settings"]
            if not isinstance(settings,dict) or set(settings)!={"path","format","event","acquisition"} or settings["path"]!="source.txt" or settings["format"]!="utf8":raise PipelineError("game_ingest_local_snapshot_required")
            event=settings["event"]
            if (not isinstance(event,dict) or set(event)!=RECORD-{"schema_version","source_id","scope","text","access_scope"} or event["actor_account_id"]!=binding["actor_account_id"]
                    or event["record_kind"]!="raw_event" or not isinstance(event["quality_flags"],list) or "synthetic" not in event["quality_flags"]
                    or any(not token(event[k]) or not event[k].startswith("synthetic-") for k in ("record_id","source_event_id"))):raise PipelineError("game_ingest_snapshot_metadata_invalid")
            if type(event["revision"]) is not int or event["revision"]<1:raise PipelineError("game_ingest_explicit_revision_required")
            expected={"source_id":binding["source_id"],"record_id":event["record_id"],"revision":event["revision"],"locator":binding["source_locator"],"transformation_version":None}
            if event["source_refs"]!=[expected]:raise PipelineError("game_ingest_provenance_invalid")
            acquisition=settings["acquisition"]
            if not isinstance(acquisition,dict) or acquisition.get("mode")!="provided_local" or acquisition.get("synthetic") is not True:raise PipelineError("game_ingest_synthetic_acquisition_required")
        policy=self.policy
        if (set(policy)!={"principal","synthetic_only","caller_access_scope","grants"} or not isinstance(policy["principal"],str) or not policy["principal"].startswith("synthetic-")
                or policy["synthetic_only"] is not True or not access(policy["caller_access_scope"],selector) or policy["caller_access_scope"]["read_scopes"]
                or not isinstance(policy["grants"],list) or len(policy["grants"])!=1):raise PipelineError("game_ingest_store_authority_invalid")
        grant=policy["grants"][0]
        if (not isinstance(grant,dict) or set(grant)!={"scope","operations","source_ids","source_kind","source_access_scope"} or grant["scope"]!=scope
                or grant["operations"]!=["ingest"] or grant["source_ids"]!=[binding["source_id"]] or grant["source_kind"]!="synthetic" or not access(grant["source_access_scope"],selector)):raise PipelineError("game_ingest_store_source_invalid")
        self.code_files=self._code_files();self.watched={str(p):file_sha(p) for p in [path,policy_path,self.python,*map(Path,self.code_files)]}
        self.fingerprint=digest({"config":cfg,"files":self.watched});self.runner=runner or subprocess.run

    def _code_files(self):
        return {str(p) for repo,folder in ((self.importer,"source"),(self.store,"src/hgpt_ssr")) for p in (repo/folder).rglob("*") if p.is_file() and p.suffix in {".py",".json"}}

    def _unchanged(self):
        if self.code_files!=self._code_files() or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("game_ingest_configuration_changed")

    @staticmethod
    def _file_id(path):
        info=path.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("game_ingest_file_identity_unavailable")
        return [str(info.st_dev),str(info.st_ino)]

    def _store_id(self):
        identity=self._file_id(self.database)
        try:
            with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True,timeout=.1)) as db:
                if (db.execute("PRAGMA application_id").fetchone()[0]!=0x48535352 or db.execute("PRAGMA user_version").fetchone()[0]!=1
                        or {tuple(r) for r in db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}!=OBJECTS):raise PipelineError("game_ingest_store_continuity_failed")
        except sqlite3.Error as error:
            code=getattr(error,"sqlite_errorcode",0)&255
            raise PipelineError("game_ingest_store_busy" if code in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} else "game_ingest_store_continuity_failed") from None
        if identity!=self._file_id(self.database):raise PipelineError("game_ingest_store_continuity_failed")
        return identity

    def _job_files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("game_ingest_state_path_invalid")
            for name in files:
                path=Path(folder)/name;result[str(path.relative_to(self.state))]={"identity":self._file_id(path),"sha256":file_sha(path)}
        return result

    def _save_owner(self,owner):
        temp=self.state/("owner-"+uuid4().hex+".tmp");temp.write_bytes(encoded(owner));os.replace(temp,self.state/"synthetic-game-owner.json")

    @contextmanager
    def _owned(self):
        marker=self.state/"synthetic-game-owner.json"
        for name in ("synthetic-game-owner.json","pipeline.lock","ingest-receipts.jsonl","imports"):
            selected=self.state/name
            if selected.is_symlink() or getattr(selected,"is_junction",lambda:False)() or selected.exists() and name!="imports" and not S_ISREG(selected.lstat().st_mode):raise PipelineError("game_ingest_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("game_ingest_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file:file.write(encoded({"mode":"synthetic_game_ingest_only","binding_sha256":self.fingerprint,"store_file_id":None,"files":{}}))
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
            except OSError:raise PipelineError("game_ingest_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","binding_sha256","store_file_id","files"} or owner["mode"]!="synthetic_game_ingest_only" or owner["binding_sha256"]!=self.fingerprint:raise PipelineError("game_ingest_state_binding_mismatch")
                if self._job_files()!=owner["files"]:raise PipelineError("game_ingest_handoff_continuity_failed")
                if owner["store_file_id"] is None and self.database.exists():raise PipelineError("game_ingest_unattested_store")
                if owner["store_file_id"] is not None and (not self.database.exists() or self._store_id()!=owner["store_file_id"]):raise PipelineError("game_ingest_store_continuity_failed")
                try:yield owner
                finally:
                    owner["files"]=self._job_files()
                    if self.database.exists():
                        identity=self._store_id()
                        if owner["store_file_id"] not in (None,identity):raise PipelineError("game_ingest_store_continuity_failed")
                        owner["store_file_id"]=identity
                    self._save_owner(owner)
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _expected(self,snapshot):
        source=snapshot["source"]
        return {**deepcopy(source["settings"]["event"]),"schema_version":"1.0.1","source_id":source["source_id"],"scope":deepcopy(source["scope"]),"text":snapshot["content"],"access_scope":deepcopy(source["source_access_scope"])}

    @staticmethod
    def _result(value,request_id,*,read=False):
        if (not isinstance(value,dict) or value.get("schema_version")!="1.0.1" or value.get("domain")!="game_knowledge" or value.get("request_id")!=request_id
                or value.get("status") not in STATUSES or not isinstance(value.get("evidence"),list) or not read and value["evidence"]):raise PipelineError("game_ingest_native_result_invalid")
        reason=value.get("reason_code")
        if not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None:raise PipelineError("game_ingest_native_result_invalid")
        return value["status"],reason

    def _call(self,args,cwd,*,data=None):
        return self.runner(args,cwd=str(cwd),input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)

    def ingest(self,payload):
        if (not isinstance(payload,dict) or set(payload)!={"request_id","synthetic","snapshots"} or not token(payload["request_id"]) or payload["synthetic"] is not True
                or not isinstance(payload["snapshots"],list) or len(payload["snapshots"])>50 or any(not token(a) or a in {".",".."} for a in payload["snapshots"])):raise RetrievalInputError("synthetic_game_batch_invalid")
        report={"request_id":payload["request_id"],"mode":"synthetic_game_ingest_only","items":[],"model_invoked":False,"network_used":False,"public_output":"not_attempted"}
        for index,alias in enumerate(payload["snapshots"],1):
            item={"index":index,"snapshot":alias,"status":"held","stage":"input","reason_code":None,"storage_outcome":"not_attempted"};report["items"].append(item)
            snapshot=self.snapshots.get(alias)
            if snapshot is None:item["reason_code"]="registered_synthetic_snapshot_required";continue
            acquisition=snapshot["source"]["settings"]["acquisition"];policy=acquisition.get("policy")
            uses=policy.get("uses") if isinstance(policy,dict) else None
            purpose=uses.get("local_cache") if isinstance(uses,dict) else None
            if not isinstance(purpose,dict) or purpose.get("status")!="allowed" or not isinstance(purpose.get("basis"),str) or not purpose["basis"].strip():item["reason_code"]="local_cache_not_allowed";continue
            source=snapshot["source"];preflight=None
            if source["authorized"] is not True:preflight="source_not_authorized"
            for operation in ("read","write"):
                if not self.caller[operation+"_scopes"]:preflight="importer_"+operation+"_not_allowed"
                if not source["source_access_scope"][operation+"_scopes"]:preflight="source_"+operation+"_not_allowed"
            for name in ("source_policy","robots"):
                approval=acquisition.get(name)
                try:
                    valid=(isinstance(approval,dict) and approval.get("decision") in ({"approved"} if name=="source_policy" else {"approved","not_applicable"})
                           and all(isinstance(approval.get(k),str) and approval[k].strip() for k in ("authority","checked_at","reference"))
                           and datetime.fromisoformat(approval["checked_at"]).tzinfo is not None)
                except (ValueError,TypeError):valid=False
                if not valid or name=="source_policy" and approval.get("purpose")!="local_cache":preflight=name+"_not_approved"
            if preflight:item["reason_code"]=preflight;continue
            request_id=payload["request_id"]+"-"+str(index)
            try:
                self._unchanged()
                with self._owned():
                    job=self.state/"imports"/alias;job.mkdir(parents=True,exist_ok=True)
                    registry={"schema_version":"1.0.1","synthetic":True,"state_dir":"handoff","caller_access_scope":self.caller,"sources":[snapshot["source"]]}
                    query={"schema_version":"1.0.1","request_id":request_id,"text":"synthetic registered snapshot import","intent":"import","scope":self.binding["scope"],"actor_account_id":None,"person_id":None,"time_from":None,"time_to":None,"exclude_event_ids":[],"limit":1,"layer":"raw","conditions":{k:self.binding[k] for k in ("source_id","source_locator")},"access_scope":self.caller}
                    for name,value in (("registry.json",registry),("query.json",query)):(job/name).write_bytes(encoded(value))
                    (job/"source.txt").write_bytes(snapshot["content"].encode("utf-8"));item["stage"]="importer"
                    child=self._call([str(self.python),"-B","-X","utf8","source/hgpt_ssr_cli.py","import","--config",str(job/"registry.json"),"--query",str(job/"query.json")],self.importer)
                    if len(child.stdout)>2_000_000:raise PipelineError("game_ingest_native_output_oversized")
                    value=load_object(child.stdout);status,reason=self._result(value,request_id,read=True);item.update(importer_status=status,importer_exit=child.returncode,reason_code=reason)
                    if status=="ok" and child.returncode==0:
                        handoff_path=job/"handoff/handoff.jsonl"
                        if not handoff_path.is_file() or handoff_path.stat().st_size>2_000_000:raise PipelineError("game_ingest_handoff_missing_or_oversized")
                        lines=handoff_path.read_bytes().splitlines()
                        if len(lines)!=1:raise PipelineError("game_ingest_handoff_invalid")
                        handoff=load_object(lines[0]);expected=self._expected(snapshot);stable={k:v for k,v in expected.items() if k not in {"received_at","access_scope"}}
                        expected_acquisition={**deepcopy(acquisition),"media_type":"text/plain"}
                        if (set(handoff)!={"schema_version","record","source_locator","source_kind","content_sha256","payload_sha256","handoff_at","acquisition"}
                                or handoff["schema_version"]!="1.0.1" or handoff["record"]!=expected or handoff["source_locator"]!=self.binding["source_locator"] or handoff["source_kind"]!="synthetic"
                                or handoff["content_sha256"]!=file_sha(job/"source.txt") or handoff["payload_sha256"]!=digest(stable) or handoff["acquisition"]!=expected_acquisition
                                or datetime.fromisoformat(handoff["handoff_at"]).tzinfo is None or len(value["evidence"])!=1 or value["evidence"][0].get("source_kind")!="synthetic"
                                or any(value["evidence"][0].get(k)!=expected[k] for k in expected if k not in {"schema_version","record_kind","access_scope"})):raise PipelineError("game_ingest_handoff_invalid")
                        self._unchanged();item.update(stage="store",storage_outcome="unknown",record_sha256=digest(expected),handoff_sha256=digest(handoff))
                        policy_path=job/"store-policy.json";policy_path.write_bytes(encoded(self.policy))
                        child=self._call([str(self.python),"-B","-X","utf8","src/hgpt_ssr","ingest","--db",str(self.database),"--policy",str(policy_path),"--request-id",request_id,"--input","-"],self.store,data=encoded(expected))
                        if len(child.stdout)>2_000_000:raise PipelineError("game_ingest_native_output_oversized")
                        status,reason=self._result(load_object(child.stdout),request_id);item.update(store_status=status,store_exit=child.returncode,reason_code=reason)
                        if child.returncode==0 and status=="ok":
                            if reason in {"inserted","idempotent"}:item.update(status="stored" if reason=="inserted" else "duplicate",storage_outcome="confirmed")
                            else:item.update(status="uncertain",reason_code="game_ingest_store_ack_unrecognized")
                        self._unchanged()
                with self._owned():
                    JsonlLog(self.state/"ingest-receipts.jsonl").append({"request_id":request_id,**item,"binding_sha256":self.fingerprint,"local_cache_grant_sha256":digest(purpose),"ai_input":"not_used","ai_train":"not_used","public_output":"not_attempted"})
            except (PipelineError,OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError) as error:
                item.update(status="uncertain" if item["stage"]=="store" or item["storage_outcome"]=="unknown" else "held",reason_code=getattr(error,"code","game_ingest_local_operation_failed"))
        counts={k:sum(i["status"]==k for i in report["items"]) for k in ("stored","duplicate","held","uncertain")};successful=counts["stored"]+counts["duplicate"]
        report.update(counts=counts,status="empty" if not report["items"] else "complete" if successful==len(report["items"]) else "partial" if successful else "held")
        return report
