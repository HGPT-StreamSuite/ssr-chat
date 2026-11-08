"""Synthetic, host-owned current-view bridge; never a full-history adapter.

Fresh channel-store sidecars decide current eligibility before the unchanged
channel reader sees a private current-only JSONL view. History/exact/withdrawn
queries are rejected. Original Records, ACLs and query values are not rewritten.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

from .chat_store_binding import (BindingError, canonical, digest, load, local, now,
                                quiescent, read, require, sha, write, write_bytes)

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent.parent
STORE = WORKSPACE / 'repos/components/hgpt-channel-store'
READER = WORKSPACE / 'repos/components/hgpt-ssr-channel-search'
PROFILE = 'channel_store_current_search_synthetic_v1'
IDENTITY = ('source_id', 'source_event_id', 'record_id', 'revision', 'layer')
LAYERS = {'raw_event': 'raw', 'draft': 'draft', 'published_statement': 'published'}
QUERY_FIELDS = {'schema_version','request_id','text','intent','scope','actor_account_id','person_id',
                'time_from','time_to','exclude_event_ids','limit','layer','conditions','access_scope'}
PROFILE_FIELDS = {'schema_version','profile','synthetic','source_pins','archive','scope','context','request','grant'}
READER_EXITS = {'ok':0,'no_match':0,'skipped':0,'missing_input':2,'not_configured':3,'denied':4,'error':5}


def source_pins():
    result = {}
    for name, root in (('caller', REPO/'ssr_chat'), ('provider', STORE/'source'), ('reader', READER/'source')):
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.suffix in {'.py','.json'} and '__pycache__' not in path.parts:
                result[name+'/'+path.relative_to(root).as_posix()] = sha(path.read_bytes())
    return result


def identity(value):
    return tuple(value[key] for key in IDENTITY)


def read_allowed(acl, scope):
    """The closed fixture profile accepts explicit exact/any selectors only."""
    if not isinstance(acl, dict) or set(acl) != {'read_scopes','write_scopes'}:
        return False
    for selector in acl['read_scopes']:
        if not isinstance(selector, dict) or set(selector) != set(scope) or selector.get('domain') != scope['domain']:
            continue
        good = True
        for key in set(scope)-{'domain'}:
            part = selector[key]
            good = good and isinstance(part,dict) and (
                (set(part)=={'mode','value'} and part['mode']=='exact' and part['value']==scope[key])
                or (set(part)=={'mode'} and part['mode']=='any'))
        if good:
            return True
    return False


def validate_query(query, scope, sources):
    require(set(query)==QUERY_FIELDS and query['schema_version']=='1.0.1', 'query_contract_invalid')
    require(query['scope']==scope and scope['domain']=='channel_knowledge', 'query_scope_mismatch')
    require(isinstance(query['request_id'],str) and bool(query['request_id']), 'query_request_id_invalid')
    require(query['layer'] in {'raw','draft','published'}, 'query_layer_invalid')
    conditions=query['conditions']
    require(isinstance(conditions,dict), 'query_conditions_invalid')
    require('revision' not in conditions and 'revision_view' not in conditions and
            conditions.get('revision_mode','latest')=='latest' and
            conditions.get('include_withdrawn',False) is False, 'current_view_only')
    require(set(conditions)<={'source_ids','text_match','revision_mode','include_withdrawn','before_event_time'},
            'query_conditions_unsupported')
    require(isinstance(conditions.get('source_ids',[]),list) and
            set(conditions.get('source_ids',[])) <= sources, 'query_source_mismatch')


def project_current(result, query, request, grant, context):
    """Validate a freshly executed provider packet and produce an explicit view.

    This function is not an authority for caller-supplied packets. execute()
    owns the fixed producer invocation, data/source pins and original stdout.
    """
    require(result.get('kind')=='channel_store_snapshot_result' and result.get('snapshot_version')=='1.0.0'
            and result.get('status')=='ok' and result.get('request_id')==request['request_id'], 'snapshot_result_invalid')
    receipt=result['receipt']; packet=result['snapshot']
    require(receipt['read_only'] is True and receipt['source_before_sha256']==receipt['source_after_sha256']
            and receipt['grant_sha256']==digest(grant), 'snapshot_receipt_invalid')
    require(packet['snapshot_id']==digest({k:v for k,v in packet.items() if k!='snapshot_id'}), 'snapshot_hash_invalid')
    require(packet['snapshot_version']=='1.0.0' and packet['native_contract_version']=='1.0.1'
            and packet['completeness']=='complete_stored_history' and request['revisions'] is None,
            'complete_history_packet_required')
    scope=request['scope']
    require(packet['scope']==scope==query['scope']==grant['scope'], 'snapshot_scope_mismatch')
    require(grant['principal_account_id']==context['principal_account_id'] and grant['allow_history'] is True
            and grant['allow_withdrawals'] is True and grant['purpose']=='search_index'
            and grant['purpose_allowed'] is True, 'snapshot_grant_invalid')
    require(packet['authorized_purpose']=={'purpose':'search_index','policy_ref':grant['policy_ref'],
            'downstream_ai_uses_granted':False}, 'snapshot_purpose_mismatch')
    bindings={item['source_id']:item['source_kind'] for item in packet['source_bindings']}
    require(len(bindings)==len(packet['source_bindings']) and set(bindings)==set(request['source_ids'])
            and packet['source_bindings']==sorted(grant['sources'],key=lambda x:x['source_id'])
            and all(kind=='synthetic' for kind in bindings.values()), 'snapshot_sources_mismatch')
    require(set(packet['layers'])==set(request['layers'])==set(grant['layers']), 'snapshot_layers_mismatch')
    validate_query(query,scope,set(bindings))
    policies={policy['source_id']:policy for policy in context['source_policies']}
    records={}; metadata={}; latest={}
    for record in packet['records']:
        require(record['scope']==scope and record['schema_version']=='1.0.1' and
                'synthetic' in record['quality_flags'], 'record_scope_or_kind_invalid')
        layer=LAYERS.get(record['record_kind']); require(layer in packet['layers'], 'record_layer_invalid')
        key=(record['source_id'],record['source_event_id'],record['record_id'],record['revision'],layer)
        require(key not in records, 'duplicate_record')
        records[key]=record
        family=key[:2]
        if family not in latest or key[3]>latest[family][3]: latest[family]=key
    for item in packet['record_metadata']:
        key=identity(item); require(key not in metadata and key in records, 'record_metadata_mismatch')
        record=records[key]; policy=policies[item['source_id']]
        require(item['record_included'] is True and item['payload_sha256']==digest(record)
                and item['source_kind']==bindings[item['source_id']], 'record_payload_mismatch')
        require(item['source_policy_current']=={k:policy[k] for k in
                ('policy_ref','access_scope','allow_query','allow_published')} and policy['allow_query'] is True
                and (key[4]!='published' or policy['allow_published'] is True), 'source_policy_mismatch')
        require(read_allowed(item['source_access_scope_snapshot'],scope) and
                read_allowed(policy['access_scope'],scope) and read_allowed(record['access_scope'],scope),
                'source_acl_denied')
        require(type(item['withdrawn_direct']) is bool and type(item['current_eligible']) is bool and
                item['is_latest_revision'] is (latest[key[:2]]==key), 'metadata_flags_invalid')
        metadata[key]=item
    require(set(metadata)==set(records), 'record_metadata_missing')
    fact_keys=[identity(fact['target']) for fact in packet['withdrawals']]
    require(all(set(fact)=={'target','principal_account_id','recorded_at'} and
                isinstance(fact['principal_account_id'],str) and bool(fact['principal_account_id']) and
                isinstance(fact['recorded_at'],str) and bool(fact['recorded_at'])
                for fact in packet['withdrawals']), 'withdrawal_provenance_invalid')
    require(len(set(fact_keys))==len(fact_keys) and set(fact_keys)==
            {key for key,item in metadata.items() if item['withdrawn_direct']}, 'withdrawal_facts_mismatch')
    def blocked(key, seen=None):
        seen=set() if seen is None else set(seen)
        require(key in metadata and key not in seen, 'lineage_invalid'); seen.add(key)
        item=metadata[key]
        if item['withdrawn_direct']: return True
        parent=item['lineage_parent']
        if parent is None: return False
        parent=identity(parent)
        require(parent in metadata, 'lineage_missing')
        return blocked(parent,seen) or blocked(latest[parent[:2]],seen)
    current={identity(item):item for item in packet['current']}
    require(len(current)==len(packet['current']) and set(current)==set(latest.values()), 'current_index_invalid')
    eligible=[]
    for key,item in metadata.items():
        is_latest=latest[key[:2]]==key
        allowed=is_latest and not blocked(key)
        require(item['current_eligible'] is allowed, 'current_eligibility_invalid')
        if is_latest:
            state='active' if allowed else 'withdrawn' if item['withdrawn_direct'] else 'ancestor_withdrawn'
            require(current[key]['state']==state, 'withdrawal_state_invalid')
        if allowed and key[4]==query['layer']: eligible.append(deepcopy(records[key]))
    require(len(packet['source_coverage'])==len(bindings) and
            {c['source_id'] for c in packet['source_coverage']}==set(bindings), 'source_coverage_invalid')
    for coverage in packet['source_coverage']:
        count=sum(key[0]==coverage['source_id'] for key in records)
        require(coverage['stored_revisions']==coverage['returned_revisions']==count, 'source_coverage_invalid')
    return eligible


def invoke(stage, argv, cwd, root):
    started=now(); stdout=b''; stderr=b''; exit_code=None; outcome='unavailable'; failure=None
    command=[sys.executable,'-B','-X','utf8',*argv]
    try:
        child=subprocess.run(command,cwd=cwd,stdin=subprocess.DEVNULL,capture_output=True,timeout=30,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        stdout,stderr,exit_code,outcome=child.stdout,child.stderr,child.returncode,'returned'
    except subprocess.TimeoutExpired as exc:
        stdout,stderr,outcome=exc.stdout or b'',exc.stderr or b'','timeout'
        failure={'type':'TimeoutExpired','timeout':30}
    except OSError as exc:
        failure={'type':type(exc).__name__,'errno':exc.errno,'winerror':getattr(exc,'winerror',None),
                 'filename':str(exc.filename)[:512] if exc.filename else None}
    write_bytes(root/(stage+'.stdout.bin'),stdout); write_bytes(root/(stage+'.stderr.bin'),stderr)
    call={'stage':stage,'command':command,'cwd':str(cwd),'started_at':started,'finished_at':now(),
          'exit_code':exit_code,'outcome':outcome,'failure':failure,'stdout_sha256':sha(stdout),'stderr_sha256':sha(stderr)}
    write(root/(stage+'.execution.json'),call)
    require(outcome=='returned','execution_'+outcome)
    return exit_code,load(stdout),call


def reader_config(scope, principal, layer, sources):
    # Native applicability lists non-null dimensions; the six-field Scope is
    # still retained verbatim, including every exact null.
    return {'contract_version':'1.0.1','domain':'channel_knowledge','principal_account_id':principal,
            'applicable_scope_fields':[key for key,value in scope.items() if value is not None],
            'grants':[{'scope':scope,'layers':[layer],'allow_history':False,'allow_withdrawn':False}],
            'sources':sources}


def execute(profile_path,query_path,run_dir):
    root=local(run_dir,WORKSPACE/'evidence'); root.mkdir(parents=True,exist_ok=False)
    report={'profile':PROFILE,'scope':'current_view_only','provider_invoked':False,'reader_invoked':False,'model_invoked':False,
            'public_output':'not_attempted','calls':[],'ai_grants_created':[]}
    before=source_pins(); write(root/'source-before.json',before)
    effective={}; code=4
    try:
        profile=read(local(profile_path)); query=read(local(query_path))
        require(set(profile)==PROFILE_FIELDS and type(profile['schema_version']) is int and profile['schema_version']==1 and profile['profile']==PROFILE
                and profile['synthetic'] is True and profile['source_pins']==before,'host_profile_invalid')
        paths={key:local(profile[key]['path']) for key in ('archive','scope','context','request','grant')}
        require(paths['archive'].is_relative_to(WORKSPACE/'evidence'), 'synthetic_archive_required')
        for key,path in paths.items():require(sha(path.read_bytes())==profile[key]['sha256'],'host_input_pin_mismatch')
        effective={str(path):sha(path.read_bytes()) for path in [*paths.values(),local(profile_path),local(query_path)]}
        request,grant,context=read(paths['request']),read(paths['grant']),read(paths['context'])
        require(read(paths['scope'])==request['scope'],'host_scope_mismatch')
        validate_query(query,request['scope'],set(request['source_ids']))
        quiescent(paths['archive'])
        write(root/'input-query.json',query)
        args=[str(STORE/'source/ssr.py'),'--db',str(paths['archive']),'--scope-file',str(paths['scope']),
              '--trusted-context',str(paths['context']),'snapshot','--request-file',str(paths['request']),
              '--snapshot-grant',str(paths['grant'])]
        native_code,packet,call=invoke('provider',args,STORE,root); report['calls'].append(call)
        write(root/'snapshot-result.json',packet)
        if native_code!=0 or packet.get('status')!='ok':
            require(packet.get('snapshot') is None,'provider_non_ok_data')
            report.update(status='held',reason_code='provider_'+str(packet.get('reason_code')),provider_status=packet.get('status'))
        else:
            records=project_current(packet,query,request,grant,context)
            # This private view deliberately omits entire ineligible current families.
            # It is never offered through the complete-history adapter interface.
            sources=[]
            for binding in packet['snapshot']['source_bindings']:
                path=root/('view-'+str(len(sources))+'.jsonl')
                write_bytes(path,b''.join(canonical(r)+b'\n' for r in records if r['source_id']==binding['source_id']))
                sources.append({**binding,'scope':request['scope'],'layer':query['layer'],'kind':'snapshot','path':str(path)})
            config=reader_config(request['scope'],context['principal_account_id'],query['layer'],sources)
            write(root/'current-view-contract.json',{'profile':PROFILE,'view':'eligible_current_only',
                  'not_complete_revision_history':True,'original_snapshot_id':packet['snapshot']['snapshot_id'],
                  'excluded_current':[x for x in packet['snapshot']['current'] if x['state']!='active'],
                  'original_record_hashes':[digest(r) for r in records],'ai_grants_created':[]})
            write(root/'reader-config.json',config)
            reader_code,result,call=invoke('reader',[str(READER/'source/ssr.py'),'--config',str(root/'reader-config.json'),
                    '--request',str(root/'input-query.json')],READER,root)
            report['calls'].append(call); report['reader_invoked']=True
            require(reader_code==READER_EXITS.get(result.get('status')) and result.get('schema_version')=='1.0.1'
                    and result.get('request_id')==query['request_id'],'reader_result_invalid')
            candidates={(r['source_id'],r['record_id'],r['revision']):r for r in records}
            for evidence in result['evidence']:
                record=candidates[(evidence['source_id'],evidence['record_id'],evidence['revision'])]
                require(all(evidence[k]==v for k,v in record.items() if k not in {'access_scope','record_kind','schema_version'})
                        and evidence['source_kind']=='synthetic' and evidence['layer']==query['layer'], 'reader_evidence_mismatch')
            write(root/'reader-result.json',result)
            report.update(status=result['status'],reason_code=result.get('reason_code'),result=result,
                          snapshot_id=packet['snapshot']['snapshot_id'],eligible_record_count=len(records))
            code=reader_code
    except (ValueError,OSError,KeyError,TypeError,RecursionError) as exc:
        report.update(status='held',reason_code=getattr(exc,'code','current_view_input_or_execution_invalid'))
    try:
        after=source_pins(); unchanged=after==before and all(Path(p).is_file() and sha(Path(p).read_bytes())==v for p,v in effective.items())
    except (OSError,ValueError):after={};unchanged=False
    write(root/'source-after.json',after)
    if not unchanged:
        report.pop('result',None); report.update(status='held',reason_code='source_changed_during_execution');code=4
    report['calls']=[]
    for stage in ('provider','reader'):
        execution=root/(stage+'.execution.json')
        if execution.is_file():
            call=read(execution);report['calls'].append(call)
            report[stage+'_invoked']=call['outcome'] in {'returned','timeout'}
    report.update(source_unchanged=unchanged,source_snapshot_sha256=digest(before),exit_code=code)
    write(root/'receipt.json',report)
    return code,report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True);parser.add_argument('--query',required=True);parser.add_argument('--run-dir',required=True)
    args=parser.parse_args(argv)
    code,result=execute(args.profile,args.query,args.run_dir)
    print(json.dumps(result,ensure_ascii=False,allow_nan=False));return code


if __name__=='__main__':raise SystemExit(main())
