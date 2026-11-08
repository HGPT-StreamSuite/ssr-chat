"""Explicit synthetic relationship/visit declarations -> native task-26 ledger."""
from contextlib import closing,contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re,sqlite3,subprocess
from stat import S_ISREG
from uuid import uuid4

from .chat_ingest import RECORD,SCOPE,FLAGS,access,digest,encoded,file_sha,local,token
from .channel_ingest import EVIDENCE
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

STATUSES={"ok","no_match","skipped","error","missing_input","denied","not_configured"}


class ChannelEventGateway:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        if set(cfg)!={"schema_version","mode","python","events_repo","writer_policy","reader_policy","state_root","binding","record_visibility","events","queries"} or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_channel_events_only":raise PipelineError("channel_events_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.repo=local(cfg["events_repo"],path.parent);root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("channel_events_state_outside_dedicated_root")
        self.state=state.resolve();self.database=self.state/"synthetic-channel-events.sqlite3";self.binding=deepcopy(cfg["binding"]);binding=self.binding
        if (not isinstance(binding,dict) or set(binding)!={"source_id","actor_account_id","scope"} or any(not token(binding[k]) or not binding[k].startswith("synthetic-") for k in ("source_id","actor_account_id"))):raise PipelineError("channel_events_binding_invalid")
        scope=binding["scope"]
        if (not isinstance(scope,dict) or set(scope)!=set(SCOPE) or scope["domain"]!="person_profile" or any(not isinstance(scope[k],str) or not scope[k].startswith("synthetic-") for k in ("platform","channel_id"))
                or any(scope[k] is not None and (not isinstance(scope[k],str) or not scope[k].startswith("synthetic-")) for k in ("game_id","session_id","content_version"))):raise PipelineError("channel_events_scope_invalid")
        if not self.python.is_file() or not (self.repo/"run.py").is_file() or not (self.repo/"src/hgpt_ssr/store.py").is_file():raise PipelineError("channel_events_component_missing")
        selector={"domain":"person_profile",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}};self.visibility=deepcopy(cfg["record_visibility"])
        if not access(self.visibility,selector):raise PipelineError("channel_events_visibility_invalid")
        paths={k:local(cfg[k],path.parent) for k in ("writer_policy","reader_policy")};self.writer=load_object(paths["writer_policy"].read_bytes());self.reader=load_object(paths["reader_policy"].read_bytes())
        for name,policy,operations in (("writer",self.writer,{"ingest"}),("reader",self.reader,{"get","export"})):
            if (set(policy)!={"schema_version","operations","access_scope","sources"} or policy["schema_version"]!="1.0.1" or not isinstance(policy["operations"],list) or set(policy["operations"])-operations
                    or not access(policy["access_scope"],selector) or policy["access_scope"]["read_scopes" if name=="writer" else "write_scopes"]
                    or not isinstance(policy["sources"],dict) or set(policy["sources"])!={binding["source_id"]}):raise PipelineError("channel_events_policy_invalid")
            source=policy["sources"][binding["source_id"]]
            if not isinstance(source,dict) or set(source)!={"access_scope","policy"} or not access(source["access_scope"],selector) or not isinstance(source["policy"],dict):raise PipelineError("channel_events_source_policy_invalid")
        self.events=deepcopy(cfg["events"]);self.queries=deepcopy(cfg["queries"])
        if not isinstance(self.events,dict) or not 1<=len(self.events)<=100 or any(not token(a) or a in {".",".."} for a in self.events):raise PipelineError("channel_events_registry_invalid")
        if not isinstance(self.queries,dict) or not 1<=len(self.queries)<=100:raise PipelineError("channel_events_queries_invalid")
        for alias,item in self.queries.items():
            if not token(alias) or not isinstance(item,dict) or set(item)!={"operation","query"} or item["operation"] not in {"get","export"}:raise PipelineError("channel_events_query_binding_invalid")
            query=item["query"]
            if (not isinstance(query,dict) or query.get("scope")!=scope or query.get("person_id") is not None or query.get("actor_account_id") not in ({binding["actor_account_id"]} if item["operation"]=="get" else {None,binding["actor_account_id"]})
                    or not access(query.get("access_scope"),selector) or query["access_scope"]["write_scopes"]):raise PipelineError("channel_events_query_binding_invalid")
        self.code_files=self._code_files();watched=[path,self.python,*paths.values(),*map(Path,self.code_files)]
        if any(p.is_symlink() for p in watched):raise PipelineError("channel_events_symlink_not_allowed")
        self.watched={str(p):file_sha(p) for p in watched};self.policy_fingerprint=digest({"config":cfg,"files":self.watched})
        # Data ownership is stable across explicit trusted policy updates. Every
        # operation still rechecks the current policy; native immutable snapshots
        # retain the first source/Record authority and cannot be widened here.
        self.identity=digest({"mode":cfg["mode"],"python":str(self.python),"python_sha256":file_sha(self.python),"code":{p:file_sha(p) for p in self.code_files},"root":str(root),"binding":binding,"record_visibility":self.visibility})
        self.runner=runner or subprocess.run

    def _code_files(self):return {str(p) for p in (self.repo/"src").rglob("*.py")}|{str(self.repo/"run.py")}

    def _unchanged(self):
        if self._code_files()!=self.code_files or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("channel_events_configuration_changed")

    def _db_id(self):
        info=self.database.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("channel_events_store_identity_invalid")
        identity=[str(info.st_dev),str(info.st_ino)]
        try:
            with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True,timeout=.1)) as db:
                if (db.execute("PRAGMA application_id").fetchone()[0]!=0x48535352 or db.execute("PRAGMA user_version").fetchone()[0]!=2
                        or {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}!={"raw_events"}):raise PipelineError("channel_events_store_continuity_failed")
        except sqlite3.Error as error:
            raise PipelineError("channel_events_store_busy" if getattr(error,"sqlite_errorcode",0)&255 in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} else "channel_events_store_continuity_failed") from None
        now=self.database.lstat()
        if identity!=[str(now.st_dev),str(now.st_ino)]:raise PipelineError("channel_events_store_continuity_failed")
        return identity

    def _files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("channel_events_state_path_invalid")
            for name in files:
                path=Path(folder)/name;info=path.lstat()
                if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("channel_events_state_path_invalid")
                result[str(path.relative_to(self.state))]={"identity":[str(info.st_dev),str(info.st_ino)],"sha256":file_sha(path)}
        return result

    @contextmanager
    def _owned(self,*,allow_create=False):
        marker=self.state/"synthetic-channel-events-owner.json"
        for name in (marker.name,"pipeline.lock","event-receipts.jsonl",self.database.name,"imports"):
            path=self.state/name
            if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or path.exists() and name!="imports" and not S_ISREG(path.lstat().st_mode):raise PipelineError("channel_events_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("channel_events_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as f:f.write(encoded({"mode":"synthetic_channel_events_only","identity_sha256":self.identity,"store_file_id":None,"files":{}}))
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
            except OSError:raise PipelineError("channel_events_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","identity_sha256","store_file_id","files"} or owner["mode"]!="synthetic_channel_events_only" or owner["identity_sha256"]!=self.identity:raise PipelineError("channel_events_state_binding_mismatch")
                if self._files()!=owner["files"]:raise PipelineError("channel_events_handoff_continuity_failed")
                if owner["store_file_id"] is None and self.database.exists():raise PipelineError("channel_events_unattested_store")
                if owner["store_file_id"] is not None and (not self.database.exists() or self._db_id()!=owner["store_file_id"]):raise PipelineError("channel_events_store_continuity_failed")
                try:yield
                finally:
                    owner["files"]=self._files()
                    if self.database.exists():
                        identity=self._db_id()
                        if owner["store_file_id"] is None and not allow_create:raise PipelineError("channel_events_unattested_store")
                        if owner["store_file_id"] not in (None,identity):raise PipelineError("channel_events_store_continuity_failed")
                        owner["store_file_id"]=identity
                    temp=self.state/("owner-"+uuid4().hex+".tmp");temp.write_bytes(encoded(owner));os.replace(temp,marker)
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _registered(self,alias):
        item=self.events.get(alias)
        if not isinstance(item,dict) or set(item)!={"envelope","declaration"}:raise PipelineError("channel_events_registered_declaration_required")
        value=item["envelope"];declaration=item["declaration"]
        if not isinstance(value,dict) or set(value)!={"record","facts","source_kind"} or value["source_kind"]!="synthetic" or not isinstance(value["facts"],dict):raise PipelineError("channel_events_explicit_facts_required")
        record=value["record"]
        if (not isinstance(record,dict) or set(record)!=RECORD or record["schema_version"]!="1.0.1" or record["record_kind"]!="raw_event" or record["scope"]!=self.binding["scope"]
                or record["source_id"]!=self.binding["source_id"] or record["actor_account_id"]!=self.binding["actor_account_id"] or record["access_scope"]!=self.visibility
                or any(not token(record[k]) or not record[k].startswith("synthetic-") for k in ("source_event_id","record_id")) or type(record["revision"]) is not int or record["revision"]<1
                or not isinstance(record["text"],str) or len(record["text"])>16384 or not isinstance(record["quality_flags"],list) or "synthetic" not in record["quality_flags"]):raise PipelineError("channel_events_synthetic_record_not_bound")
        refs=record["source_refs"]
        if (not isinstance(refs,list) or len(refs)!=1 or not isinstance(refs[0],dict) or set(refs[0])!={"source_id","record_id","revision","locator","transformation_version"}
                or any(refs[0][k]!=record[k] for k in ("source_id","record_id","revision")) or refs[0]["transformation_version"] is not None or not isinstance(refs[0]["locator"],str) or not refs[0]["locator"].startswith("synthetic://")):raise PipelineError("channel_events_source_reference_required")
        if (not isinstance(declaration,dict) or set(declaration)!={"status","source_id","scope","payload_sha256","basis"} or declaration["status"]!="declared" or declaration["source_id"]!=self.binding["source_id"]
                or declaration["scope"]!=self.binding["scope"] or declaration["payload_sha256"]!=digest(value) or not isinstance(declaration["basis"],str) or not declaration["basis"].strip()):raise PipelineError("channel_events_source_declaration_not_confirmed")
        return value

    def _grant(self,policy):
        uses=policy["sources"][self.binding["source_id"]]["policy"].get("uses",{})
        grant=uses.get("local_cache",{}) if isinstance(uses,dict) else {}
        return isinstance(grant,dict) and grant.get("status")=="allowed" and isinstance(grant.get("basis"),str) and bool(grant["basis"].strip())

    def _call(self,args):return self.runner([str(self.python),"-B","-X","utf8","run.py","--db",str(self.database),*args],cwd=str(self.repo),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)

    def _result(self,child,request_id):
        if len(child.stdout)>2_000_000:raise PipelineError("channel_events_native_output_oversized")
        result=load_object(child.stdout)
        if (result.get("schema_version")!="1.0.1" or result.get("request_id")!=request_id or result.get("domain")!="person_profile" or result.get("status") not in STATUSES or not isinstance(result.get("evidence"),list)):raise PipelineError("channel_events_native_result_invalid")
        reason=result.get("reason_code")
        if reason is not None and (not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None):raise PipelineError("channel_events_native_result_invalid")
        return result

    def _job(self,payload):
        path=self.state/"imports"/("request-"+digest(payload)[:16]+"-"+uuid4().hex[:12]);path.mkdir(parents=True);return path

    def _read_evidence(self,result):
        originals=[]
        for alias in self.events:
            try:originals.append(self._registered(alias))
            except PipelineError:pass
        for item in result["evidence"]:
            if not isinstance(item,dict) or set(item)!=EVIDENCE or item["layer"]!="raw" or item["source_kind"]!="synthetic":raise PipelineError("channel_events_evidence_invalid")
            matches=[e for e in originals if all(item[k]==e["record"][k] for k in RECORD-{"schema_version","record_kind","access_scope"})]
            if not matches:raise PipelineError("channel_events_evidence_not_declared_original")
            if not any("explicit_event_type:"+e["facts"].get("event_type","") in item["match_reasons"] and (e["facts"].get("event_type")!="relationship" or "relationship_status:"+e["facts"].get("status","") in item["match_reasons"]) for e in matches):raise PipelineError("channel_events_explicit_facts_changed")

    def run(self,payload):
        if not isinstance(payload,dict) or not token(payload.get("request_id")) or payload.get("synthetic") is not True or payload.get("operation") not in {"ingest","get","export"}:raise RetrievalInputError("synthetic_channel_events_request_invalid")
        operation=payload["operation"];fields={"request_id","synthetic","operation","events" if operation=="ingest" else "query"}
        if (set(payload)!=fields or operation=="ingest" and (not isinstance(payload["events"],list) or len(payload["events"])>20 or any(not token(a) for a in payload["events"]))
                or operation!="ingest" and not token(payload["query"])):raise RetrievalInputError("synthetic_channel_events_request_invalid")
        output={"request_id":payload["request_id"],"operation":operation,"mode":"synthetic_channel_events_only","status":"held","model_invoked":False,"network_used":False,"public_output":"not_attempted"}
        if operation=="ingest":
            output["items"]=[]
            for index,alias in enumerate(payload["events"],1):
                item={"index":index,"status":"held","stage":"input","reason_code":None,"storage_outcome":"not_attempted"};output["items"].append(item)
                try:
                    value=self._registered(alias)
                    if not self._grant(self.writer):raise PipelineError("channel_events_local_cache_not_allowed")
                    self._unchanged()
                    with self._owned(allow_create=True):
                        job=self._job({**payload,"item":index});(job/"event.json").write_bytes(encoded(value));(job/"policy.json").write_bytes(encoded(self.writer));native_id=payload["request_id"]+"-"+str(index)
                        item.update(stage="store",storage_outcome="unknown");child=self._call(["--policy",str(job/"policy.json"),"ingest","--request-id",native_id,"--input",str(job/"event.json")]);result=self._result(child,native_id)
                        if result["evidence"]:raise PipelineError("channel_events_write_receipt_exposes_raw")
                        item.update(native_status=result["status"],native_exit=child.returncode,reason_code=result.get("reason_code"));self._unchanged()
                        if child.returncode==0 and result["status"]=="ok" and result.get("reason_code") in {"ingested","idempotent_replay"}:item.update(status="stored" if result["reason_code"]=="ingested" else "duplicate",storage_outcome="confirmed")
                        elif result.get("reason_code")=="storage_error" or result["status"]=="ok":item.update(status="uncertain")
                        else:item["storage_outcome"]="rejected"
                except (PipelineError,OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:
                    item.update(status="uncertain" if item["storage_outcome"]=="unknown" else "held",reason_code=getattr(error,"code","channel_events_local_operation_failed"))
            counts={k:sum(i["status"]==k for i in output["items"]) for k in ("stored","duplicate","held","uncertain")};ok=counts["stored"]+counts["duplicate"]
            output.update(counts=counts,status="empty" if not output["items"] else "complete" if ok==len(output["items"]) else "partial" if ok else "held")
        else:
            output.update(event_count=None,reason_code=None)
            try:
                selected=self.queries.get(payload["query"])
                if selected is None or selected["operation"]!=operation:raise PipelineError("channel_events_registered_query_required")
                if not self._grant(self.reader):raise PipelineError("channel_events_local_cache_not_allowed")
                self._unchanged()
                with self._owned():
                    before=file_sha(self.database) if self.database.exists() else None;job=self._job(payload);query=deepcopy(selected["query"]);query["request_id"]=payload["request_id"]
                    (job/"query.json").write_bytes(encoded(query));(job/"policy.json").write_bytes(encoded(self.reader));args=["--policy",str(job/"policy.json"),operation,"--query",str(job/"query.json")]
                    if operation=="export":args.extend(["--output",str(job/"export.jsonl")])
                    child=self._call(args);result=self._result(child,payload["request_id"]);output.update(native_status=result["status"],native_exit=child.returncode,reason_code=result.get("reason_code"));self._unchanged()
                    if (file_sha(self.database) if self.database.exists() else None)!=before:raise PipelineError("channel_events_source_changed_during_read")
                    if child.returncode==0 and result["status"] in {"ok","no_match"}:
                        self._read_evidence(result)
                        if operation=="export" and result["status"]=="ok":
                            path=job/"export.jsonl"
                            if path.stat().st_size>2_000_000:raise PipelineError("channel_events_export_oversized")
                            exported=[load_object(line) for line in path.read_bytes().splitlines() if line.strip()]
                            originals=[]
                            for alias in self.events:
                                try:originals.append(self._registered(alias))
                                except PipelineError:pass
                            if len(exported)!=len(result["evidence"]) or any(v not in originals for v in exported):raise PipelineError("channel_events_export_not_declared_original")
                            output["export_sha256"]=file_sha(path)
                        (job/"native-result.json").write_bytes(encoded(result));output.update(status=result["status"],event_count=len(result["evidence"]),provenance_sha256=digest(result),limited=result.get("reason_code")=="limit_reached")
            except (PipelineError,OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:output.update(status="held",event_count=None,reason_code=getattr(error,"code","channel_events_local_operation_failed"))
        if self.state.exists():
            try:
                with self._owned():JsonlLog(self.state/"event-receipts.jsonl").append({**output,"identity_sha256":self.identity,"policy_sha256":self.policy_fingerprint,"ai_input":"not_used","ai_train":"not_used","person_linking":"not_used"})
            except (PipelineError,OSError,ValueError,TypeError,KeyError) as error:
                if operation!="ingest":output.update(status="held",event_count=None,reason_code=getattr(error,"code","channel_events_receipt_failed"))
                else:output["receipt_status"]="held"
        return output
