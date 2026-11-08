"""Current planner -> fixed game-name caller -> native game reader, synthetic only."""
from copy import deepcopy
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

from .chat_history_pipeline import REPO,WORKSPACE,encoded,sha,write,now,local,linked
from .modular_retrieval import load_object,PipelineError
from .game_name_binding import PROFILE,BindingError,map_task,reject_numeric_text

REPOS={"ssr-chat":REPO,"planner":REPO.parent/"components/hgpt-ssr-query-planner","reader":REPO.parent/"components/hgpt-ssr-game-search"}
SOURCE_PROFILE=REPO/"contracts/game_name_source.synthetic.v1.json"
DATA_KEYS={"snapshot","source_records","projection_registry","manifest","context","handoffs"}

def source_files():
    result={}
    for name,repo in REPOS.items():
        for directory,dirs,files in os.walk(repo):
            base=Path(directory);relative=base.relative_to(repo)
            if relative==Path("."):
                dirs[:]=sorted(d for d in dirs if d in {"ssr_chat","source","tests","fixtures","contracts","interfaces","docs","examples"} and not linked(base/d))
            else:
                dirs[:]=sorted(d for d in dirs if d not in {"__pycache__","acceptance","evidence",".test-output","runs"} and not linked(base/d))
            for name_ in sorted(files):
                path=base/name_
                if path.suffix not in {".py",".json",".jsonl",".md",".js",".mjs",".css",".html",".toml",".lock"} or linked(path) or name_.startswith(".env"):continue
                result[name+"/"+path.relative_to(repo).as_posix()]=sha(path.read_bytes())
    return result

def _jsonl(raw):return [load_object(line.encode("utf-8")) for line in raw.decode("utf-8").splitlines() if line.strip()]
def _key(record):return encoded([record["scope"],record["source_id"],record["source_event_id"],record["record_id"],record["revision"]])

def registered_source():
    profile=load_object(SOURCE_PROFILE.read_bytes())
    if profile.get("profile")!=PROFILE or set(profile.get("files",{}))!=DATA_KEYS:raise ValueError("registered_profile_invalid")
    manifest=local(WORKSPACE/profile["authority_manifest_workspace_relative_path"])
    manifest.relative_to(WORKSPACE/"evidence")
    if sha(manifest.read_bytes())!=profile["authority_manifest_sha256"]:raise ValueError("saved_source_authority_changed")
    raw={}
    for name,entry in profile["files"].items():
        path=local(WORKSPACE/entry["workspace_relative_path"]);path.relative_to(WORKSPACE/"evidence")
        data=path.read_bytes()
        if sha(data)!=entry["sha256"]:raise ValueError("saved_source_artifact_changed")
        raw[name]=data
    return profile,raw

def _validate_data(config,config_path):
    profile,authority=registered_source()
    if config["source_id"]!=profile["source_id"]:raise ValueError("registered_source_id_mismatch")
    blobs={};hashes={}
    for name in DATA_KEYS:
        path=local(config[name]);path.relative_to(config_path.parent)
        blobs[name]=path.read_bytes();hashes[str(path)]=sha(blobs[name])
    for name in DATA_KEYS-{"manifest","context"}:
        if sha(blobs[name])!=profile["files"][name]["sha256"]:raise ValueError("registered_"+name+"_changed")
    manifest=load_object(blobs["manifest"]);expected_manifest=load_object(authority["manifest"])
    # The only permitted manifest variation is a narrower source-use refusal.
    narrowed=deepcopy(expected_manifest);policy=manifest.get("source_policies",{}).get(profile["source_id"],{})
    if policy.get("query_allowed") is False and isinstance(policy.get("reason_code"),str) and policy["reason_code"]:
        narrowed["source_policies"][profile["source_id"]].update(query_allowed=False,reason_code=policy["reason_code"])
    if manifest!=expected_manifest and manifest!=narrowed:raise ValueError("manifest_profile_mismatch")
    context=load_object(blobs["context"]);base_context=load_object(authority["context"])
    if set(context)!=set(base_context):raise ValueError("context_profile_mismatch")
    access=context.get("access_scope",{});original=base_context["access_scope"]
    if (set(access)!={"read_scopes","write_scopes"} or access["write_scopes"]!=original["write_scopes"]
            or not isinstance(access["read_scopes"],list) or any(s not in original["read_scopes"] for s in access["read_scopes"])
            or not isinstance(context.get("allowed_layers"),list) or any(s not in base_context["allowed_layers"] for s in context["allowed_layers"])
            or {k:v for k,v in context.items() if k not in {"access_scope","allowed_layers"}}!={k:v for k,v in base_context.items() if k not in {"access_scope","allowed_layers"}}):raise ValueError("context_profile_mismatch")
    records=_jsonl(blobs["source_records"]);rows=_jsonl(blobs["snapshot"])
    metadata=load_object(b'{"rows":'+blobs["projection_registry"]+b'}')["rows"]
    handoffs=_jsonl(blobs["handoffs"])
    source_records={};registered={};saved={}
    for record in records:
        if not {"schema_version","record_kind","access_scope"}<=set(record):raise ValueError("full_original_record_required")
        if record.get("schema_version")!="1.0.1" or record.get("source_id")!=profile["source_id"] or record.get("scope")!=profile["scope"] or "synthetic" not in record.get("quality_flags",[]):raise ValueError("original_record_binding_invalid")
        key=_key(record)
        if key in source_records:raise ValueError("export_record_ambiguous")
        source_records[key]=record
    for item in metadata:
        key=_key(item["record"])
        if item["record_sha256"]!=sha(encoded(item["record"])):raise ValueError("projection_hash_mismatch")
        if key in registered and registered[key]!=item:raise ValueError("projection_ambiguous")
        registered[key]=item
    for item in handoffs:
        key=_key(item["record"])
        if key in saved:raise ValueError("handoff_ambiguous")
        saved[key]=item
    seen=set()
    for row in rows:
        if set(row)!={"name","fields","record"}:raise ValueError("snapshot_envelope_invalid")
        key=_key(row["record"])
        if key in seen or key not in source_records or key not in registered or key not in saved:raise ValueError("snapshot_export_identity_mismatch")
        seen.add(key);record=source_records[key];meta=registered[key];handoff=saved[key]
        if row["record"]!=record or record!=meta["record"] or record!=handoff["record"] or row["name"]!=meta["name"] or row["fields"]!=meta["fields"]:raise ValueError("original_record_or_projection_changed")
        grant=handoff["acquisition"]["policy"]["uses"].get("search_index",{})
        if grant.get("status")!="allowed" or not grant.get("basis"):raise ValueError("source_search_index_not_allowed")
    if seen!=set(source_records):raise ValueError("snapshot_export_incomplete")
    return profile,source_records,hashes

def execute(config_path,input_path,run_dir):
    root=local(run_dir);root.relative_to(WORKSPACE/"evidence");root.mkdir(parents=True,exist_ok=False)
    before=source_files();write(root/"source-before.json",before)
    report={"profile":PROFILE,"data_mode":"saved_authored_synthetic","reader_invoked":False,"model_invoked":False,"public_output":"not_attempted","calls":[]}
    code=3;effective={};calls=report["calls"]
    try:
        config_path=local(config_path);input_path=local(input_path);config_raw=config_path.read_bytes();config=load_object(config_raw)
        required={"schema_version","profile","synthetic","template","source_id","source_files_sha256",*DATA_KEYS}
        if set(config)!=required or config["schema_version"]!="1.0.1" or config["profile"]!=PROFILE or config["synthetic"] is not True:raise ValueError("game_name_configuration_invalid")
        if config["source_files_sha256"]!=before:raise ValueError("source_pin_mismatch")
        profile,records,effective=_validate_data(config,config_path)
        effective[str(config_path)]=sha(config_raw);write(root/"effective-inputs.json",effective)
        template=config["template"]
        if not isinstance(template,dict) or template.get("synthetic") is not True or not isinstance(template.get("trusted_context"),dict) or template["trusted_context"].get("synthetic") is not True or not isinstance(template.get("request"),dict):raise ValueError("synthetic_template_required")
        conditions=template["request"].get("conditions",{})
        if not isinstance(conditions,dict):raise ValueError("game_name_conditions_unsupported")
        if "filters" in conditions:raise ValueError("numeric_filters_not_supported")
        if set(conditions)-{"before_event_time"}:raise ValueError("game_name_conditions_unsupported")
        payload=load_object(input_path.read_bytes())
        if set(payload)!={"request_id","text"} or any(not isinstance(v,str) or not v.strip() for v in payload.values()) or len(payload["text"])>16384:raise ValueError("only_request_id_and_text_allowed")
        reject_numeric_text(payload["text"])
        request=deepcopy(template);request["request"].update(payload)
        write(root/"input.json",payload);write(root/"planner-input.json",request)
        def invoke(stage,repo,args,input_file):
            folder=root/stage;folder.mkdir();raw=input_file.read_bytes();(folder/"input.bin").write_bytes(raw)
            command=[sys.executable,"-B","-X","utf8",*args];started=now();stdout=stderr=b"";exit_code=None;outcome="unavailable";failure=None
            try:
                child=subprocess.run(command,cwd=repo,stdin=subprocess.DEVNULL,capture_output=True,timeout=30,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                stdout,stderr,exit_code,outcome=child.stdout,child.stderr,child.returncode,"returned"
            except subprocess.TimeoutExpired as exc:
                stdout,stderr,outcome=exc.stdout or b"",exc.stderr or b"","timeout"
                failure={"type":"TimeoutExpired","timeout_seconds":exc.timeout,"execution_state":"started; completion uncertain"}
            except OSError as exc:
                failure={"type":type(exc).__name__,"errno":exc.errno,"winerror":getattr(exc,"winerror",None),"filename":str(exc.filename) if exc.filename else None,"message":str(exc),"execution_state":"unavailable; no retry"}
            (folder/"stdout.bin").write_bytes(stdout);(folder/"stderr.bin").write_bytes(stderr)
            receipt={"stage":stage,"command":command,"cwd":str(repo),"started_at":started,"finished_at":now(),"exit_code":exit_code,"outcome":outcome,"failure":failure,"stdin":"DEVNULL","input_sha256":sha(raw),"stdout_sha256":sha(stdout),"stderr_sha256":sha(stderr)}
            write(folder/"receipt.json",receipt);calls.append(receipt)
            if outcome!="returned":raise ValueError("component_"+outcome)
            return exit_code,load_object(stdout)
        planner_code,plan=invoke("planner",REPOS["planner"],["-m","source.hgpt_ssr","--contract-version","1.0.1","--input",str(root/"planner-input.json")],root/"planner-input.json")
        write(root/"plan.json",plan);report.update(plan=plan,planner_exit_code=planner_code)
        if planner_code!=0:raise ValueError("planner_not_ready")
        if plan.get("schema_version")!="1.0.1" or plan.get("request_id")!=payload["request_id"] or not isinstance(plan.get("tasks"),list) or len(plan["tasks"])!=1 or plan.get("missing_inputs"):raise ValueError("single_bound_game_task_required")
        task=plan["tasks"][0];query=map_task(task,profile=config["profile"])
        if query["request_id"]!=payload["request_id"] or query["text"]!=payload["text"]:raise ValueError("task_input_identity_changed")
        if query["scope"]!=profile["scope"]:raise ValueError("game_source_scope_mismatch")
        write(root/"original-query.json",task["query"]);write(root/"reader-query.json",query)
        report["binding"]={"profile":PROFILE,"task_id":task["task_id"],"changed_json_pointers":[],"intent_preserved":"game_lookup"}
        reader_code,native=invoke("reader",REPOS["reader"],["ssr.py","--query",str(root/"reader-query.json"),"--snapshot",str(local(config["snapshot"])),"--manifest",str(local(config["manifest"])),"--context",str(local(config["context"]))],root/"reader-query.json")
        write(root/"reader-result.json",native)
        exits={"ok":0,"no_match":0,"skipped":0,"missing_input":2,"not_configured":2,"denied":2,"error":1}
        if native.get("schema_version")!="1.0.1" or native.get("request_id")!=payload["request_id"] or native.get("domain")!="game_knowledge" or reader_code!=exits.get(native.get("status")):raise ValueError("reader_result_identity_or_exit_invalid")
        for item in native.get("execution",{}).get("attempted_sources",[]):
            if item.get("source_id") not in {None,profile["source_id"]}:raise ValueError("native_attempt_source_mismatch")
        for evidence in native.get("evidence",[]):
            key=_key(evidence)
            if key not in records or evidence.get("source_kind")!="synthetic" or evidence.get("layer")!="raw":raise ValueError("native_evidence_source_mismatch")
            if any(evidence.get(k)!=v for k,v in records[key].items() if k not in {"schema_version","record_kind","access_scope"}):raise ValueError("native_original_record_changed")
        report.update(status="reader_result",result=native,reader_exit_code=reader_code);code=reader_code
    except (ValueError,OSError,TypeError,KeyError,subprocess.SubprocessError) as exc:
        report.update(status="held",reason_code=getattr(exc,"code",str(exc)) if isinstance(exc,ValueError) else "game_name_input_or_execution_invalid")
    after=source_files();write(root/"source-after.json",after)
    same=before==after and all(Path(p).is_file() and sha(Path(p).read_bytes())==digest for p,digest in effective.items())
    report["reader_invoked"]=any(c["stage"]=="reader" and c["outcome"] in {"returned","timeout"} for c in calls)
    report.update(source_snapshot_sha256=sha(encoded(before)),source_unchanged=same,real_llm_success=False)
    if not same:report.update(status="held",reason_code="source_changed_during_execution");code=3
    write(root/"receipt.json",{**report,"exit_code":code})
    return report,code

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("config","input","run-dir"):parser.add_argument("--"+name,required=True)
    args=parser.parse_args(argv);report,code=execute(args.config,args.input,args.run_dir)
    print(json.dumps(report,ensure_ascii=False,allow_nan=False));return code
if __name__=="__main__":raise SystemExit(main())
