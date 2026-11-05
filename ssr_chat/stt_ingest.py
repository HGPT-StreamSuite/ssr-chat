"""Synthetic STT text -> native task-21 -> native task-8, with isolated state."""
from contextlib import closing,contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
from stat import S_ISREG
import sqlite3
import subprocess
import tempfile
from uuid import uuid4

from .chat_ingest import SCOPE,RECORD,FLAGS,access,digest,encoded,file_sha,token,local
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

FIELDS=RECORD|{"extensions"}
STT={"segment_id","segment_revision","state","speaker_id","started_at","ended_at","source_confidence"}
STATUSES={"ok","no_match","skipped","error","missing_input","denied","not_configured"}


class SttIngestPipeline:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        fields={"schema_version","mode","python","normalizer_repo","store_repo","normalizer_policy","store_sources","store_caller","purpose_policy","state_root","binding"}
        if set(cfg)!=fields or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_stt_ingest_only":raise PipelineError("stt_ingest_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.normalizer=local(cfg["normalizer_repo"],path.parent);self.store=local(cfg["store_repo"],path.parent)
        root=local(cfg["state_root"],path.parent);selected=Path(state_dir).absolute()
        if (selected.is_symlink() or getattr(selected,"is_junction",lambda:False)() or not selected.resolve().is_relative_to(root) or selected.resolve()==root):
            raise PipelineError("stt_ingest_state_outside_dedicated_root")
        self.state=selected.resolve()
        if not self.python.is_file() or not (self.normalizer/"source/cli.py").is_file() or not (self.store/"hgpt_ssr/__main__.py").is_file():raise PipelineError("stt_ingest_component_missing")
        paths={k:local(cfg[k],path.parent) for k in ("normalizer_policy","store_sources","store_caller","purpose_policy")}
        self.policy=load_object(paths["normalizer_policy"].read_bytes());self.sources=load_object(paths["store_sources"].read_bytes())
        self.caller=load_object(paths["store_caller"].read_bytes());self.purpose=load_object(paths["purpose_policy"].read_bytes());self.binding=deepcopy(cfg["binding"])
        binding=self.binding
        if (not isinstance(binding,dict) or set(binding)!={"source_id","actor_account_id","scope"} or not token(binding["source_id"]) or not binding["source_id"].startswith("synthetic-")
                or binding["actor_account_id"] is not None and (not token(binding["actor_account_id"]) or not binding["actor_account_id"].startswith("synthetic-"))):
            raise PipelineError("stt_ingest_binding_invalid")
        scope=binding["scope"]
        if (not isinstance(scope,dict) or set(scope)!=set(SCOPE) or scope["domain"]!="stt_transcript" or scope["platform"]!="synthetic-stt"
                or any(not isinstance(scope[k],str) or not scope[k] for k in ("channel_id","session_id"))
                or any(scope[k] is not None and (not isinstance(scope[k],str) or not scope[k]) for k in ("game_id","content_version"))):raise PipelineError("stt_ingest_scope_invalid")
        selector={"domain":"stt_transcript",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        source=binding["source_id"]
        if (set(self.policy)!={"schema_version","low_confidence_threshold","caller_access_scope","grants"} or self.policy["schema_version"]!="1.0.1"
                or type(self.policy["low_confidence_threshold"]) not in (int,float) or not 0<=self.policy["low_confidence_threshold"]<=1
                or not access(self.policy["caller_access_scope"],selector) or self.policy["caller_access_scope"]["read_scopes"]
                or not isinstance(self.policy["grants"],list) or len(self.policy["grants"])!=1):raise PipelineError("stt_ingest_normalizer_authority_invalid")
        grant=self.policy["grants"][0]
        if (not isinstance(grant,dict) or set(grant)!={"source_id","source_kind","access_scope","existing_write_allowed"} or grant["source_id"]!=source
                or grant["source_kind"]!="synthetic" or not access(grant["access_scope"],selector)
                or grant["existing_write_allowed"] is not None and type(grant["existing_write_allowed"]) is not bool):raise PipelineError("stt_ingest_normalizer_source_invalid")
        self.visibility=deepcopy(grant["access_scope"])
        if (set(self.sources)!={"sources"} or not isinstance(self.sources["sources"],list) or len(self.sources["sources"])!=1
                or not access(self.caller,selector) or self.caller["read_scopes"]):raise PipelineError("stt_ingest_store_authority_invalid")
        authoritative=self.sources["sources"][0]
        if (not isinstance(authoritative,dict) or set(authoritative)!={"source_id","source_kind","scopes","operations","policy_ref","access_scope"}
                or authoritative["source_id"]!=source or authoritative["source_kind"]!="synthetic" or authoritative["scopes"]!=[scope]
                or authoritative["operations"] not in ([],["ingest"]) or not access(authoritative["access_scope"],selector)
                or not isinstance(authoritative["policy_ref"],str) or not authoritative["policy_ref"].startswith("synthetic:") or len(authoritative["policy_ref"])>500):raise PipelineError("stt_ingest_store_source_invalid")
        if (set(self.purpose)!={"source_id","scope","uses"} or self.purpose["source_id"]!=source or self.purpose["scope"]!=scope
                or not isinstance(self.purpose["uses"],dict) or set(self.purpose["uses"])-{"local_cache","search_index","ai_input","ai_train"}):raise PipelineError("stt_ingest_purpose_policy_invalid")
        for use in self.purpose["uses"].values():
            if (not isinstance(use,dict) or set(use)!={"status","basis"} or use["status"] not in {"allowed","denied","unknown","prohibited"}
                    or not isinstance(use["basis"],str) or not 1<=len(use["basis"])<=500):raise PipelineError("stt_ingest_purpose_policy_invalid")
        self.cache_grant=deepcopy(self.purpose["uses"].get("local_cache",{}))
        self.code_files=self._code_files()
        watched=[path,self.python,*paths.values(),*map(Path,self.code_files)]
        if any(p.is_symlink() for p in watched):raise PipelineError("stt_ingest_symlink_not_allowed")
        self.watched={str(p):file_sha(p) for p in watched};self.fingerprint=digest({"config":cfg,"files":self.watched})
        self.runner=runner or subprocess.run;self.database=self.state/"synthetic-stt.sqlite3"

    def _code_files(self):
        return {str(p) for repo,folder in ((self.normalizer,"source"),(self.store,"hgpt_ssr")) for p in (repo/folder).rglob("*.py")}

    def _unchanged(self):
        if self._code_files()!=self.code_files or any(not Path(p).is_file() or file_sha(p)!=expected for p,expected in self.watched.items()):raise PipelineError("stt_ingest_configuration_changed")

    def _file_identity(self):
        info=self.database.lstat()
        if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("stt_ingest_store_identity_unavailable")
        return [str(info.st_dev),str(info.st_ino)]

    def _verified_store_identity(self):
        identity=self._file_identity()
        try:
            with closing(sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True,timeout=.1)) as database:
                if (database.execute("PRAGMA application_id").fetchone()[0]!=0x48535352
                        or database.execute("PRAGMA user_version").fetchone()[0]!=3
                        or {r[0] for r in database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}!={"raw_events","ingest_attempts"}):
                    raise PipelineError("stt_ingest_store_continuity_failed")
        except sqlite3.Error as error:
            code=getattr(error,"sqlite_errorcode",0)&255
            raise PipelineError("stt_ingest_store_busy" if code in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} else "stt_ingest_store_continuity_failed") from None
        if identity!=self._file_identity():raise PipelineError("stt_ingest_store_continuity_failed")
        return identity

    def _write_owner(self,owner):
        target=self.state/"synthetic-stt-owner.json";temp=self.state/("owner-"+uuid4().hex+".tmp")
        temp.write_bytes(encoded(owner));os.replace(temp,target)

    @contextmanager
    def _owned_state(self):
        marker=self.state/"synthetic-stt-owner.json"
        def check_paths():
            for name in ("synthetic-stt-owner.json","pipeline.lock","ingest-receipts.jsonl"):
                selected=self.state/name
                if selected.is_symlink() or selected.exists() and not S_ISREG(selected.lstat().st_mode):raise PipelineError("stt_ingest_state_path_invalid")
            work=self.state/"work"
            if work.is_symlink() or getattr(work,"is_junction",lambda:False)() or not work.resolve().is_relative_to(self.state):raise PipelineError("stt_ingest_state_path_invalid")
        check_paths()
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("stt_ingest_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file:file.write(encoded({"schema_version":"1.0.1","mode":"synthetic_stt_ingest_only","binding_sha256":self.fingerprint,"store_file_id":None}))
            except FileExistsError:pass
        owner=load_object(marker.read_bytes())
        if (set(owner)!={"schema_version","mode","binding_sha256","store_file_id"} or owner["schema_version"]!="1.0.1"
                or owner["mode"]!="synthetic_stt_ingest_only" or owner["binding_sha256"]!=self.fingerprint):raise PipelineError("stt_ingest_state_binding_mismatch")
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
            except OSError:raise PipelineError("stt_ingest_state_busy") from None
            try:
                check_paths();owner=load_object(marker.read_bytes())
                if owner.get("binding_sha256")!=self.fingerprint:raise PipelineError("stt_ingest_state_binding_mismatch")
                if owner["store_file_id"] is None and self.database.exists():raise PipelineError("stt_ingest_unattested_store")
                if owner["store_file_id"] is not None and (not self.database.exists() or self._verified_store_identity()!=owner["store_file_id"]):raise PipelineError("stt_ingest_store_continuity_failed")
                yield owner
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _input(self,record):
        if not isinstance(record,dict) or set(record)!=FIELDS:return "minimal_stt_record_fields_required"
        if record["schema_version"]!="1.0.1" or record["record_kind"]!="raw_event" or record["source_id"]!=self.binding["source_id"]:return "synthetic_stt_source_not_bound"
        if record["actor_account_id"]!=self.binding["actor_account_id"] or record["scope"]!=self.binding["scope"]:return "stt_identity_or_scope_not_bound"
        if record["access_scope"]!=self.visibility:return "incoming_stt_source_acl_mismatch"
        if any(not token(record[k]) or not record[k].startswith("synthetic-") for k in ("source_event_id","record_id")):return "synthetic_stt_event_identity_required"
        if record["revision"] is None:return "unknown_revision"
        if type(record["revision"]) is not int or record["revision"]<1:return "invalid_revision"
        if not isinstance(record["text"],str) or not record["text"].strip() or len(record["text"])>16_384:return "stt_text_invalid"
        try:encoded(record)
        except (UnicodeError,ValueError,TypeError,RecursionError):return "stt_record_json_invalid"
        flags=record["quality_flags"]
        if not isinstance(flags,list) or not all(isinstance(f,str) and f for f in flags) or "synthetic" not in flags or len(set(flags))!=len(flags):return "synthetic_stt_flag_required"
        refs=record["source_refs"]
        if (not isinstance(refs,list) or len(refs)!=1 or not isinstance(refs[0],dict) or set(refs[0])!={"source_id","record_id","revision","locator","transformation_version"}
                or any(refs[0][k]!=record[k] for k in ("source_id","record_id","revision")) or refs[0]["transformation_version"] is not None
                or not isinstance(refs[0]["locator"],str) or not refs[0]["locator"].startswith("synthetic://") or len(refs[0]["locator"])>500):return "synthetic_stt_provenance_required"
        extensions=record["extensions"]
        if not isinstance(extensions,dict) or set(extensions)!={"stt"} or not isinstance(extensions["stt"],dict) or set(extensions["stt"])!=STT:return "stt_metadata_required"
        stt=extensions["stt"]
        if stt["segment_revision"] is None:return "unknown_revision"
        if stt["segment_id"]!=record["source_event_id"] or type(stt["segment_revision"]) is not int or stt["segment_revision"]!=record["revision"]:return "stt_segment_identity_mismatch"
        if self.cache_grant.get("status")!="allowed":return "local_cache_not_allowed"
        return None

    def _normalized(self,record):
        expected=deepcopy(record);stt=record["extensions"]["stt"];flags=[stt["state"]]
        if stt["speaker_id"] is None:flags.append("speaker_unknown")
        if stt["source_confidence"] is not None and stt["source_confidence"]<self.policy["low_confidence_threshold"]:flags.append("low_confidence")
        for flag in flags:
            if flag not in expected["quality_flags"]:expected["quality_flags"].append(flag)
        return expected

    @staticmethod
    def _result(value,request_id):
        if (not isinstance(value,dict) or value.get("schema_version")!="1.0.1" or value.get("request_id")!=request_id or value.get("domain")!="stt_transcript"
                or value.get("status") not in STATUSES or value.get("evidence")!=[]):raise PipelineError("stt_ingest_native_result_invalid")
        reason=value.get("reason_code")
        return value["status"],reason if isinstance(reason,str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}",reason) else "native_operation_not_confirmed"

    def _call(self,command,cwd,*,data=None):
        return self.runner(command,cwd=str(cwd),input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)

    def ingest(self,payload):
        if (not isinstance(payload,dict) or set(payload)!={"request_id","synthetic","events"} or not token(payload["request_id"]) or payload["synthetic"] is not True
                or not isinstance(payload["events"],list) or len(payload["events"])>50):raise RetrievalInputError("synthetic_stt_batch_invalid")
        report={"request_id":payload["request_id"],"mode":"synthetic_stt_ingest_only","items":[],"model_invoked":False,"public_output":"not_attempted","audio_used":False,"transport_authenticated":False}
        for index,record in enumerate(payload["events"],1):
            item={"index":index,"status":"held","stage":"input","reason_code":None,"storage_outcome":"not_attempted"};report["items"].append(item)
            reason=self._input(record)
            if reason:item["reason_code"]=reason;continue
            item["input_sha256"]=digest(record);request_id=payload["request_id"]+"-"+str(index)
            try:
                self._unchanged()
                with self._owned_state() as owner:
                    work=self.state/"work";work.mkdir(exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="normalize-",dir=work) as temporary:
                        temp=Path(temporary).resolve()
                        if not temp.is_relative_to(work.resolve()):raise PipelineError("stt_ingest_work_path_invalid")
                        for name,value in (("policy.json",self.policy),("sources.json",self.sources),("caller.json",self.caller)):(temp/name).write_bytes(encoded(value))
                        item["stage"]="normalizer"
                        child=self._call([str(self.python),"-B","-X","utf8","-m","source.cli","--policy",str(temp/"policy.json"),"--request-id",request_id,"--output","record"],self.normalizer,data=encoded(record)+b"\n")
                        if len(child.stdout)>2_000_000:raise PipelineError("stt_ingest_native_output_oversized")
                        normalized=load_object(child.stdout);item["normalizer_exit"]=child.returncode
                        if set(normalized)!=FIELDS:
                            status,reason=self._result(normalized,request_id);item.update(normalizer_status=status,reason_code=reason)
                        elif child.returncode!=0:raise PipelineError("stt_ingest_native_result_invalid")
                        elif normalized!=self._normalized(record):raise PipelineError("stt_ingest_normalized_record_changed")
                        else:
                            item.update(normalizer_status="ok",normalized_sha256=digest(normalized));self._unchanged();item.update(stage="store",storage_outcome="unknown")
                            try:
                                child=self._call([str(self.python),"-B","-X","utf8","-m","hgpt_ssr","--db",str(self.database),"--sources",str(temp/"sources.json"),"--caller-access",str(temp/"caller.json"),"ingest","--request-id",request_id,"--input","-"],self.store,data=encoded(normalized))
                            finally:
                                if self.database.exists():
                                    current=self._verified_store_identity()
                                    if owner["store_file_id"] is not None and owner["store_file_id"]!=current:raise PipelineError("stt_ingest_store_continuity_failed")
                                    owner["store_file_id"]=current;self._write_owner(owner)
                            if len(child.stdout)>2_000_000:raise PipelineError("stt_ingest_native_output_oversized")
                            status,reason=self._result(load_object(child.stdout),request_id);item.update(store_status=status,store_exit=child.returncode,reason_code=reason)
                            if child.returncode==0 and status=="ok" and reason in {"stored","idempotent"}:item.update(status="stored" if reason=="stored" else "duplicate",storage_outcome="confirmed")
                            self._unchanged()
                        JsonlLog(self.state/"ingest-receipts.jsonl").append({"request_id":request_id,**item,"binding_sha256":self.fingerprint,"local_cache_grant_sha256":digest(self.cache_grant),
                            "ai_input":"not_used","ai_train":"not_used","audio":"not_used","public_output":"not_attempted"})
            except (PipelineError,OSError,ValueError,TypeError,subprocess.SubprocessError) as error:
                item.update(status="uncertain" if item["storage_outcome"]=="unknown" or item["stage"]=="store" else "held",reason_code=getattr(error,"code","stt_ingest_local_operation_failed"))
        counts={key:sum(item["status"]==key for item in report["items"]) for key in ("stored","duplicate","held","uncertain")};successful=counts["stored"]+counts["duplicate"]
        report.update(counts=counts,status="empty" if not report["items"] else "complete" if successful==len(report["items"]) else "partial" if successful else "held")
        return report
