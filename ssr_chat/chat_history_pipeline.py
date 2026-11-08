"""Explicit synthetic-only planner -> history binding -> native chat reader CLI.

No HTTP, database, model or arbitrary command configuration is accepted.
"""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .chat_history_binding import PROFILE, BindingError, map_task
from .modular_retrieval import load_object, PipelineError

REPO=Path(__file__).resolve().parents[1]
WORKSPACE=REPO.parent.parent
REPOS={"ssr-chat":REPO,"planner":REPO.parent/"components/hgpt-ssr-query-planner","reader":REPO.parent/"components/hgpt-ssr-chat-search"}
SOURCE_DIRS={"ssr_chat","source","hgpt_ssr","tests","fixtures","contracts","docs"}
SUFFIXES={".py",".json",".jsonl",".md",".js",".css",".html",".toml",".lock"}
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
def sha(raw):return hashlib.sha256(raw).hexdigest()
def write(path,value):
    with Path(path).open("xb") as stream:stream.write(json.dumps(value,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")+b"\n")
def now():return datetime.now(timezone.utc).isoformat()
def linked(path):return path.is_symlink() or getattr(path,"is_junction",lambda:False)()

def local(path):
    path=Path(path)
    if not path.is_absolute() or str(path).startswith(("\\\\","//")) or "://" in str(path):raise ValueError("absolute_local_path_required")
    for part in (path,*path.parents):
        if linked(part):raise ValueError("linked_path_refused")
    return path.resolve()

def source_files():
    """Allowlisted source/test/fixture fingerprints. No Git metadata or live state reads."""
    result={}
    for name,repo in REPOS.items():
        for directory,dirs,files in os.walk(repo):
            base=Path(directory);relative=base.relative_to(repo)
            if relative==Path("."):
                dirs[:]=sorted(d for d in dirs if d in SOURCE_DIRS and not linked(base/d))
            else:
                dirs[:]=sorted(d for d in dirs if d not in {"__pycache__","acceptance","evidence",".test-output","runs"} and not linked(base/d))
            for filename in sorted(files):
                path=base/filename
                if path.suffix not in SUFFIXES or path.is_symlink() or filename.startswith(".env"):continue
                result[name+"/"+path.relative_to(repo).as_posix()]=sha(path.read_bytes())
    return result

def execute(config_path,input_path,run_dir):
    root=local(run_dir);root.relative_to(WORKSPACE/"evidence")
    root.mkdir(parents=True,exist_ok=False)
    before=source_files();write(root/"source-before.json",before)
    calls=[];report={"profile":PROFILE,"data_mode":"authored_synthetic","reader_invoked":False,"model_invoked":False,"public_output":"not_attempted","calls":calls}
    exit_code=3;effective={}
    try:
        config_path=local(config_path);input_path=local(input_path)
        config_raw=config_path.read_bytes();config=load_object(config_raw)
        required={"schema_version","profile","synthetic","template","snapshot","authorization","source_id","source_files_sha256"}
        if set(config)!=required or config["schema_version"]!="1.0.1" or config["profile"]!=PROFILE or config["synthetic"] is not True:raise ValueError("synthetic_binding_configuration_invalid")
        if config["source_files_sha256"]!=before:raise ValueError("source_pin_mismatch")
        template=config["template"]
        if (not isinstance(template,dict) or template.get("synthetic") is not True or not isinstance(template.get("request"),dict)
                or not isinstance(template.get("trusted_context"),dict) or template["trusted_context"].get("synthetic") is not True):raise ValueError("synthetic_template_required")
        source_path=local(config["snapshot"]);auth_path=local(config["authorization"])
        for path in (source_path,auth_path):path.relative_to(config_path.parent)
        source_raw=source_path.read_bytes();auth_raw=auth_path.read_bytes()
        source=load_object(source_raw);authorization=load_object(auth_raw)
        if source.get("source_kind")!="synthetic" or not isinstance(source.get("records"),list) or any(not isinstance(r,dict) or "synthetic" not in r.get("quality_flags",[]) for r in source["records"]):raise ValueError("synthetic_snapshot_required")
        if not isinstance(config["source_id"],str) or source.get("source_id")!=config["source_id"] or any(r.get("source_id")!=config["source_id"] for r in source["records"]):raise ValueError("reader_source_binding_mismatch")
        effective={str(config_path):sha(config_raw),str(source_path):sha(source_raw),str(auth_path):sha(auth_raw)}
        write(root/"effective-inputs.json",effective)
        payload=load_object(input_path.read_bytes())
        if set(payload)!={"request_id","text"} or any(not isinstance(payload[k],str) or not payload[k].strip() for k in payload) or len(payload["text"])>16384:raise ValueError("only_request_id_and_text_allowed")
        request=deepcopy(template);request["request"].update(payload)
        write(root/"input.json",payload);write(root/"planner-input.json",request)
        def invoke(stage,repo,args,input_file):
            folder=root/stage;folder.mkdir()
            raw=input_file.read_bytes();(folder/"input.bin").write_bytes(raw)
            command=[sys.executable,"-B","-X","utf8",*args]
            started=now();stdout=stderr=b"";native_exit=None;outcome="unavailable"
            try:
                child=subprocess.run(command,cwd=repo,stdin=subprocess.DEVNULL,capture_output=True,timeout=30,
                    creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                stdout,stderr,native_exit,outcome=child.stdout,child.stderr,child.returncode,"returned"
            except subprocess.TimeoutExpired as exc:
                stdout,stderr,outcome=exc.stdout or b"",exc.stderr or b"","timeout"
            except OSError:
                pass
            (folder/"stdout.bin").write_bytes(stdout);(folder/"stderr.bin").write_bytes(stderr)
            receipt={"stage":stage,"command":command,"cwd":str(repo),"started_at":started,"finished_at":now(),"exit_code":native_exit,"outcome":outcome,"input_sha256":sha(raw),"stdout_sha256":sha(stdout),"stderr_sha256":sha(stderr),"stdin":"DEVNULL"}
            write(folder/"receipt.json",receipt);calls.append(receipt)
            if outcome!="returned":raise ValueError("component_"+outcome)
            if len(stdout)>2_000_000:raise ValueError("component_output_too_large")
            return native_exit,load_object(stdout)
        planner_code,plan=invoke("planner",REPOS["planner"],["-m","source.hgpt_ssr","--contract-version","1.0.1","--input",str(root/"planner-input.json")],root/"planner-input.json")
        write(root/"plan.json",plan);report["plan"]=plan;report["planner_exit_code"]=planner_code
        if planner_code!=0:raise ValueError("planner_not_ready")
        if plan.get("schema_version")!="1.0.1" or plan.get("request_id")!=payload["request_id"] or not isinstance(plan.get("tasks"),list) or len(plan["tasks"])!=1 or plan.get("missing_inputs"):raise ValueError("single_bound_chat_task_required")
        task=plan["tasks"][0]
        if not isinstance(task,dict) or not isinstance(task.get("query"),dict) or task["query"].get("request_id")!=payload["request_id"]:raise ValueError("task_request_identity_mismatch")
        mapped=map_task(task,profile=config["profile"])
        write(root/"original-query.json",task["query"]);write(root/"reader-query.json",mapped)
        report["binding"]={"profile":PROFILE,"task_id":task["task_id"],"changed_json_pointers":["/intent"],"from_intent":"chat_history","to_intent":"history"}
        report["reader_attempted"]=True
        reader_code,native=invoke("reader",REPOS["reader"],["-m","hgpt_ssr","--query",str(root/"reader-query.json"),"--snapshot",str(source_path),"--authorization",str(auth_path)],root/"reader-query.json")
        report["reader_invoked"]=True
        write(root/"reader-result.json",native)
        native_exits={"ok":0,"no_match":0,"skipped":0,"missing_input":2,"not_configured":3,"denied":4,"error":1}
        if reader_code!=native_exits.get(native.get("status")) or native.get("schema_version")!="1.0.1" or native.get("request_id")!=payload["request_id"] or native.get("domain")!="chat_history":raise ValueError("reader_result_identity_or_exit_invalid")
        report.update(status="reader_result",reader_exit_code=reader_code,result=native)
        exit_code=reader_code
    except (ValueError,OSError,TypeError,KeyError,subprocess.SubprocessError) as exc:
        report.update(status="held",reason_code=getattr(exc,"code","binding_input_or_execution_invalid") if isinstance(exc,(BindingError,PipelineError)) else str(exc) if isinstance(exc,ValueError) else "binding_input_or_execution_invalid")
    after=source_files();write(root/"source-after.json",after)
    report["reader_invoked"]=any(call["stage"]=="reader" and call["outcome"] in {"returned","timeout"} for call in calls)
    same=before==after and all(Path(path).is_file() and sha(Path(path).read_bytes())==digest for path,digest in effective.items())
    report.update(source_snapshot_sha256=sha(encoded(before)),source_unchanged=same,source_scope="3 fixed checkouts allowlisted source/contracts/tests/synthetic fixtures; no Git metadata/live state",real_llm_success=False)
    if not same:report.update(status="held",reason_code="source_changed_during_execution");exit_code=3
    write(root/"receipt.json",{**report,"exit_code":exit_code})
    return report,exit_code

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("config","input","run-dir"):parser.add_argument("--"+name,required=True)
    args=parser.parse_args(argv)
    report,code=execute(args.config,args.input,args.run_dir)
    print(json.dumps(report,ensure_ascii=False,allow_nan=False))
    return code
if __name__=="__main__":raise SystemExit(main())
