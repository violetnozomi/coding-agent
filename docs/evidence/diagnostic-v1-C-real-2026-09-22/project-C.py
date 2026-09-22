"""Post-run external projections only; never used by the Agent."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile

from analyze import analyze, read, save

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
sys.path.insert(0,str(ROOT))
from nz_coder.runtime.verification.reference_evidence import reference_digest
SUITE=ROOT/'tests/evaluation/fixtures/agent_core_diagnostic_v1'
spec=importlib.util.spec_from_file_location('validator',SUITE/'validate.py')
validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
P=OUT/'C/nzcoder'


def facts(s):
    ledger=s.get('requirement_ledger') or {}
    return {k:s.get(k) for k in ('mutation_generation','acceptance_mutation_generation','verification_generation',
             'verification_contract','work_phase','primary_recovery_classification','supporting_recovery_classifications',
             'recovery_repair_targets','budget_pressure_zone','completion_review_generation')} | {
             'requirement_ledger':ledger,'unresolved_ids':[i['requirement']['id'] for i in ledger.get('items',[]) if i.get('status')!='satisfied'],
             'task_reference_evidence':s.get('task_reference_evidence',[]),
             'reference_digest':reference_digest(s.get('task_reference_evidence',[]),s.get('task_reference_omitted_count',0)),
             'task_reference_omitted_count':s.get('task_reference_omitted_count',0)}


def test_audit(ws,initial):
    h=validator.hashes(ws)
    old={k:v for k,v in initial.items() if k.startswith('tests/')}
    now={k:v for k,v in h.items() if k.startswith('tests/')}
    functions={}
    for name in now:
        if name.endswith('.py'):
            try:
                tree=ast.parse((ws/name).read_text())
                functions[name]=[{'name':n.name,'line':n.lineno,'text':ast.get_source_segment((ws/name).read_text(),n)}
                                 for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test')]
            except SyntaxError: functions[name]=[{'parse_error':True}]
    return {'tests_hashes':now,'tests_added':sorted(now.keys()-old.keys()),'tests_removed':sorted(old.keys()-now.keys()),
            'tests_modified':sorted(k for k in old.keys()&now.keys() if old[k]!=now[k]),
            'original_test_files_byte_preserved':all(now.get(k)==v for k,v in old.items()),
            'test_functions':functions,'meaningful_tests_obligation':'uncertain',
            'note':'Function bodies support separate evaluator content audit; changed file does not itself prove meaning.'}


def main():
    assert (P/'process.json').exists(), 'Never analyze unfinished run as final'
    summary=analyze(P)
    snapshots=read(P/'state-snapshots.jsonl');events=read(P/'runtime-full.jsonl')
    requests=read(P/'provider-requests.jsonl');responses=read(P/'provider-responses.jsonl')
    normalized=json.loads((P/'normalized-responses.json').read_text())
    byid={r['request_id']:r for r in normalized}
    initial=json.loads((P/'initial-hashes.json').read_text())
    obligations=json.loads((OUT/'preflight/user-obligations.json').read_text())
    versions=[];previous=initial
    for version in sorted((P/'workspace-versions').iterdir()):
        h=json.loads((version/'hashes.json').read_text())
        observations=[s for s in snapshots if s['workspace_version']==int(version.name[1:]) and s.get('settled_snapshot') and s['workspace_hashes']==h]
        if not observations: continue
        with tempfile.TemporaryDirectory(prefix='C-version-evaluator-') as td:
            ws=Path(td)/'workspace';ws.mkdir()
            for name,digest in h.items():
                assert hashlib.sha256((version/name).read_bytes()).hexdigest()==digest
                dst=ws/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(version/name,dst)
            acceptance=validator.acceptance('C_long_horizon',ws)
            public=validator.project_tests('C_long_horizon',ws)
            audit=test_audit(ws,initial)
        checks={r['name']:r['passed'] for r in acceptance['checks']}
        ledger=json.loads(json.dumps(obligations))
        for item in ledger['obligations']:
            item['status']='unknown'
            item['evidence']=[{'check':k,'passed':checks[k],'scope':'partial behavior witness; not full obligation proof'} for k in item['acceptance_checks']]
        versions.append({'version':f'V{len(versions)}','snapshot':version.name,'source_hashes':h,
            'changed_files':sorted(k for k in previous.keys()|h.keys() if previous.get(k)!=h.get(k)),
            'observations':[{'seq':s['seq'],'reason':s['reason'],'request_id':s['request_id'],'transaction_active':s['transaction_active'],**facts(s['state'])} for s in observations],
            'acceptance':acceptance,'project_tests':public,'test_audit':audit,'user_obligations':ledger,
            'spec_hash':h.get('CONFIG_SPEC.md'),'spec_matches_original':h.get('CONFIG_SPEC.md')==initial['CONFIG_SPEC.md']})
        previous=h
    save(P/'versions.json',{'method':'Post-run acceptance and project tests on each settled captured version; never fed to model. One serialized tool transaction may be a partial implementation but is a stable file version.','versions':versions})
    save(P/'test-diff-audit.json',versions[-1]['test_audit'])
    save(P/'user-obligations-final.json',versions[-1]['user_obligations'])
    save(P/'authority-evidence.json',[{'seq':s['seq'],'reason':s['reason'],'request_id':s['request_id'],**facts(s['state'])} for s in snapshots])
    save(P/'verification.json',[e for e in events if 'verif' in e['event']])
    save(P/'terminal-boundary.json',[e for e in events if 'terminal_boundary' in e['event']])
    compaction=[e for e in events if 'compact' in e['event']]
    save(P/'compaction.json',{'observed':bool(compaction),'events':compaction})
    rows=json.loads((P/'causal-table.json').read_text())
    semantic=[];usage=[];reviews=[];timeouts=[]
    for row,q in zip(rows,requests):
        rid=q['request_id'];ss=[s for s in snapshots if s['request_id']==rid]
        before=next((s for s in ss if s['reason']=='before_provider_http_request'),{})
        after=ss[-1] if ss else before
        nextq=next((x for x in requests if x['request_id']>rid and x['purpose']=='coding'),None)
        response=byid.get(rid,{})
        row.update(before=facts(before.get('state',{})),after=facts(after.get('state',{})),
            files_read=[t['tool_input'].get('path') for t in row['tool_executions'] if t['name'] in ('read_file','read_symbol')],
            files_mutated=sorted(k for k in before.get('workspace_hashes',{}).keys()|after.get('workspace_hashes',{}).keys()
                                 if before.get('workspace_hashes',{}).get(k)!=after.get('workspace_hashes',{}).get(k)),
            next_main_request_id=nextq['request_id'] if nextq else None,
            next_main_visible_messages=nextq['payload']['messages'] if nextq else None,
            context_compaction=[e for e in compaction if e['request_id']==rid],
            terminal_decision=[e for e in events if e['request_id']==rid and 'terminal_boundary' in e['event']],
            user_obligation_versions=[v['version'] for v in versions if any(o['request_id']==rid for o in v['observations'])])
        raw=next((r for r in responses if r['request_id']==rid),{})
        usage.append({'request_id':rid,'purpose':q['purpose'],'ordinal':q['ordinal'],
            'model':q['payload']['model'],'provider':'openai-compatible','raw_provider_usage':response.get('usage'),
            'duration_ms':raw.get('duration_ms'),'http_status':raw.get('status'),'cost':None,'cost_source':'Provider billing not exposed'})
        if q['purpose']!='coding':
            semantic.append({'request_id':rid,'purpose':q['purpose'],'label':q['purpose']+'-'+str(q['ordinal']),
                'visible_input':q['payload']['messages'],'runtime_before':row['before'],'response':response,
                'events':[e for e in events if e['request_id']==rid],
                'next_main_visible_messages':row['next_main_visible_messages']})
        for call in response.get('tool_calls',[]):
            if call['name']=='review_run_evidence': reviews.append({'request_id':rid,'call':call,'before':row['before'],'results':[t for t in row['tool_executions'] if t['name']==call['name']]})
        for tool in row['tool_executions']:
            if tool.get('metadata',{}).get('timed_out'):
                timeouts.append({'request_id':rid,'tool':tool,'next_main_request_id':row['next_main_request_id'],'next_main_visible_messages':row['next_main_visible_messages']})
    save(P/'causal-table.json',rows)
    save(P/'semantic-review.json',semantic)
    save(P/'review-run-evidence.json',{'observed':bool(reviews),'calls':reviews})
    save(P/'timeout-recovery.json',{'observed':bool(timeouts),'timeouts':timeouts})
    save(P/'usage.json',{'per_request':usage,'totals':summary['usage'],'cost':None,'cost_display':'cost unknown','cache_rule':'already part of prompt, not added twice'})
    finals=[r for r in normalized if r['purpose']=='coding' and r.get('content')]
    save(P/'final-assistant-visible.json',finals[-1] if finals else None)
    save(P/'permission-events.json',read(P/'permissions.jsonl'))
    lines=['# C real request trajectory','','| Request | Trigger | Tools | G before/after | VG after | Unresolved | Finish |','|---|---|---|---|---|---|---|']
    for r in rows:
        lines.append(f"| {r['purpose']}-{r['ordinal']} (HTTP {r['request_id']}) | {r['trigger']} | {', '.join(t['name'] for t in r['tool_executions'])} | {r['before']['mutation_generation']} / {r['after']['mutation_generation']} | {r['after']['verification_generation']} | {r['after']['unresolved_ids']} | {r.get('response',{}).get('finish_reason')} |")
    (P/'trajectory.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'terminal':summary['terminal'],'usage':summary['usage'],'versions':[(v['version'],v['acceptance']['summary']) for v in versions],'review_calls':len(reviews),'timeouts':len(timeouts)}))


if __name__=='__main__':main()
