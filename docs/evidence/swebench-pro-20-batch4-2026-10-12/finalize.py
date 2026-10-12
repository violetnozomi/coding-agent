"""本批闭账核对与报告；只读任务原件，不执行模型、工具或评分。"""
from pathlib import Path
from collections import Counter
import hashlib,json,math,shutil,re,os
from dotenv import dotenv_values
root=Path('/home/pyh/nzcoder');doc=root/'docs/evidence/swebench-pro-20-batch4-2026-10-12'
out=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-20-batch4-2026-10-12')
manifest=json.loads((out/'task-manifest.json').read_text());ids=[t['instance_id'] for t in manifest['tasks']]
assert len(ids)==len(set(ids))==20
for prior in ['nz-swebench-pro-v2-20-2026-10-11','nz-swebench-pro-v2-20-batch2-2026-10-11','nz-swebench-pro-v2-40-batch3-2026-10-11']:
 assert not set(ids)&{t['instance_id'] for t in json.loads((out.parent/prior/'task-manifest.json').read_text())['tasks']}
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 return h.hexdigest()
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
assert sha(out/'task-manifest.json')==(doc/'task-manifest.sha256').read_text().strip()
metrics=json.loads((doc/'current-outcomes.json').read_text());assert [r['instance_id'] for r in metrics]==ids
stages=[name for name in ('live','p2','supplement-memory','p3') if (out/name).exists()]
phase_summary=[];allad=[];allsettled=[];allrejections=[];wire=Counter();models=Counter();raw=0;responses_present=0;responses_json=0;attempts={};canonical={r['instance_id']:r for r in metrics}
for stage in stages:
 p=out/stage
 s=json.loads((p/'summary.json').read_text());grant=json.loads((p/'authorization-used.json').read_text())
 rows=[json.loads(x) for x in (p/'ledger.jsonl').read_text().splitlines()]
 allrejections += [{**r,'stage':stage} for r in rows if r['event']=='local_rejection']
 ad=[r for r in rows if r['event']=='admitted'];settled=[r for r in rows if r['event']=='settled']
 assert Counter(r['attempt_id'] for r in ad)==Counter(r['attempt_id'] for r in settled)
 assert all(v==1 for v in Counter(r['attempt_id'] for r in ad).values())
 by={r['attempt_id']:r for r in settled}
 fee=sum(by[a['attempt_id']]['estimated_cost_reported_cny'] if by[a['attempt_id']]['estimated_cost_reported_cny'] is not None else a['estimated_cost_reserved_cny'] for a in ad)
 assert math.isclose(fee,s['estimated_cost_occupied_cny'],abs_tol=1e-8)
 assert fee<=grant['cost_limit']+1e-8
 if stage!='live':
  previous_used=sum(x['occupied_cny'] for x in phase_summary)
  assert math.isclose(grant['previous_used_cny'],previous_used,abs_tol=1e-8)
  assert math.isclose(grant['cost_limit']+previous_used,50,abs_tol=1e-8)
  # 只读核对前序已结算账本的精确串接，不依赖可变的临时目录名。
  predecessor=b''.join((out/r['stage']/'ledger.jsonl').read_bytes() for r in phase_summary)
  assert grant['predecessor_ledger_sha256']==hashlib.sha256(predecessor).hexdigest()
  if 'source_ledger_chain' in grant:
   assert [r['ledger_sha256'] for r in grant['source_ledger_chain']]==[r['ledger_sha256'] for r in phase_summary]
 for a in ad:
  slot=p/a['run'];request=slot/'private-provider'/f"{a['attempt_id']}.request.json";response=slot/'private-provider'/f"{a['attempt_id']}.response.txt"
  assert sha(request)==a['payload_sha256'];body=json.loads(request.read_text())
  wire[json.dumps({k:body.get(k,'unset') for k in ['model','stream','thinking','reasoning_effort','max_tokens','max_completion_tokens']},sort_keys=True)]+=1
  try:data=json.loads(response.read_text()) if response.exists() and response.stat().st_size else None
  except json.JSONDecodeError:data=None
  if response.exists() and response.stat().st_size:responses_present+=1
  if data:
   responses_json+=1
   models[str(data.get('model','unknown'))]+=1
  record=canonical[a['unit']];assert record['record_root']==str(slot)
  folder=doc/{'live':'attempts','p2':'preparation-retry-attempts','p3':'final-unattempted','supplement-memory':'memory-attempts'}[stage]/a['run']
  rawids={x['attempt_id']:x for x in json.loads((folder/'raw-identity.json').read_text())};index=rawids[a['attempt_id']]
  assert index['private_request_sha256']==sha(request)
  assert index['private_response_sha256']==(sha(response) if response.exists() else None)
  raw+=1
 for r in s['tasks']:
  if r['attempt'] and canonical[r['instance_id']]['record_root']==str(p/f"b4-{r['number']:02}"):
   assert r['instance_id'] not in attempts;attempts[r['instance_id']]=(stage,r)
 allad+=ad;allsettled+=settled
 phase_summary.append({'stage':stage,'ledger_sha256':sha(p/'ledger.jsonl'),'physical_main':sum(a['role']=='main' for a in ad),'physical_auxiliary':sum(a['role']=='auxiliary' for a in ad),'occupied_cny':fee,'authorized_remaining_at_start_cny':grant['cost_limit'],'accounting_failures':s['accounting_failures']})
 for name in ['summary.json','authorization-used.json','execution-config.json','continuation-plan.json','resource-order.json']:
  if (p/name).exists():shutil.copy2(p/name,doc/(stage+'-'+name))
assert len(metrics)==20
assert len(attempts)==sum(r['actual_agent_attempt'] for r in metrics)
frozen_code=json.loads((out/'live/execution-config.json').read_text())['code_files_sha256']
assert all(json.loads((out/stage/'execution-config.json').read_text())['code_files_sha256']==frozen_code for stage in stages)
allfee=sum(p['occupied_cny'] for p in phase_summary);assert allfee<=50+1e-8
assert len({a['attempt_id'] for a in allad})==len(allad)
verified=[]
for record in metrics:
 if not record['actual_agent_attempt']:continue
 slot=Path(record['record_root']);report=json.loads((slot/'report.json').read_text())
 assert sha(slot/'model.patch')==report['patch_sha256']
 receipt=json.loads((slot/'archive-receipt.json').read_text());assert sha(Path(receipt['bundle']))==receipt['sha256']
 verdict=json.loads((slot/'evaluation-result.json').read_text());assert verdict['resolved'] is record['official_resolved']
 assert verdict['unchanged_official_verifier'] is True
 verified.append({'instance_id':record['instance_id'],'record_root':str(slot),'patch_sha256':report['patch_sha256'],'bundle_sha256':receipt['sha256'],'official_resolved':verdict['resolved']})
keys=[k for k in (dotenv_values(root/'.env').get('API_KEY'),os.environ.get('NZ_REVIEW_DEEPSEEK_API_KEY')) if k];files=0
for path in doc.rglob('*'):
 if not path.is_file() or '__pycache__' in path.parts:continue
 rawbytes=path.read_bytes();assert all(key.encode() not in rawbytes for key in keys)
 if path.suffix in {'.json','.jsonl'}:
  values=[json.loads(x) for x in rawbytes.splitlines()] if path.suffix=='.jsonl' else [json.loads(rawbytes)]
  def check(x):
   if isinstance(x,dict):
    assert not set(x)&{'reasoning_content','api_key','Authorization'}
    for v in x.values():check(v)
   elif isinstance(x,list):
    for v in x:check(v)
  for v in values:check(v)
 files+=1
unknown=[s['attempt_id'] for s in allsettled if not s.get('usage_valid') or not isinstance(s.get('usage'),dict) or s.get('estimated_cost_reported_cny') is None or s.get('reservation_retained')]
known=sum(s.get('estimated_cost_reported_cny') or 0 for s in allsettled)
usagekeys=['prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens']
observed={k:[s['usage'][k] for s in allsettled if s.get('usage_valid') and isinstance((s.get('usage') or {}).get(k),(int,float))] for k in usagekeys}
observed['reasoning_tokens']=[s['usage']['completion_tokens_details']['reasoning_tokens'] for s in allsettled if s.get('usage_valid') and isinstance(((s.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens'),(int,float))]
usages={k:sum(v) if v else None for k,v in observed.items()}
main_tasks=sum(r['main_model_was_called'] for r in metrics)
account={'manifest_sha256':sha(out/'task-manifest.json'),'fixed_tasks':20,'sdk_started_tasks':len(attempts),'actual_model_tasks':main_tasks,
 'unattempted_numbers':[i+1 for i in range(20) if not metrics[i]['main_model_was_called']], 'phases':phase_summary,
 'main_physical_requests':sum(a['role']=='main' for a in allad),'auxiliary_physical_requests':sum(a['role']=='auxiliary' for a in allad),
 'known_usage_peak_estimate_cny':known,'retained_unknown_or_other_reservation_cny':allfee-known,'estimated_occupied_cny':allfee,
 'unused_authorization_cny':50-allfee,'old_batch_allowance_used':False,'actual_provider_bill_cny':None,
 'unknown_usage_request_ids':unknown,'known_usage_sums':usages,'usage_field_observation_counts':{k:len(v) for k,v in observed.items()},'usage_total_complete':not unknown,
 'wire_configurations':[{**json.loads(k),'requests':v} for k,v in wire.items()],'response_model_counts':dict(models),
 'raw_request_identity_records_verified':raw,'nonempty_raw_response_records':responses_present,'complete_json_response_records':responses_json,'patch_and_archive_records_verified':verified,
 'local_rejection_reasons':dict(Counter(r['reason'] for r in allrejections)),
 'local_rejections_by_bucket':dict(Counter(r['bucket'] for r in allrejections)),
 'public_files_key_scanned':files,'public_reasoning_removed':True}
save(doc/'final-accounting-and-integrity.json',account)
statuses=Counter(r['agent_status'] for r in metrics if r['actual_agent_attempt'])
passed=sum(r['main_model_was_called'] and r['official_resolved'] is True for r in metrics);failed=sum(r['main_model_was_called'] and r['official_resolved'] is False for r in metrics)
unknown_count=sum(not r['main_model_was_called'] or r['official_resolved'] is None for r in metrics)

folders={'live':'attempts','p2':'preparation-retry-attempts','p3':'final-unattempted','supplement-memory':'memory-attempts'}
lines=['# 第四批固定20题：真实并行评估与闭账','',
 f'排除前三批80个题号，新增20个不同题目。实际调用主模型{main_tasks}题，官方独立验收{passed}通过、{failed}未通过、{unknown_count}未知或未运行。Agent终态与功能验收分开记录。','',
 '| 题号 / 轨迹 | 主 / 辅助请求 | Agent终态 | 官方独立验收 |','|---|---:|---|---|']
for n,r in enumerate(metrics,1):
 stage=Path(r['record_root']).parent.name
 relative=f"{folders[stage]}/b4-{n:02}"
 outcome='未调用模型' if not r['main_model_was_called'] else '通过' if r['official_resolved'] is True else '未通过' if r['official_resolved'] is False else '未知'
 lines.append(f"| [{n:02}]({relative}/README.md) | {r['main_physical_requests']} / {r['auxiliary_physical_requests']} | {r['agent_status']} | {outcome} |")
lines += ['',f"实际主/辅助请求{account['main_physical_requests']}/{account['auxiliary_physical_requests']}。本批总授权人民币50元，估算占用{allfee:.8f}元，未知或其他保留{allfee-known:.8f}元，未用{50-allfee:.8f}元在本批结束后关闭，不转为后续授权。供应商实际账单未知。",'',
 '复用现有原生Agent、4路有界并行执行器和共享费用账本；镜像准备串行，大环境依空间准入处理。付费失败题不重跑；前序请求完全结算后，只首次运行零模型调用题。所有阶段Core和原生入口冻结一致，模型、40主请求/6辅助请求及3000秒口径不变。', '',
 '实际出站max_tokens/max_completion_tokens、thinking、reasoning_effort及stream见[逐笔核验](final-accounting-and-integrity.json)。393216为费用预留，非输出限制；未设置不写成medium/false。估算不是供应商账单，缺失usage或缺失字段保留unknown/null和观测数。', '',
 '容器独立工作区、HOME及Session，无网络、非root；密钥仅在宿主Relay。Agent不接触官方隐藏验收，停止后冻结实际补丁，在新鲜原始镜像执行未改的官方验收。公开协议和工具记录脱敏，原始Session/Artifact及Provider原件保留本机并提供SHA256。宿主没有提供正确补丁或向模型泄露验收结果。', '',
 '第13题官方验收停在补丁应用阶段：三个Redis appendonlydir文件already exists，退出97，没有启动官方测试。初态hash只覆盖tracked文件，不能证明冲突文件与初态相同；原补丁保持不变。该项记unknown，不将验收阻断当作测试断言失败或通过。细节见[原始阻断事实](attempts/b4-13/acceptance-blocker.json)。', '',
 '本轮未修改Core、验证器或评分函数。导出阶段排序反例修复：python -m pytest -q tests/evaluation/test_batch4_evidence_chronology.py，2 passed；仍禁止覆盖已付费尝试。未运行全仓或Windows，不重跑未变的检查。历史compaction fake失败test_context_overflow_stops_after_three_compaction_attempts、test_pre_send_and_reactive_compactions_share_one_three_attempt_owner保留，不删除或弱化。', '',
 '每题README与learning.md提供请求→工具事实→补丁→独立验收的面试复盘入口。冻结验收通过只证明其覆盖范围，不追认Agent未运行的测试。本批结果不能证明整体SWE成功率、Core效果提高或并行提速倍数。']
(doc/'final-report.md').write_text('\n'.join(lines)+'\n')
save(doc/'authorization-closed.json',{'authorized_cny':50,'occupied_cny':allfee,'unused_cny':50-allfee,
 'authorization_closed':True,'unused_not_authorized_for_future_tasks':True,'all_admissions_settled':True,
 'actual_provider_bill_cny':None,'unknown_usage_request_ids':unknown})
combined=[]
for batch,folder in enumerate(('swebench-pro-20-2026-10-11','swebench-pro-20-batch2-2026-10-11','swebench-pro-40-batch3-2026-10-11','swebench-pro-20-batch4-2026-10-12'),1):
 previous=root/'docs/evidence'/folder
 frozen=json.loads((previous/'task-manifest.json').read_text())['tasks']
 records=json.loads((previous/'current-outcomes.json').read_text())
 assert [r['instance_id'] for r in records]==[r['instance_id'] for r in frozen]
 for n,r in enumerate(records,1):
  combined.append({'cumulative_number':len(combined)+1,'batch':batch,'batch_number':n,
    'instance_id':r['instance_id'],'main_model_was_called':r['main_physical_requests']>0,
    'agent_status':r['agent_status'],'official_resolved':r['official_resolved'],
    'main_physical_requests':r['main_physical_requests'],'auxiliary_physical_requests':r['auxiliary_physical_requests'],
    'record_root':r['record_root'],'public_batch':folder})
assert len(combined)==len({r['instance_id'] for r in combined})==100
save(doc/'cumulative-100-outcomes.json',{'fixed_unique_tasks':100,'batches':4,
 'main_model_tasks':sum(r['main_model_was_called'] for r in combined),
 'official_pass':sum(r['main_model_was_called'] and r['official_resolved'] is True for r in combined),
 'official_fail':sum(r['main_model_was_called'] and r['official_resolved'] is False for r in combined),
 'unknown_or_unrun':sum(not r['main_model_was_called'] or r['official_resolved'] is None for r in combined),
 'not_a_single_matched_resource_budget_experiment':True,'tasks':combined})
print(json.dumps({'actual_model_tasks':main_tasks,'official_pass':passed,'official_fail':failed,'unknown_or_unrun':unknown_count,
 'main':account['main_physical_requests'],'auxiliary':account['auxiliary_physical_requests'],'estimated_occupied_cny':allfee},ensure_ascii=False))
