"""Offline causal projection. No Provider invocation; immutable snapshot acceptance."""
import hashlib,json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
from analyze import analyze,read,save
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
PRE=OUT.parent/'complex-paired-preflight-2026-09-20'
ISOLATE=ROOT/'tests/evaluation/fixtures/offline_exec.py'
NODE=Path.home()/'.local/lib/python3.13/site-packages/playwright/driver/node'
sys.path.insert(0,str(ROOT))
from nz_coder.runtime.verification.reference_evidence import reference_digest

def state_facts(st):
    ledger=st.get('requirement_ledger') or {}
    return {'mutation_generation':st.get('mutation_generation'),'verification_generation':st.get('verification_generation'),
        'acceptance_mutation_generation':st.get('acceptance_mutation_generation'),
        'unresolved_ids':[x['requirement']['id'] for x in ledger.get('items',[]) if x.get('status')!='satisfied'],
        'requirement_ledger':ledger,'verification_contract':st.get('verification_contract'),
        'authoritative_references':st.get('task_reference_evidence',[]),
        'reference_digest':reference_digest(st.get('task_reference_evidence',[]),st.get('task_reference_omitted_count',0)),
        'task_reference_omitted_count':st.get('task_reference_omitted_count',0)}

def acceptance(case,version,hashes):
    with tempfile.TemporaryDirectory(prefix='post-authority-version-') as td:
        ws=Path(td)/'workspace';ws.mkdir()
        for name,digest in hashes.items():
            assert hashlib.sha256((version/name).read_bytes()).hexdigest()==digest
            dst=ws/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(version/name,dst)
        cmd=([str(NODE),'--test',str(PRE/'acceptance/Q.cjs')] if case=='Q' else
             [sys.executable,'-m','pytest','-q','-p','no:cacheprovider',str(PRE/'acceptance/M.py')])
        env={'PATH':f'{NODE.parent}:{Path(sys.executable).parent}:/usr/bin:/bin','PYTHONPATH':str(ws),'TASK_WORKSPACE':str(ws),
             'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
        argv=[sys.executable,str(ISOLATE),*cmd]
        try:
            p=subprocess.run(argv,cwd=ws,env=env,text=True,capture_output=True,timeout=45)
            return {'argv':argv,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
        except subprocess.TimeoutExpired:
            return {'argv':argv,'exit_code':None,'timeout':True}

def project(case):
    p=OUT/case/'nzcoder';summary=analyze(p)
    snapshots=read(p/'state-snapshots.jsonl');transport=read(p/'transport/state-snapshots.jsonl')
    events=read(p/'runtime-full.jsonl');requests=read(p/'provider-requests.jsonl');responses=read(p/'provider-responses.jsonl')
    norm=json.loads((p/'normalized-responses.json').read_text());byid={x['request_id']:x for x in norm}
    byrequest={x['request_id']:x for x in requests}
    snapbyid={x['seq']:x for x in snapshots}
    rows=json.loads((p/'causal-table.json').read_text())
    boundaries=[e for e in events if e['event']=='terminal_boundary_settled']
    save(p/'terminal-boundary.json',boundaries)
    save(p/'authority-evidence.json',[{'seq':s['seq'],'reason':s['reason'],'request_id':s['request_id'],**state_facts(s.get('state',{}))} for s in snapshots])
    versions=[];seen={}
    for directory,shots in [(p/'workspace-versions',snapshots),(p/'transport/workspace-versions',transport)]:
        if not directory.exists():continue
        for v in sorted(directory.iterdir()):
            h=json.loads((v/'hashes.json').read_text());identity=hashlib.sha256(json.dumps(h,sort_keys=True).encode()).hexdigest()
            occurrences=[{'snapshot_source':str(directory.relative_to(p)),'seq':s['seq'],'time':s['time'],'reason':s['reason'],'request_id':s['request_id'],**state_facts(s.get('state',{}))} for s in shots if s['workspace_version']==int(v.name[1:])]
            if identity in seen:seen[identity]['observations']+=occurrences;continue
            row={'version_source':str(v.relative_to(p)),'source_hashes':h,'identity':identity,'observations':occurrences,'acceptance':acceptance(case,v,h)}
            versions.append(row);seen[identity]=row
    versions.sort(key=lambda x:min(s['time'] for s in x['observations']))
    for i,v in enumerate(versions):v['version']=f'V{i}'
    save(p/'versions.json',{'method':'Post-run offline acceptance on every distinct captured version; not fed to any model. Concurrent write batches may expose only settled file versions; no inferred intermediate bytes.','versions':versions})
    semantic=[];reviews=[];usage=[]
    for row in rows:
        rid=row['request_id'];q=byrequest[rid];r=byid.get(rid,{});nextq=next((x for x in requests if x['request_id']>rid and x['purpose']=='coding'),None)
        bef=snapbyid.get(int(q.get('runtime_snapshot') or 0),{})
        afters=[s for s in snapshots if s['request_id']==rid];aft=afters[-1] if afters else bef
        rawresponse=next((x for x in responses if x['request_id']==rid),{})
        label=('main-' if q['purpose']=='coding' else q['purpose']+'-')+str(q['ordinal'])
        row.update(label=label,before=state_facts(bef.get('state',{})),after=state_facts(aft.get('state',{})),
            terminal_boundary=[b for b in boundaries if b['request_id']==rid],
            next_main_request_id=nextq['request_id'] if nextq else None,
            next_main_visible_messages=nextq['payload'].get('messages') if nextq else None,
            provider_duration_ms=rawresponse.get('duration_ms'),attempt_number=q['ordinal'])
        usage.append({'label':label,'request_id':rid,'purpose':q['purpose'],'provider':'openai-compatible','model':q['payload']['model'],
            'raw_provider_usage':r.get('usage'),'duration_ms':rawresponse.get('duration_ms'),'http_status':rawresponse.get('status'),
            'physical_attempt':q['ordinal'],'cost':None,'cost_source':'Provider did not expose billing data','cache_rule':'cache input is part of prompt; not added again'})
        if q['purpose']!='coding':
            semantic.append({'label':label,'request_id':rid,'purpose':q['purpose'],'visible_input':q['payload']['messages'],
                'runtime_evidence_before':row['before'],'response':r,'runtime_events':[e for e in events if e['request_id']==rid],
                'next_main_request_id':row['next_main_request_id'],'next_main_visible_messages':row['next_main_visible_messages']})
        for call in r.get('tool_calls',[]):
            if call['name']!='review_run_evidence':continue
            try:args=json.loads(call['arguments'])
            except ValueError:args=None
            tools=[t for t in row['tool_executions'] if t['name']=='review_run_evidence']
            rawtools=[t for t in read(p/'tool-results.jsonl') if t['result']['name']=='review_run_evidence' and t['request_id']==rid]
            facts=[{'before':snapbyid.get(t['before_snapshot']), 'after':snapbyid.get(t['after_snapshot'])} for t in rawtools]
            reviews.append({'label':label,'request_id':rid,'raw_tool_call':call,'arguments':args,
                'argument_class':'valid_zero_arg' if args=={} else 'extra_or_invalid_arguments',
                'execution':tools,'runtime_snapshots':facts,'request_state':row['before'],
                'next_main_request_id':row['next_main_request_id'],'next_main_visible_messages':row['next_main_visible_messages']})
    save(p/'causal-table.json',rows);save(p/'semantic-review.json',semantic);save(p/'review-run-evidence.json',reviews)
    save(p/'usage.json',{'per_request':usage,'totals':summary['usage'],'cost':None,'cost_source':'not exposed','cost_display':'cost unknown'})
    save(p/'tool-events.json',read(p/'tool-results.jsonl'))
    save(p/'permission-events.json',read(p/'permissions.jsonl'))
    print(json.dumps({'case':case,'summary':summary,'versions':[(v['version'],v['acceptance'].get('stdout','').splitlines()[-1:] or v['acceptance']) for v in versions],'review_calls':len(reviews)},ensure_ascii=False))

if __name__=='__main__':project(sys.argv[1])
