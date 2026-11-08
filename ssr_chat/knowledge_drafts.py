"""Owned synthetic channel current view -> native knowledge-draft API; no publication."""
from contextlib import contextmanager
from copy import deepcopy
import os
from pathlib import Path
import re
from stat import S_ISREG
import subprocess
from urllib.parse import urlencode
from uuid import uuid4

from .chat_ingest import SCOPE,RECORD,FLAGS,access,digest,encoded,file_sha,token,local
from .channel_ingest import ChannelIngestPipeline,EVIDENCE
from .modular_api import RetrievalInputError
from .modular_retrieval import PipelineError,load_object
from .stores import JsonlLog

# Fixed trusted host bridge. The child has the native candidate's own working
# directory/import namespace; user input cannot select Python code or a module.
WORKER='''import json,sys
from pathlib import Path
from source.hgpt_ssr.engine import organize
from source.hgpt_ssr.hooks import configured_hook
bundle=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
purpose=bundle["purpose"];calls=[]
def read_policy(context):
    decision=purpose["organizer_read_decision"]
    allowed=(decision is not None and decision["allowed"] is True and context["operation"]=="read" and context["source_kind"]=="synthetic"
             and context["record"]["source_id"]==purpose["source_id"] and dict(context["record"]["scope"])==purpose["scope"]
             and all(purpose[name]["status"]=="allowed" and bool(purpose[name]["basis"].strip()) for name in ("local_cache","knowledge_drafts")))
    calls.append({"operation":context["operation"],"source_kind":context["source_kind"],"allowed":allowed})
    return allowed
audit={}
result=organize(bundle["query"],bundle["packet"],trusted_access=bundle["access"],hook=configured_hook(bundle["hook"]) if bundle["hook"] is not None else None,
                caller_marks=bundle["marks"],audit=audit,read_policy=read_policy if purpose["organizer_read_decision"] is not None else None)
print(json.dumps({"result":result,"audit":audit,"policy_decisions":calls},ensure_ascii=False,allow_nan=False))
raise SystemExit(0 if result["status"] in ("ok","no_match","skipped") else 1)
'''


class KnowledgeDraftGateway:
    def __init__(self,config_path,state_dir,*,runner=None):
        path=Path(config_path).resolve();cfg=load_object(path.read_bytes())
        fields={"schema_version","mode","python","draft_repo","source_pipeline","source_state_dir","source_snapshot_sha256","reader_context","draft_access","purpose_policy","hook","caller_marks","state_root","records"}
        if set(cfg)!=fields or cfg["schema_version"]!="1.0.1" or cfg["mode"]!="synthetic_knowledge_drafts_only":raise PipelineError("knowledge_drafts_configuration_invalid")
        self.python=local(cfg["python"],path.parent);self.repo=local(cfg["draft_repo"],path.parent);root=local(cfg["state_root"],path.parent);state=Path(state_dir).absolute()
        if state.is_symlink() or getattr(state,"is_junction",lambda:False)() or not state.resolve().is_relative_to(root) or state.resolve()==root:raise PipelineError("knowledge_drafts_state_outside_dedicated_root")
        self.state=state.resolve();self.source=ChannelIngestPipeline(local(cfg["source_pipeline"],path.parent),local(cfg["source_state_dir"],path.parent));self.binding=deepcopy(self.source.binding);scope=self.binding["scope"];source=self.binding["source_id"]
        if self.python!=self.source.python or not (self.repo/"source/hgpt_ssr/engine.py").is_file():raise PipelineError("knowledge_drafts_component_missing")
        self.snapshot=cfg["source_snapshot_sha256"];self.hook=deepcopy(cfg["hook"]);self.marks=deepcopy(cfg["caller_marks"])
        if not isinstance(self.snapshot,str) or re.fullmatch(r"[0-9a-f]{64}",self.snapshot) is None:raise PipelineError("knowledge_drafts_snapshot_pin_required")
        if self.hook is not None and (not isinstance(self.hook,dict) or set(self.hook)!={"name","separator"} or self.hook["name"]!="quote_fragments" or not isinstance(self.hook["separator"],str) or len(self.hook["separator"])>100):raise PipelineError("knowledge_drafts_hook_invalid")
        if not isinstance(self.marks,dict) or set(self.marks)!={"marks"} or not isinstance(self.marks["marks"],list) or not 1<=len(self.marks["marks"])<=20:raise PipelineError("knowledge_drafts_marks_required")
        paths={k:local(cfg[k],path.parent) for k in ("reader_context","draft_access","purpose_policy")}
        self.reader=load_object(paths["reader_context"].read_bytes());self.access=load_object(paths["draft_access"].read_bytes());self.purpose=load_object(paths["purpose_policy"].read_bytes())
        selector={"domain":"channel_knowledge",**{k:{"mode":"exact","value":scope[k]} for k in SCOPE[1:]}}
        reader=self.reader
        if (set(reader)!={"principal_account_id","access_scope","read_layers","write_layers","operations","source_policies"} or not token(reader["principal_account_id"]) or not reader["principal_account_id"].startswith("synthetic-")
                or not access(reader["access_scope"],selector) or reader["access_scope"]["write_scopes"] or reader["write_layers"]!=[] or reader["read_layers"] not in ([],["raw"]) or reader["operations"] not in ([],["get"])
                or not isinstance(reader["source_policies"],list) or len(reader["source_policies"])!=1):raise PipelineError("knowledge_drafts_reader_invalid")
        current=reader["source_policies"][0]
        if (not isinstance(current,dict) or set(current)!={"source_id","source_kind","allow_ingest","allow_query","allow_published","policy_ref","access_scope"} or current["source_id"]!=source or current["source_kind"]!="synthetic"
                or current["allow_ingest"] is not False or current["allow_published"] is not False or type(current["allow_query"]) is not bool or not access(current["access_scope"],selector) or current["access_scope"]["write_scopes"]
                or not isinstance(current["policy_ref"],str) or not current["policy_ref"].startswith("synthetic://")):raise PipelineError("knowledge_drafts_source_registry_invalid")
        if (set(self.access)!={"actor_account_id","access_scope"} or self.access["actor_account_id"] is not None and (not token(self.access["actor_account_id"]) or not self.access["actor_account_id"].startswith("synthetic-"))
                or not access(self.access["access_scope"],selector) or self.access["access_scope"]["write_scopes"]):raise PipelineError("knowledge_drafts_host_access_invalid")
        if set(self.purpose)!={"source_id","scope","archive_read_decision","organizer_read_decision","local_cache","knowledge_drafts"} or self.purpose["source_id"]!=source or self.purpose["scope"]!=scope:raise PipelineError("knowledge_drafts_purpose_binding_invalid")
        for name in ("archive_read_decision","organizer_read_decision"):
            decision=self.purpose[name]
            if decision is not None and (not isinstance(decision,dict) or set(decision)!={"allowed","basis"} or type(decision["allowed"]) is not bool or not isinstance(decision["basis"],str) or not decision["basis"].strip()):raise PipelineError("knowledge_drafts_read_decision_invalid")
        self.records=deepcopy(cfg["records"])
        bundle={"schema_version":"1.0.1","source_id":source,"scope":scope,"events":self.records}
        if self.source._bundle(bundle) or not self.records or len({r["source_event_id"] for r in self.records})!=len(self.records):raise PipelineError("knowledge_drafts_current_originals_required")
        self.roster={self.source._key(r):r for r in self.records};self.code_files=self._code_files();watched=[path,self.python,*paths.values(),*map(Path,self.code_files)]
        if any(p.is_symlink() for p in watched):raise PipelineError("knowledge_drafts_symlink_not_allowed")
        self.watched={**self.source.watched,**{str(p):file_sha(p) for p in watched}};self.fingerprint=digest({"config":cfg,"files":self.watched,"worker":WORKER});self.runner=runner or subprocess.run

    def _code_files(self):return {str(p) for p in (self.repo/"source").rglob("*") if p.is_file() and p.suffix in {".py",".json"}}

    def _unchanged(self):
        self.source._unchanged()
        if self.code_files!=self._code_files() or any(not Path(p).is_file() or file_sha(p)!=v for p,v in self.watched.items()):raise PipelineError("knowledge_drafts_configuration_changed")

    def _source_ready(self):
        marker=self.source.state/"synthetic-channel-owner.json";db=self.source.database
        if marker.is_symlink() or not marker.is_file() or not db.is_file():raise PipelineError("knowledge_drafts_source_not_collected")
        owner=load_object(marker.read_bytes())
        if (set(owner)!={"mode","binding_sha256","store_file_id","files"} or owner["mode"]!="synthetic_channel_ingest_only" or owner["binding_sha256"]!=self.source.fingerprint
                or owner["store_file_id"]!=self.source._file_id(db) or owner["files"]!=self.source._job_files()):raise PipelineError("knowledge_drafts_source_not_attested")
        if db.stat().st_size>8_000_000 or any(db.parent.glob(db.name+"-*")) or file_sha(db)!=self.snapshot:raise PipelineError("knowledge_drafts_source_snapshot_changed")

    def _files(self):
        result={};root=self.state/"imports"
        if not root.exists():return result
        for folder,dirs,files in os.walk(root,followlinks=False):
            for name in [*dirs,*files]:
                path=Path(folder)/name
                if path.is_symlink() or getattr(path,"is_junction",lambda:False)() or not path.resolve().is_relative_to(self.state):raise PipelineError("knowledge_drafts_state_path_invalid")
            for name in files:
                path=Path(folder)/name;info=path.lstat()
                if not S_ISREG(info.st_mode) or not info.st_ino:raise PipelineError("knowledge_drafts_state_path_invalid")
                result[str(path.relative_to(self.state))]={"identity":[str(info.st_dev),str(info.st_ino)],"sha256":file_sha(path)}
        return result

    @contextmanager
    def _owned(self):
        marker=self.state/"synthetic-knowledge-drafts-owner.json"
        for name in ("synthetic-knowledge-drafts-owner.json","pipeline.lock","draft-receipts.jsonl","imports"):
            selected=self.state/name
            if selected.is_symlink() or getattr(selected,"is_junction",lambda:False)() or selected.exists() and name!="imports" and not S_ISREG(selected.lstat().st_mode):raise PipelineError("knowledge_drafts_state_path_invalid")
        if self.state.exists() and not marker.exists() and any(self.state.iterdir()):raise PipelineError("knowledge_drafts_state_not_owned")
        self.state.mkdir(parents=True,exist_ok=True)
        if not marker.exists():
            try:
                with marker.open("xb") as file:file.write(encoded({"mode":"synthetic_knowledge_drafts_only","binding_sha256":self.fingerprint,"files":{}}))
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
            except OSError:raise PipelineError("knowledge_drafts_state_busy") from None
            try:
                owner=load_object(marker.read_bytes())
                if set(owner)!={"mode","binding_sha256","files"} or owner["mode"]!="synthetic_knowledge_drafts_only" or owner["binding_sha256"]!=self.fingerprint:raise PipelineError("knowledge_drafts_state_binding_mismatch")
                if self._files()!=owner["files"]:raise PipelineError("knowledge_drafts_cache_continuity_failed")
                try:yield
                finally:
                    owner["files"]=self._files();temp=self.state/("owner-"+uuid4().hex+".tmp");temp.write_bytes(encoded(owner));os.replace(temp,marker)
            finally:
                lock.seek(0)
                if os.name=="nt":msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _call(self,args,cwd):return self.runner(args,cwd=str(cwd),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=25,creationflags=FLAGS)

    def _packet(self):return {"schema_version":"1.0.1","source_kinds":{self.binding["source_id"]:"synthetic"},"events":self.records}

    def _validate(self,envelope,request_id):
        if not isinstance(envelope,dict) or set(envelope)!={"result","audit","policy_decisions"}:raise PipelineError("knowledge_drafts_envelope_invalid")
        result,audit,decisions=envelope["result"],envelope["audit"],envelope["policy_decisions"];status,reason=self.source._result(result,request_id,read=True)
        if (not isinstance(audit,dict) or audit.get("input_hash")!=digest(self._packet()) or audit.get("input_hash_after")!=audit.get("input_hash") or audit.get("input_unchanged") is not True
                or not isinstance(audit.get("draft_sources"),list) or len(audit["draft_sources"])!=len(result["evidence"]) or not isinstance(decisions,list)):raise PipelineError("knowledge_drafts_audit_invalid")
        if status!="ok":
            if result["evidence"]:raise PipelineError("knowledge_drafts_partial_output_invalid")
            return status,reason
        if len(result["evidence"])!=len(self.marks["marks"]) or len(decisions)!=len(self.records) or any(d!={"operation":"read","source_kind":"synthetic","allowed":True} for d in decisions):raise PipelineError("knowledge_drafts_policy_not_confirmed")
        for item,source,mark in zip(result["evidence"],audit["draft_sources"],self.marks["marks"]):
            fragments=[];originals=[];refs=[];flags=set()
            for ref in mark["source_refs"]:
                raw=self.roster.get((ref["source_id"],ref["source_event_id"],ref["revision"],ref["record_id"]))
                if raw is None:raise PipelineError("knowledge_drafts_provenance_changed")
                fragment=raw["text"][ref["start"]:ref["end"]];fragments.append(fragment);flags.update(raw["quality_flags"])
                originals.append({"record":raw,"layer":"raw","source_kind":"synthetic","fragment":{"start":ref["start"],"end":ref["end"],"text":fragment}})
                locator="hgpt-raw:?"+urlencode({"source_event_id":ref["source_event_id"],"start":ref["start"],"end":ref["end"],"offset_unit":"unicode_codepoint","input_sha256":audit["input_hash"]})
                refs.append({"source_id":raw["source_id"],"record_id":raw["record_id"],"revision":raw["revision"],"locator":locator,"transformation_version":audit["transformation_version"]})
                for r in raw["source_refs"]:
                    if r not in refs:refs.append(r)
            if (not isinstance(item,dict) or set(item)!=EVIDENCE or item["layer"]!="draft" or item["source_kind"]!="synthetic" or item["source_id"]!="hgpt-ssr:organizer" or not item["record_id"].startswith("draft-")
                    or item["source_event_id"]!=item["record_id"] or item["revision"]!=1 or item["scope"]!=self.binding["scope"] or item["actor_account_id"]!=self.access["actor_account_id"] or item["event_time"]!=item["received_at"]
                    or item["text"]!=self.hook["separator"].join(fragments) or item["source_refs"]!=refs or not flags|{"derived_draft","draft_caller_label:"+mark["caller_label"]}<=set(item["quality_flags"])
                    or source!={"draft_record_id":item["record_id"],"caller_label":mark["caller_label"],"transformation_version":audit["transformation_version"],"sources":originals}):raise PipelineError("knowledge_drafts_original_or_mark_changed")
        return status,reason

    def _persist(self,job,envelope,summary):
        with (job/"native-envelope.json").open("xb") as f:f.write(encoded(envelope))
        with (job/"completion.json").open("xb") as f:f.write(encoded({"envelope_sha256":file_sha(job/"native-envelope.json"),"summary":summary}))

    def draft(self,payload):
        if not isinstance(payload,dict) or set(payload)!={"request_id","synthetic"} or not token(payload["request_id"]) or payload["synthetic"] is not True:raise RetrievalInputError("synthetic_knowledge_draft_request_invalid")
        output={"request_id":payload["request_id"],"status":"held","mode":"synthetic_knowledge_drafts_only","stage":"permission","draft_count":None,"replayed":False,"model_invoked":False,"network_used":False,"public_output":"not_attempted","reason_code":None}
        for name in ("local_cache","knowledge_drafts"):
            grant=self.purpose[name]
            if not isinstance(grant,dict) or grant.get("status")!="allowed" or not isinstance(grant.get("basis"),str) or not grant["basis"].strip():output["reason_code"]=name+"_not_allowed";return output
        decision=self.purpose["archive_read_decision"] or {};current=self.reader["source_policies"][0]
        if (decision.get("allowed") is not True or not self.reader["access_scope"]["read_scopes"] or not current["access_scope"]["read_scopes"] or current["allow_query"] is not True
                or self.reader["operations"]!=["get"] or self.reader["read_layers"]!=["raw"]):output["reason_code"]="knowledge_archive_read_not_allowed";return output
        try:
            self._unchanged();self._source_ready()
            with self._owned():
                job=self.state/"imports"/("request-"+digest(payload)[:32]);job.mkdir(parents=True,exist_ok=True)
                completed=job/"completion.json";native=job/"native-envelope.json"
                if completed.exists():
                    receipt=load_object(completed.read_bytes());envelope=load_object(native.read_bytes());self._validate(envelope,payload["request_id"])
                    if set(receipt)!={"envelope_sha256","summary"} or receipt["envelope_sha256"]!=file_sha(native) or receipt["summary"].get("request_id")!=payload["request_id"] or receipt["summary"].get("provenance_sha256")!=digest(envelope):raise PipelineError("knowledge_drafts_completion_invalid")
                    self._unchanged();self._source_ready();return {**receipt["summary"],"status":"duplicate","replayed":True}
                if native.exists():raise PipelineError("knowledge_drafts_completion_incomplete")
                query={"schema_version":"1.0.1","request_id":payload["request_id"]+"-source","text":"synthetic complete current raw channel snapshot","intent":"query","scope":self.binding["scope"],"actor_account_id":None,"person_id":None,"time_from":None,"time_to":None,"exclude_event_ids":[],"limit":100,"layer":"raw","conditions":{},"access_scope":self.reader["access_scope"]}
                for name,value in (("reader-context.json",self.reader),("source-query.json",query),("scope.json",self.binding["scope"])):(job/name).write_bytes(encoded(value))
                output["stage"]="archive";child=self._call([str(self.python),"-B","-X","utf8","source/ssr.py","--db",str(self.source.database),"--scope-file",str(job/"scope.json"),"--trusted-context",str(job/"reader-context.json"),"get","--query-file",str(job/"source-query.json")],self.source.store)
                if len(child.stdout)>2_000_000:raise PipelineError("knowledge_drafts_native_output_oversized")
                archive=load_object(child.stdout);status,reason=self.source._result(archive,query["request_id"],read=True);output.update(archive_status=status,archive_exit=child.returncode)
                if child.returncode!=0 or status not in {"ok","no_match"}:output["reason_code"]=reason or "knowledge_archive_not_authorized";return output
                if len(archive["evidence"])>=100:raise PipelineError("knowledge_drafts_archive_may_be_truncated")
                seen=set()
                for item in archive["evidence"]:
                    if not isinstance(item,dict) or set(item)!=EVIDENCE:raise PipelineError("knowledge_drafts_archive_evidence_invalid")
                    key=self.source._key(item);record=self.roster.get(key)
                    if record is None or key in seen or item["source_kind"]!="synthetic" or item["layer"]!="raw" or any(item[k]!=record[k] for k in RECORD-{"schema_version","record_kind","access_scope"}):raise PipelineError("knowledge_drafts_archive_not_registered_original")
                    seen.add(key)
                if seen!=set(self.roster):raise PipelineError("knowledge_drafts_current_snapshot_incomplete")
                self._source_ready();(job/"source-result.json").write_bytes(encoded(archive))
                native_query={**query,"request_id":payload["request_id"],"text":"synthetic explicit marked fragments to unpublished knowledge drafts","intent":"organize","layer":"draft","access_scope":self.access["access_scope"]}
                bundle={"packet":self._packet(),"query":native_query,"access":self.access,"marks":self.marks,"hook":self.hook,"purpose":self.purpose};(job/"native-input.json").write_bytes(encoded(bundle))
                output["stage"]="draft";child=self._call([str(self.python),"-B","-X","utf8","-c",WORKER,str(job/"native-input.json")],self.repo)
                if len(child.stdout)>2_000_000:raise PipelineError("knowledge_drafts_native_output_oversized")
                envelope=load_object(child.stdout);status,reason=self._validate(envelope,payload["request_id"]);output.update(draft_status=status,draft_exit=child.returncode,reason_code=reason);self._unchanged();self._source_ready()
                if child.returncode!=0 or status not in {"ok","no_match"}:return output
                output.update(status="drafted" if status=="ok" else "no_match",draft_count=len(envelope["result"]["evidence"]),source_snapshot_sha256=self.snapshot,provenance_sha256=digest(envelope),snapshot_records=len(self.roster));self._persist(job,envelope,output)
            with self._owned():JsonlLog(self.state/"draft-receipts.jsonl").append({**output,"binding_sha256":self.fingerprint,"purpose_sha256":digest(self.purpose),"ai_input":"not_used","ai_train":"not_used"})
            return output
        except (PipelineError,OSError,ValueError,TypeError,KeyError,AttributeError,subprocess.SubprocessError) as error:
            output.update(status="held",draft_count=None,reason_code=getattr(error,"code","knowledge_drafts_local_operation_failed"));return output
