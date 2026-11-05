"""Synthetic raw person/account records via native task-6; no manual linking."""
from contextlib import closing,contextmanager
from copy import deepcopy
import os,re,sqlite3,subprocess
from pathlib import Path
from stat import S_ISREG
from uuid import uuid4
from .chat_ingest import RECORD,SCOPE,FLAGS,access,digest,encoded,file_sha,local,token
from .channel_ingest import EVIDENCE
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

STATUSES={"ok","no_match","skipped","error","missing_input","denied","not_configured"}


class IdentityRecordGateway:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        if set(cfg)!={"schema_version","mode","python","identity_repo","writer_access","reader_access","purpose_policy","state_root","binding","record_visibility","events","queries"} or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_identity_records_only":raise PipelineError("identity_records_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.repo=local(cfg["identity_repo"],path.parent);root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("identity_records_state_outside_dedicated_root")
        self.state=state.resolve();self.database=self.state/"synthetic-identity-records.sqlite3";self.binding=deepcopy(cfg["binding"]);b=self.binding
        if not isinstance(b,dict) or set(b)!={"source_id","scope","person_ids","account_ids"} or not token(b["source_id"]) or not b["source_id"].startswith("synthetic-"):raise PipelineError("identity_records_binding_invalid")
        for name,prefix in (("person_ids","synthetic-person-"),("account_ids","synthetic-account-")):
            ids=b[name]
            if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not token(v) or not v.startswith(prefix) for v in ids) or len(set(ids))!=len(ids):raise PipelineError("identity_records_declared_ids_required")
        scope=b["scope"]
        if (not isinstance(scope,dict) or set(scope)!=set(SCOPE) or scope["domain"]!="person_profile" or any(not isinstance(scope[k],str) or not scope[k].startswith("synthetic-") for k in ("platform","channel_id"))
                or any(scope[k] is not None and (not isinstance(scope[k],str) or not scope[k].startswith("synthetic-")) for k in ("game_id","session_id","content_version"))):raise PipelineError("identity_records_scope_invalid")
        if not self.python.is_file() or not (self.repo/"source/ssr_identity/__main__.py").is_file():raise PipelineError("identity_records_component_missing")
        selector={"domain":"person_profile",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}};self.visibility=deepcopy(cfg["record_visibility"])
        if not access(self.visibility,selector):raise PipelineError("identity_records_visibility_invalid")
        paths={k:local(cfg[k],path.parent) for k in ("writer_access","reader_access","purpose_policy")};self.writer=load_object(paths["writer_access"].read_bytes());self.reader=load_object(paths["reader_access"].read_bytes());self.purpose=load_object(paths["purpose_policy"].read_bytes())
        if not access(self.writer,selector) or self.writer["read_scopes"] or not access(self.reader,selector) or self.reader["write_scopes"]:raise PipelineError("identity_records_caller_access_invalid")
        if set(self.purpose)!={"source_id","scope","read_decision","write_decision","uses"} or self.purpose["source_id"]!=b["source_id"] or self.purpose["scope"]!=scope or not isinstance(self.purpose["uses"],dict):raise PipelineError("identity_records_purpose_binding_invalid")
        for name in ("read_decision","write_decision"):
            d=self.purpose[name]
            if d is not None and (not isinstance(d,dict) or set(d)!={"allowed","basis"} or type(d["allowed"]) is not bool or not isinstance(d["basis"],str) or not d["basis"].strip()):raise PipelineError("identity_records_source_decision_invalid")
        self.events=deepcopy(cfg["events"]);self.queries=deepcopy(cfg["queries"])
        if not isinstance(self.events,dict) or not 1<=len(self.events)<=100 or any(not token(a) or a in {".",".."} for a in self.events):raise PipelineError("identity_records_registry_invalid")
        if not isinstance(self.queries,dict) or not 1<=len(self.queries)<=100:raise PipelineError("identity_records_queries_invalid")
        for alias,item in self.queries.items():
            if not token(alias) or not isinstance(item,dict) or item.get("operation") not in {"query","history","get"}:raise PipelineError("identity_records_query_binding_invalid")
            if item["operation"]=="query":
                q=item.get("query")
                if (set(item)!={"operation","query"} or not isinstance(q,dict) or q.get("scope")!=scope or q.get("person_id") not in {None,*b["person_ids"]} or q.get("actor_account_id") not in {None,*b["account_ids"]}
                        or not access(q.get("access_scope"),selector) or q["access_scope"]["write_scopes"]):raise PipelineError("identity_records_query_binding_invalid")
            elif (set(item)!={"operation","source_event_id","revision"} or not token(item["source_event_id"]) or not item["source_event_id"].startswith("synthetic-")
                    or item["revision"] is not None and (type(item["revision"]) is not int or item["revision"]<1) or item["operation"]=="history" and item["revision"] is not None):raise PipelineError("identity_records_history_binding_invalid")
        self.code_files=self._code_files();watched=[path,self.python,*paths.values(),*map(Path,self.code_files)]
        if any(p.is_symlink() for p in watched):raise PipelineError("identity_records_symlink_not_allowed")
        self.watched={str(p):file_sha(p) for p in watched};self.policy_fingerprint=digest({"config":cfg,"files":self.watched})
        self.identity=digest({"mode":cfg["mode"],"python":str(self.python),"python_sha256":file_sha(self.python),"code":{p:file_sha(p) for p in self.code_files},"root":str(root),"binding":b,"record_visibility":self.visibility});self.runner=runner or subprocess.run

    def _code_files(self):return {str(p) for p in (self.repo/"source").rglob("*.py")}
    def _unchanged(self):
        if self._code_files()!=self.code_files or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("identity_records_configuration_changed")

    def _db_id(self):
        info=self.database.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("identity_records_store_identity_invalid")
        identity=[str(info.st_dev),str(info.st_ino)]
        try:
            with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True,timeout=.1)) as db:
                if (db.execute("PRAGMA application_id").fetchone()[0]!=0x53535249 or db.execute("PRAGMA user_version").fetchone()[0]!=3
                        or {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}!={"sources","identity_events","record_revisions"}):raise PipelineError("identity_records_store_continuity_failed")
        except sqlite3.Error as error:
            raise PipelineError("identity_records_store_busy" if getattr(error,"sqlite_errorcode",0)&255 in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} else "identity_records_store_continuity_failed") from None
        now=self.database.lstat()
        if identity!=[str(now.st_dev),str(now.st_ino)]:raise PipelineError("identity_records_store_continuity_failed")
        return identity

    def _files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("identity_records_state_path_invalid")
            for name in files:
                path=Path(folder)/name;info=path.lstat()
                if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("identity_records_state_path_invalid")
                result[str(path.relative_to(self.state))]={"identity":[str(info.st_dev),str(info.st_ino)],"sha256":file_sha(path)}
        return result

    @contextmanager
    def _owned(self,*,allow_create=False):
        marker=self.state/"synthetic-identity-records-owner.json"
        for name in (marker.name,"pipeline.lock","event-receipts.jsonl",self.database.name,"imports"):
            path=self.state/name
            if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or path.exists() and name!="imports" and not S_ISREG(path.lstat().st_mode):raise PipelineError("identity_records_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("identity_records_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as f:f.write(encoded({"mode":"synthetic_identity_records_only","identity_sha256":self.identity,"store_file_id":None,"files":{}}))
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
            except OSError:raise PipelineError("identity_records_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","identity_sha256","store_file_id","files"} or owner["mode"]!="synthetic_identity_records_only" or owner["identity_sha256"]!=self.identity:raise PipelineError("identity_records_state_binding_mismatch")
                if self._files()!=owner["files"]:raise PipelineError("identity_records_handoff_continuity_failed")
                if owner["store_file_id"] is None and self.database.exists():raise PipelineError("identity_records_unattested_store")
                if owner["store_file_id"] is not None and (not self.database.exists() or self._db_id()!=owner["store_file_id"]):raise PipelineError("identity_records_store_continuity_failed")
                try:yield
                finally:
                    owner["files"]=self._files()
                    if self.database.exists():
                        identity=self._db_id()
                        if owner["store_file_id"] is None and not allow_create:raise PipelineError("identity_records_unattested_store")
                        if owner["store_file_id"] not in (None,identity):raise PipelineError("identity_records_store_continuity_failed")
                        owner["store_file_id"]=identity
                    temp=self.state/("owner-"+uuid4().hex+".tmp");temp.write_bytes(encoded(owner));os.replace(temp,marker)
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _registered(self,alias):
        item=self.events.get(alias)
        if not isinstance(item,dict) or set(item)!={"entity_kind","person_id","record","declaration"} or item["entity_kind"] not in {"person","platform_account"}:raise PipelineError("identity_records_raw_annotation_required")
        r=item["record"]
        if (not isinstance(r,dict) or set(r)!=RECORD or r["schema_version"]!="1.0.1" or r["record_kind"]!="raw_event" or r["scope"]!=self.binding["scope"] or r["source_id"]!=self.binding["source_id"] or r["access_scope"]!=self.visibility
                or any(not token(r[k]) or not r[k].startswith("synthetic-") for k in ("source_event_id","record_id")) or type(r["revision"]) is not int or r["revision"]<1
                or not isinstance(r["text"],str) or len(r["text"])>16384 or not isinstance(r["quality_flags"],list) or "synthetic" not in r["quality_flags"]):raise PipelineError("identity_records_synthetic_record_not_bound")
        if (item["entity_kind"]=="person" and (item["person_id"] not in self.binding["person_ids"] or r["actor_account_id"] is not None)
                or item["entity_kind"]=="platform_account" and (item["person_id"] is not None or r["actor_account_id"] not in self.binding["account_ids"])):raise PipelineError("identity_records_explicit_annotation_invalid")
        refs=r["source_refs"]
        if (not isinstance(refs,list) or len(refs)!=1 or not isinstance(refs[0],dict) or set(refs[0])!={"source_id","record_id","revision","locator","transformation_version"} or any(refs[0][k]!=r[k] for k in ("source_id","record_id","revision"))
                or refs[0]["transformation_version"] is not None or not isinstance(refs[0]["locator"],str) or not refs[0]["locator"].startswith("synthetic://")):raise PipelineError("identity_records_source_reference_required")
        d=item["declaration"];payload={k:item[k] for k in ("entity_kind","person_id","record")}
        if (not isinstance(d,dict) or set(d)!={"status","source_id","scope","payload_sha256","basis"} or d["status"]!="declared" or d["source_id"]!=self.binding["source_id"] or d["scope"]!=self.binding["scope"]
                or d["payload_sha256"]!=digest(payload) or not isinstance(d["basis"],str) or not d["basis"].strip()):raise PipelineError("identity_records_declaration_not_confirmed")
        return item

    def _permit(self,write):
        decision=self.purpose["write_decision" if write else "read_decision"] or {}
        if decision.get("allowed") is not True:raise PipelineError("identity_records_source_write_not_allowed" if write else "identity_records_source_read_not_allowed")
        for name in (("local_cache",) if write else ("local_cache","search_index")):
            grant=self.purpose["uses"].get(name,{})
            if not isinstance(grant,dict) or grant.get("status")!="allowed" or not isinstance(grant.get("basis"),str) or not grant["basis"].strip():raise PipelineError("identity_records_"+name+"_not_allowed")

    def _call(self,args):return self.runner([str(self.python),"-B","-X","utf8","-m","source.ssr_identity","--db",str(self.database),*args],cwd=str(self.repo),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)
    def _decode(self,child):
        if len(child.stdout)>2_000_000:raise PipelineError("identity_records_native_output_oversized")
        return load_object(child.stdout)
    def _receipt(self,child,command):
        value=self._decode(child)
        if value.get("command")!=command:raise PipelineError("identity_records_native_receipt_invalid")
        if child.returncode!=0:
            error=value.get("error",{});reason=error.get("reason_code")
            if set(value)!={"command","error"} or not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None:raise PipelineError("identity_records_native_receipt_invalid")
            return None,reason
        if set(value)!={"command","data"}:raise PipelineError("identity_records_native_receipt_invalid")
        return value["data"],None
    def _job(self,payload):
        p=self.state/"imports"/("request-"+digest(payload)[:16]+"-"+uuid4().hex[:12]);p.mkdir(parents=True);return p
    def _originals(self):
        values=[]
        for alias in self.events:
            try:values.append(self._registered(alias)["record"])
            except PipelineError:pass
        return values
    def _validate_records(self,records):
        originals=self._originals()
        if not isinstance(records,list) or any(not isinstance(r,dict) or r not in originals for r in records):raise PipelineError("identity_records_readback_not_declared_original")
    def _query_result(self,child,request_id):
        value=self._decode(child)
        if value.get("schema_version")!="1.0.1" or value.get("request_id")!=request_id or value.get("domain")!="person_profile" or value.get("status") not in STATUSES or not isinstance(value.get("evidence"),list):raise PipelineError("identity_records_native_result_invalid")
        reason=value.get("reason_code")
        if reason is not None and (not isinstance(reason,str) or re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) is None):raise PipelineError("identity_records_native_result_invalid")
        originals=self._originals()
        for item in value["evidence"]:
            if (not isinstance(item,dict) or set(item)!=EVIDENCE or item["layer"]!="raw" or item["source_kind"]!="synthetic" or not any(all(item[k]==r[k] for k in RECORD-{"schema_version","record_kind","access_scope"}) for r in originals)):raise PipelineError("identity_records_evidence_not_declared_original")
        return value

    def run(self,payload):
        if not isinstance(payload,dict) or not token(payload.get("request_id")) or payload.get("synthetic") is not True or payload.get("operation") not in {"ingest","query","history","get"}:raise RetrievalInputError("synthetic_identity_records_request_invalid")
        operation=payload["operation"];fields={"request_id","synthetic","operation","events" if operation=="ingest" else "query"}
        if (set(payload)!=fields or operation=="ingest" and (not isinstance(payload["events"],list) or len(payload["events"])>20 or any(not token(a) for a in payload["events"])) or operation!="ingest" and not token(payload["query"])):raise RetrievalInputError("synthetic_identity_records_request_invalid")
        output={"request_id":payload["request_id"],"operation":operation,"mode":"synthetic_identity_records_only","status":"held","model_invoked":False,"network_used":False,"public_output":"not_attempted"}
        if operation=="ingest":
            output["items"]=[]
            for index,alias in enumerate(payload["events"],1):
                item={"index":index,"status":"held","stage":"input","reason_code":None,"storage_outcome":"not_attempted"};output["items"].append(item)
                try:
                    value=self._registered(alias);record=value["record"];self._permit(True);self._unchanged()
                    if not self.database.exists() and not self.writer["write_scopes"]:raise PipelineError("identity_records_bootstrap_write_not_allowed")
                    with self._owned(allow_create=True):
                        job=self._job({**payload,"item":index})
                        for n,v in (("record.json",record),("writer.json",self.writer),("visibility.json",self.visibility),("scope.json",self.binding["scope"])):(job/n).write_bytes(encoded(v))
                        item["stage"]="bootstrap"
                        if not self.database.exists():
                            child=self._call(["init"]);data,reason=self._receipt(child,"init")
                            if reason:item.update(reason_code=reason,native_exit=child.returncode);continue
                            if data!={"path":str(self.database.resolve()),"schema_version":3}:raise PipelineError("identity_records_init_not_confirmed")
                        item["stage"]="source";child=self._call(["source-configure","--scope-file",str(job/"scope.json"),"--source-id",self.binding["source_id"],"--source-kind","synthetic","--source-access-file",str(job/"visibility.json"),"--access-file",str(job/"writer.json")]);data,reason=self._receipt(child,"source-configure")
                        if reason:item.update(reason_code=reason,native_exit=child.returncode);continue
                        if data!={"scope":self.binding["scope"],"source_id":self.binding["source_id"],"source_kind":"synthetic"}:raise PipelineError("identity_records_source_not_confirmed")
                        command="person-ingest" if value["entity_kind"]=="person" else "account-ingest";args=[command,"--record-file",str(job/"record.json"),"--access-file",str(job/"writer.json")]
                        if command=="person-ingest":args.extend(["--person-id",value["person_id"]])
                        item.update(stage="store",storage_outcome="unknown");child=self._call(args);data,reason=self._receipt(child,command);item.update(native_exit=child.returncode,reason_code=reason);self._unchanged()
                        if reason:
                            item.update(status="uncertain" if reason=="storage_error" else "held",storage_outcome="unknown" if reason=="storage_error" else "rejected")
                        else:
                            if (not isinstance(data,dict) or set(data)!={"stored","record_id","revision"} or type(data["stored"]) is not bool or data["record_id"]!=record["record_id"] or data["revision"]!=record["revision"]):raise PipelineError("identity_records_write_receipt_invalid")
                            item.update(status="stored" if data["stored"] else "duplicate",storage_outcome="confirmed")
                except (PipelineError,OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:item.update(status="uncertain" if item["storage_outcome"]=="unknown" else "held",reason_code=getattr(error,"code","identity_records_local_operation_failed"))
            counts={k:sum(i["status"]==k for i in output["items"]) for k in ("stored","duplicate","held","uncertain")};ok=counts["stored"]+counts["duplicate"];output.update(counts=counts,status="empty" if not output["items"] else "complete" if ok==len(output["items"]) else "partial" if ok else "held")
        else:
            output.update(record_count=None,reason_code=None)
            try:
                selected=self.queries.get(payload["query"])
                if selected is None or selected["operation"]!=operation:raise PipelineError("identity_records_registered_query_required")
                self._permit(False);self._unchanged()
                with self._owned():
                    before=file_sha(self.database) if self.database.exists() else None;job=self._job(payload)
                    for n,v in (("reader.json",self.reader),("scope.json",self.binding["scope"])):(job/n).write_bytes(encoded(v))
                    if operation=="query":
                        q=deepcopy(selected["query"]);q["request_id"]=payload["request_id"];(job/"query.json").write_bytes(encoded(q));child=self._call(["query","--query-file",str(job/"query.json"),"--access-file",str(job/"reader.json")]);value=self._query_result(child,payload["request_id"]);status=value["status"];reason=value.get("reason_code");count=len(value["evidence"])
                    else:
                        command="record-history" if operation=="history" else "record-get";args=[command,"--scope-file",str(job/"scope.json"),"--source-id",self.binding["source_id"],"--source-event-id",selected["source_event_id"],"--access-file",str(job/"reader.json")]
                        if operation=="get" and selected["revision"] is not None:args.extend(["--revision",str(selected["revision"])])
                        child=self._call(args);data,reason=self._receipt(child,command);value=self._decode(child);status="error" if reason else "ok"
                        if not reason:
                            records=data if operation=="history" else [] if data is None else [data];self._validate_records(records);count=len(records);status="ok" if count else "no_match"
                        else:count=None
                    output.update(native_exit=child.returncode,native_status=status,reason_code=reason);self._unchanged()
                    if (file_sha(self.database) if self.database.exists() else None)!=before:raise PipelineError("identity_records_source_changed_during_read")
                    if child.returncode==0 and status in {"ok","no_match"}:(job/"native-result.json").write_bytes(encoded(value));output.update(status=status,record_count=count,provenance_sha256=digest(value))
            except (PipelineError,OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:output.update(status="held",record_count=None,reason_code=getattr(error,"code","identity_records_local_operation_failed"))
        if self.state.exists():
            try:
                with self._owned():JsonlLog(self.state/"event-receipts.jsonl").append({**output,"identity_sha256":self.identity,"policy_sha256":self.policy_fingerprint,"ai_input":"not_used","ai_train":"not_used","person_account_link":"not_used"})
            except (PipelineError,OSError,ValueError,TypeError,KeyError) as error:
                if operation!="ingest":output.update(status="held",record_count=None,reason_code=getattr(error,"code","identity_records_receipt_failed"))
                else:output["receipt_status"]="held"
        return output
