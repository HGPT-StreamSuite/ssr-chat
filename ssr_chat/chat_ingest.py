"""Synthetic Record -> task-20 CLI -> task-7 CLI; no Twitch, reads, AI or sends."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from stat import S_ISREG
import subprocess
import tempfile
from uuid import uuid4

from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError, load_object
from .stores import JsonlLog

SCOPE = ("domain","platform","channel_id","game_id","session_id","content_version")
RECORD = {"schema_version","source_id","source_event_id","record_id","revision","event_time","received_at","actor_account_id",
          "text","record_kind","quality_flags","source_refs","scope","access_scope"}
FLAGS = getattr(subprocess,"CREATE_NO_WINDOW",0)


def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
def digest(value): return hashlib.sha256(encoded(value)).hexdigest()
def file_sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def token(value): return isinstance(value,str) and re.fullmatch(r"[A-Za-z0-9._-]{1,100}",value) is not None


def local(value,base):
    if not isinstance(value,str) or not value or "://" in value or value.startswith(("\\\\","//")):
        raise PipelineError("chat_ingest_local_path_required")
    path=Path(value)
    return (path if path.is_absolute() else base/path).resolve()


def access(value,selector):
    return (isinstance(value,dict) and set(value)=={"read_scopes","write_scopes"}
            and all(isinstance(value[k],list) and (value[k]==[] or value[k]==[selector]) for k in value))


class ChatIngestPipeline:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve(); cfg=load_object(path.read_bytes())
        required={"schema_version","mode","python","normalizer_repo","store_repo","normalizer_context","store_policy","state_root","binding"}
        if set(cfg)!=required or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_chat_ingest_only":
            raise PipelineError("chat_ingest_configuration_invalid")
        self.python=local(cfg["python"],path.parent)
        self.normalizer=local(cfg["normalizer_repo"],path.parent); self.store=local(cfg["store_repo"],path.parent)
        context_path=local(cfg["normalizer_context"],path.parent); policy_path=local(cfg["store_policy"],path.parent)
        root=local(cfg["state_root"],path.parent); self.state=Path(state_dir).absolute()
        if self.state.is_symlink() or not self.state.resolve().is_relative_to(root) or self.state.resolve()==root:
            raise PipelineError("chat_ingest_state_outside_dedicated_root")
        self.state=self.state.resolve()
        if not self.python.is_file() or not (self.normalizer/"source/hgpt_ssr/__main__.py").is_file() or not (self.store/"hgpt_ssr.py").is_file():
            raise PipelineError("chat_ingest_component_missing")
        self.context=load_object(context_path.read_bytes()); self.policy=load_object(policy_path.read_bytes()); self.binding=deepcopy(cfg["binding"])
        binding=self.binding
        if (not isinstance(binding,dict) or set(binding)!={"source_id","actor_account_id","scope"}
                or not token(binding["source_id"]) or not token(binding["actor_account_id"])):
            raise PipelineError("chat_ingest_binding_invalid")
        scope=binding["scope"]
        if (not isinstance(scope,dict) or set(scope)!=set(SCOPE) or scope["domain"]!="chat_history"
                or any(not isinstance(scope[k],str) or not scope[k] for k in ("platform","channel_id","session_id"))
                or any(scope[k] is not None and (not isinstance(scope[k],str) or not scope[k]) for k in ("game_id","content_version"))):
            raise PipelineError("chat_ingest_scope_invalid")
        selector={"domain":"chat_history",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        source=binding["source_id"]
        if (set(self.context)!={"schema_version","adapter_kind","source_kind","caller_access_scope","sources"}
                or self.context["schema_version"]!="1.0.1" or self.context["adapter_kind"]!="fixture" or self.context["source_kind"]!="synthetic"
                or not access(self.context["caller_access_scope"],selector) or self.context["caller_access_scope"]["read_scopes"]
                or not isinstance(self.context["sources"],dict) or set(self.context["sources"])!={source}):
            raise PipelineError("chat_ingest_normalizer_authority_invalid")
        authority=self.context["sources"][source]
        if (not isinstance(authority,dict) or set(authority)!={"access_scope","write_allowed","read_allowed","policy"}
                or not access(authority["access_scope"],selector) or type(authority["write_allowed"]) is not bool or authority["read_allowed"] is not False):
            raise PipelineError("chat_ingest_source_authority_invalid")
        usage=authority["policy"]
        if not isinstance(usage,dict) or set(usage)!={"uses"} or not isinstance(usage["uses"],dict) or set(usage["uses"])-{"local_cache","search_index","ai_input","ai_train"}:
            raise PipelineError("chat_ingest_purpose_policy_invalid")
        for grant in usage["uses"].values():
            if not isinstance(grant,dict) or set(grant)!={"status","basis"} or not isinstance(grant["status"],str) or grant["status"] not in {"allowed","denied","unknown","prohibited"} or not isinstance(grant["basis"],str) or not 1<=len(grant["basis"])<=500:
                raise PipelineError("chat_ingest_purpose_policy_invalid")
        self.cache_grant=deepcopy(usage["uses"].get("local_cache",{}))
        if (set(self.policy)!={"access_scope","operations","sources","source_access_scopes"}
                or self.policy["operations"]!=["ingest"] or self.policy["sources"]!={source:"synthetic"}
                or not access(self.policy["access_scope"],selector) or self.policy["access_scope"]["read_scopes"]
                or set(self.policy["source_access_scopes"])!={source} or not access(self.policy["source_access_scopes"][source],selector)):
            raise PipelineError("chat_ingest_store_authority_invalid")
        watched=[path,context_path,policy_path]
        for repo in (self.normalizer,self.store):
            watched += [*repo.glob("*.py"),*repo.glob("source/**/*.py")]
        if any(p.is_symlink() for p in watched): raise PipelineError("chat_ingest_symlink_not_allowed")
        self.watched={str(p):file_sha(p) for p in watched}
        self.fingerprint=digest({"config":cfg,"context":self.context,"policy":self.policy,"files":self.watched})
        self.runner=runner or subprocess.run
        self.database=self.state/"synthetic-chat.sqlite3"

    def _unchanged(self):
        if any(not Path(p).is_file() or file_sha(p)!=expected for p,expected in self.watched.items()):
            raise PipelineError("chat_ingest_configuration_changed")

    def _file_identity(self):
        info=self.database.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino: raise PipelineError("chat_ingest_store_identity_unavailable")
        return [str(info.st_dev),str(info.st_ino)]

    def _write_owner(self,owner):
        target=self.state/"synthetic-ingest-owner.json"; temp=self.state/("owner-"+uuid4().hex+".tmp")
        temp.write_bytes(encoded(owner)); os.replace(temp,target)

    @contextmanager
    def _owned_state(self):
        marker=self.state/"synthetic-ingest-owner.json"
        def check_paths():
            for name in ("synthetic-ingest-owner.json","pipeline.lock","ingest-receipts.jsonl"):
                selected=self.state/name
                if selected.is_symlink() or selected.exists() and not S_ISREG(selected.lstat().st_mode):
                    raise PipelineError("chat_ingest_state_path_invalid")
            work=self.state/"work"
            if work.is_symlink() or getattr(work,"is_junction",lambda:False)() or not work.resolve().is_relative_to(self.state):
                raise PipelineError("chat_ingest_state_path_invalid")
        check_paths()
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()): raise PipelineError("chat_ingest_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file: file.write(encoded({"schema_version":"1.0.1","mode":"synthetic_chat_ingest_only","binding_sha256":self.fingerprint,"store_file_id":None}))
            except FileExistsError: pass
        owner=load_object(marker.read_bytes())
        if set(owner)!={"schema_version","mode","binding_sha256","store_file_id"} or owner["schema_version"]!="1.0.1" or owner["mode"]!="synthetic_chat_ingest_only" or owner["binding_sha256"]!=self.fingerprint:
            raise PipelineError("chat_ingest_state_binding_mismatch")
        with (self.state/"pipeline.lock").open("a+b") as lock:
            if lock.tell()==0: lock.write(b"0"); lock.flush()
            lock.seek(0)
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError: raise PipelineError("chat_ingest_state_busy") from None
            try:
                check_paths()
                owner=load_object(marker.read_bytes())
                if owner.get("binding_sha256")!=self.fingerprint: raise PipelineError("chat_ingest_state_binding_mismatch")
                if owner["store_file_id"] is None and self.database.exists(): raise PipelineError("chat_ingest_unattested_store")
                if owner["store_file_id"] is not None and (not self.database.exists() or self._file_identity()!=owner["store_file_id"]):
                    raise PipelineError("chat_ingest_store_continuity_failed")
                yield owner
            finally:
                lock.seek(0)
                if os.name=="nt": msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else: fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _input(self,envelope):
        if not isinstance(envelope,dict) or set(envelope)!={"record","source","raw_reference","synthetic"} or envelope["synthetic"] is not True:
            return "synthetic_envelope_required"
        record=envelope["record"]
        if not isinstance(record,dict) or set(record)-RECORD or set(record)!=RECORD: return "minimal_record_fields_required"
        if (record["schema_version"]!="1.0.1" or record["record_kind"]!="raw_event" or record["quality_flags"]!=["synthetic"]
                or record["source_id"]!=self.binding["source_id"] or envelope["source"]!=self.binding["source_id"]): return "synthetic_source_not_bound"
        if record["actor_account_id"]!=self.binding["actor_account_id"] or record["scope"]!=self.binding["scope"]: return "identity_or_scope_not_bound"
        if record["access_scope"]!=self.context["sources"][self.binding["source_id"]]["access_scope"]: return "incoming_source_acl_mismatch"
        if not token(record["source_event_id"]) or not token(record["record_id"]) or type(record["revision"]) is not int or record["revision"]<1: return "stable_event_identity_required"
        if not isinstance(record["text"],str) or len(record["text"])>16_384: return "chat_text_invalid"
        try: record["text"].encode("utf-8")
        except UnicodeError: return "chat_text_invalid"
        refs=record["source_refs"]; ref=envelope["raw_reference"]
        if (not isinstance(ref,str) or not ref.startswith("synthetic://") or len(ref)>500 or not isinstance(refs,list) or len(refs)!=1
                or refs[0]!={"source_id":record["source_id"],"record_id":record["record_id"],"revision":record["revision"],"locator":ref,"transformation_version":None}):
            return "synthetic_provenance_required"
        if self.cache_grant.get("status")!="allowed": return "local_cache_not_allowed"
        return None

    def _call(self,command,cwd,*,data=None):
        return self.runner(command,cwd=str(cwd),input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20,creationflags=FLAGS)

    @staticmethod
    def _result(value,request_id):
        if (not isinstance(value,dict) or value.get("schema_version")!="1.0.1" or value.get("request_id")!=request_id or value.get("domain")!="chat_history"
                or value.get("status") not in {"ok","skipped","denied","error","missing_input","not_configured","no_match"} or value.get("evidence")!=[]):
            raise PipelineError("chat_ingest_native_result_invalid")
        reason=value.get("reason_code")
        return value["status"],reason if isinstance(reason,str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) else "native_operation_not_confirmed"

    def ingest(self,payload):
        if (not isinstance(payload,dict) or set(payload)!={"request_id","synthetic","events"} or not token(payload["request_id"])
                or payload["synthetic"] is not True or not isinstance(payload["events"],list) or len(payload["events"])>50):
            raise RetrievalInputError("synthetic_chat_batch_invalid")
        report={"request_id":payload["request_id"],"mode":"synthetic_chat_ingest_only","items":[],"model_invoked":False,"public_output":"not_attempted","transport_authenticated":False}
        for index,envelope in enumerate(payload["events"],1):
            item={"index":index,"status":"held","stage":"input","reason_code":None,"storage_outcome":"not_attempted"}
            reason=self._input(envelope)
            if reason:
                item["reason_code"]=reason; report["items"].append(item); continue
            item["record_sha256"]=digest(envelope["record"])
            request_id=payload["request_id"]+"-"+str(index)
            try:
                self._unchanged()
                with self._owned_state() as owner:
                    work=self.state/"work"; work.mkdir(exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="normalize-",dir=work) as temporary:
                        temp=Path(temporary).resolve()
                        if not temp.is_relative_to(work.resolve()): raise PipelineError("chat_ingest_work_path_invalid")
                        for name,value in (("input.json",envelope),("context.json",self.context),("policy.json",self.policy)):
                            (temp/name).write_bytes(encoded(value))
                        item["stage"]="normalizer"
                        child=self._call([str(self.python),"-B","-X","utf8","-m","source.hgpt_ssr","--input",str(temp/"input.json"),"--context",str(temp/"context.json"),"--emit","envelope","--request-id",request_id],self.normalizer)
                        if len(child.stdout)>2_000_000: raise PipelineError("chat_ingest_native_output_oversized")
                        output=load_object(child.stdout); status,reason=self._result(output.get("result"),request_id)
                        item.update(normalizer_status=status,normalizer_exit=child.returncode)
                        if status!="ok" or child.returncode!=0:
                            item["reason_code"]=reason
                        elif output.get("record")!=envelope["record"] or output.get("source")!=envelope["source"] or output.get("raw_reference")!=envelope["raw_reference"] or output.get("synthetic") is not True:
                            raise PipelineError("chat_ingest_normalized_record_changed")
                        else:
                            self._unchanged(); item.update(stage="store",storage_outcome="unknown")
                            try:
                                child=self._call([str(self.python),"-B","-X","utf8","hgpt_ssr.py","--db",str(self.database),"--policy",str(temp/"policy.json"),"ingest","--request-id",request_id,"--input","-"],self.store,data=encoded(output["record"]))
                            finally:
                                # Preserve ownership even when a committed write loses its acknowledgement.
                                if self.database.exists():
                                    current=self._file_identity()
                                    if owner["store_file_id"] is not None and owner["store_file_id"]!=current: raise PipelineError("chat_ingest_store_continuity_failed")
                                    owner["store_file_id"]=current; self._write_owner(owner)
                            if len(child.stdout)>2_000_000: raise PipelineError("chat_ingest_native_output_oversized")
                            result=load_object(child.stdout); status,reason=self._result(result,request_id)
                            item.update(store_status=status,store_exit=child.returncode,reason_code=reason)
                            if child.returncode==0 and status=="ok" and reason in {"stored","idempotent"}:
                                item.update(status="stored" if reason=="stored" else "duplicate",storage_outcome="confirmed")
                            self._unchanged()
                        JsonlLog(self.state/"ingest-receipts.jsonl").append({"request_id":request_id,**item,"binding_sha256":self.fingerprint,
                            "local_cache_grant_sha256":digest(self.cache_grant),"ai_input":"not_used","ai_train":"not_used","public_output":"not_attempted"})
            except (PipelineError,OSError,ValueError,subprocess.SubprocessError) as error:
                item.update(status="uncertain" if item["storage_outcome"]=="unknown" or item["stage"]=="store" else "held",
                            reason_code=getattr(error,"code","chat_ingest_local_operation_failed"))
            report["items"].append(item)
        counts={key:sum(item["status"]==key for item in report["items"]) for key in ("stored","duplicate","held","uncertain")}
        successful=counts["stored"]+counts["duplicate"]
        report.update(counts=counts,status="empty" if not report["items"] else "complete" if successful==len(report["items"]) else "partial" if successful else "held")
        return report
