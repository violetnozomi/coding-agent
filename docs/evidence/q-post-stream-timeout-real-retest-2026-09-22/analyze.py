"""Offline projections of actual requests/events; no Agent/model invocation."""
import json
import re
from pathlib import Path

OUT=Path(__file__).resolve().parent


def read(path):
    if not path.exists():return []
    result=[]
    for line in path.read_text().splitlines():
        try:result.append(json.loads(line))
        except json.JSONDecodeError:pass  # concurrent final partial line; rerun after freeze
    return result


def normalize(response):
    pieces=response.get('chunks') or [response.get('json',{})]
    content='';calls={};finish=[];usage=None
    for piece in pieces:
        if piece.get('usage'):usage=piece['usage']
        for c in piece.get('choices',[]):
            if c.get('finish_reason'):finish.append(c['finish_reason'])
            d=c.get('delta',c.get('message',{}));content+=d.get('content') or ''
            for i,t in enumerate(d.get('tool_calls') or []):
                idx=t.get('index',i);v=calls.setdefault(idx,{'id':'','name':'','arguments':''})
                f=t.get('function',{})
                for key,value in [('id',t.get('id')),('name',f.get('name')),('arguments',f.get('arguments'))]:
                    v[key]+=value or ''
    return {'request_id':response['request_id'],'purpose':response['purpose'],
            'http_status':response.get('status'),'finish_reason':finish,'content':content,
            'tool_calls':list(calls.values()),'usage':usage,
            'transport_error_type':response.get('transport_error_type')}


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def analyze(path):
    requests=read(path/'provider-requests.jsonl')
    responses=[normalize(r) for r in read(path/'provider-responses.jsonl')]
    byid={r['request_id']:r for r in responses}
    runtime=read(path/'runtime-full.jsonl')
    stdout=read(path/'stdout.jsonl')
    tools=read(path/'tool-results.jsonl')
    # Capture IDs are the latest HTTP request at tool completion. An auxiliary
    # stall check can run inside dispatch of a prior main-model tool call.
    projected_tools=[]
    for t in tools:
        result=dict(t['result'])
        captured=t['request_id'];origin=captured
        if byid.get(captured,{}).get('purpose') != 'coding':
            args=result.get('tool_input') or {}
            if not any(k.startswith('_nz_runtime') for k in args):
                for response in reversed(responses):
                    if response['request_id']>captured or response['purpose']!='coding':continue
                    matching=False
                    for call in response['tool_calls']:
                        try:call_args=json.loads(call['arguments'])
                        except (ValueError,TypeError):continue
                        if call['name']==result['name'] and call_args==args:matching=True
                    if matching:
                        origin=response['request_id'];break
        result['recorded_capture_request_id']=captured
        result['origin_request_id']=origin
        projected_tools.append({'request_id':origin,'result':result})
    snapshots=read(path/'state-snapshots.jsonl')
    transport=read(path/'transport/state-snapshots.jsonl')
    turns=[r for r in runtime if r['event']=='provider_turn_started']
    rows=[]
    for q in requests:
        rid=q['request_id'];reply=byid.get(rid)
        before=next((s for s in snapshots if s['reason']=='before_provider_http_request' and s['request_id']==rid),None)
        before_transport=next((s for s in transport if s['seq']==q['transport_snapshot']),{})
        starts=[e for e in stdout if e.get('type')=='tool.start' and e.get('capture_request_id')==rid]
        results=[e for e in stdout if e.get('type')=='tool.result' and e.get('capture_request_id')==rid]
        executions=[]
        if path.name=='nzcoder':
            executions=[r['result'] for r in projected_tools if r['request_id']==rid]
        else:
            for result in results:
                start=next((e for e in starts if e.get('id')==result.get('id')), {})
                text=result.get('content','');exit_match=re.search(r'^Exit:\s*(-?\d+)\s*$',text,re.M)
                executions.append({'name':result['name'],'tool_input':start.get('input'),
                    'output':text,'exit_code':int(exit_match[1]) if exit_match else None,
                    'execution_source':'reference tool.result; missing structured status remains unknown',
                    'id':result.get('id')})
        state=(before or {}).get('state',{})
        row={'request_id':rid,'purpose':q['purpose'],'ordinal':q['ordinal'],
             'main_budget_before':q['main_budget_before'],'response':reply,
             'trigger':next((t['reason'] for t in turns if t['turn']==q['ordinal']),None) if q['purpose']=='coding' else q['purpose'],
             'workspace_version_before':before_transport.get('workspace_version'),
             'workspace_hashes_before':before_transport.get('workspace_hashes'),
             'state_before':state if path.name=='nzcoder' else None,
             'tool_executions':executions,
             'model_visible_tool_results':[{'tool_call_id':m.get('tool_call_id'),'content':m.get('content')}
                                           for m in q['payload'].get('messages',[]) if m.get('role')=='tool'],
             'request_options':{k:q['payload'][k] for k in ('model','stream','max_tokens','max_completion_tokens','thinking','reasoning_effort') if k in q['payload']}}
        rows.append(row)
    save(path/'normalized-responses.json',responses);save(path/'causal-table.json',rows)
    totals={}
    for r in responses:
        kind='main' if r['purpose']=='coding' else 'auxiliary'
        bucket=totals.setdefault(kind,{'requests':0,'usage_available':0})
        bucket['requests']+=1
        if r['usage']:
            bucket['usage_available']+=1
            for k in ('prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens'):
                if k in r['usage']:bucket[k]=bucket.get(k,0)+r['usage'][k]
    save(path/'usage.json',{'per_request':[{'request_id':r['request_id'],'purpose':r['purpose'],'usage':r['usage']} for r in responses],
                          'totals':totals,'cost':'cost unknown','cache_rule':'already included in prompt total'})
    terminal=None
    if (path/'result.json').exists():terminal=json.loads((path/'result.json').read_text()).get('status')
    else:terminal=next((r for r in reversed(stdout) if r.get('type')=='run.result'),None)
    acceptance=json.loads((path/'acceptance.json').read_text()) if (path/'acceptance.json').exists() else None
    summary={'case':path.parent.name,'side':path.name,'requests':len(requests),
             'responses':len(responses),'usage':totals,'terminal':terminal,
             'acceptance_passed':acceptance['passed'] if acceptance else None,
             'checks':{k:{'exit_code':v.get('exit_code'),'tail':v.get('stdout','')[-1500:]} for k,v in (acceptance or {}).get('checks',{}).items()},
             'process':json.loads((path/'process.json').read_text()) if (path/'process.json').exists() else None,
             'tool_executions':sum(len(r['tool_executions']) for r in rows),
             'normalization_notes':'reference missing structured statuses not synthesized; runtime-owned NZ tools separate from model tools'}
    save(path/'summary.json',summary)
    return summary


def main():
    summaries=[]
    for case in ('M','Q','S'):
        for side in ('infcodex','nzcoder'):
            path=OUT/case/side
            if (path/'provider-requests.jsonl').exists():summaries.append(analyze(path))
    save(OUT/'comparison-summary.json',summaries)
    for s in summaries:
        terminal=s['terminal']
        if isinstance(terminal,dict):terminal={k:v for k,v in terminal.items() if k in ('type','success','stopReason','error')}
        print(json.dumps({'case':s['case'],'side':s['side'],'requests':s['requests'],'terminal':terminal,'acceptance':s['acceptance_passed']},ensure_ascii=False))


if __name__=='__main__':main()
