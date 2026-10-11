"""本轮已结束实例的脱敏导出；不调用模型，不评分、不改任务文件。"""
from pathlib import Path
import collections, hashlib, json, shutil, sys
from dotenv import dotenv_values
ROOT=Path('/home/pyh/nzcoder');OUT=Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-40-batch3-2026-10-11')
STAGE=sys.argv[1] if len(sys.argv)>1 else 'live'
PUBLIC={'live':'attempts','preparation-retry':'preparation-blocked','p2':'preparation-retry-attempts','p3':'final-unattempted'}[STAGE]
LIVE=OUT/STAGE;DOC=ROOT/'docs/evidence/swebench-pro-40-batch3-2026-10-11';KEY=dotenv_values(ROOT/'.env').get('API_KEY')
def clean(x):
 if isinstance(x,dict):return {k:clean(v) for k,v in x.items() if k not in {'reasoning_content','api_key','Authorization'}}
 if isinstance(x,list):return [clean(v) for v in x]
 return x
def dump(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
rows=[json.loads(x) for x in (LIVE/'ledger.jsonl').read_text().splitlines()]
summary=[]
for s in sorted(LIVE.glob('b3-[0-9][0-9]')):
 if not (s/'report.json').exists():continue
 report=json.loads((s/'report.json').read_text());dst=DOC/PUBLIC/s.name;dst.mkdir(parents=True,exist_ok=True)
 for name in ('instruction.md','task.toml'):
  source=OUT/'public-tasks'/report['instance_id']/name
  if source.exists():shutil.copy2(source,dst/name)
 admissions=[x for x in rows if x['event']=='admitted' and x['run']==s.name]
 ids={x['attempt_id'] for x in admissions};settlements=[x for x in rows if x['event']=='settled' and x['attempt_id'] in ids]
 estimates=[x.get('estimated_cost_reported_cny') for x in settlements]
 rawindex=[];requests=[];responses=[]
 for number,a in enumerate(admissions,1):
  aid=a['attempt_id'];p=s/'private-provider'/f'{aid}.request.json';r=s/'private-provider'/f'{aid}.response.txt'
  pb=p.read_bytes() if p.exists() else None;rb=r.read_bytes() if r.exists() else None
  if pb is not None:
   assert hashlib.sha256(pb).hexdigest()==a['payload_sha256']
   requests.append({'request_number':number,'role':a['role'],'attempt_id':aid,'payload':clean(json.loads(pb))})
  if rb is not None:
   try:body=json.loads(rb)
   except json.JSONDecodeError:body={'partial_raw_response':rb.decode(errors='replace'),'complete_json':False}
   # 非完整 JSON 原件仅保留哈希，避免公开推理字段的半截流。
   if isinstance(body,dict) and body.get('complete_json') is not False:
    responses.append({'request_number':number,'role':a['role'],'attempt_id':aid,'response':clean(body)})
  rawindex.append({'request_number':number,'role':a['role'],'attempt_id':aid,'private_request_sha256':hashlib.sha256(pb).hexdigest() if pb is not None else None,'private_response_sha256':hashlib.sha256(rb).hexdigest() if rb is not None else None,'public_reasoning_content_removed':True})
 for n,v in [('provider-requests.jsonl',requests),('provider-responses.jsonl',responses)]:
  (dst/n).write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in v))
 dump(dst/'raw-identity.json',rawindex)
 for name in ['report.json','identity.json','environment.json','image-pull.txt','image-build.txt','initial-files.json','model.patch','patch-identity.json','agent-exit.json','agent-stdio.txt','evaluation-result.json','evaluation-command.json','evaluation-stdio.txt','archive-receipt.json','launch.json','image-preflight.json','evaluation-corrected-result.json','evaluation-corrected-command.json','evaluation-corrected-stdio.txt','delivery-recovery.json','permission-classification-audit.json','case-path-alias.json']:
  source=s/name
  if source.exists():shutil.copy2(source,dst/name)
 if (s/'case/input/run.json').exists():shutil.copy2(s/'case/input/run.json',dst/'model-config.json')
 for tree in ['final-files','initial-changed-files']:
  if (s/tree).is_dir():shutil.copytree(s/tree,dst/tree,dirs_exist_ok=True)
 result=s/'case/result'
 for name in ['result.json','runtime.jsonl','permissions.jsonl','state.json','cleanup.json','child-environment.json','runtime-recovery.json']:
  source=result/name
  if not source.exists():continue
  if name.endswith('.jsonl'):(dst/name).write_text(''.join(json.dumps(clean(json.loads(x)),ensure_ascii=False)+'\n' for x in source.read_text().splitlines()))
  else:dump(dst/name,clean(json.loads(source.read_text())))
 for directory in ['evaluation','evaluation-corrected']:
  if (s/directory).is_dir():shutil.copytree(s/directory,dst/directory,dirs_exist_ok=True)
 (dst/'ledger.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows if x.get('run')==s.name or x.get('attempt_id') in ids or str(x.get('bucket','')).split('/')[1:2]==[s.name]))
 tools=[]
 if (result/'runtime.jsonl').is_file():tools=[json.loads(x) for x in (result/'runtime.jsonl').read_text().splitlines() if json.loads(x).get('event')=='tool_call']
 command_tools=[x for x in tools if x.get('name')=='bash']
 writes=[x for x in tools if x.get('is_write') and x.get('executed') and x.get('status')=='ok']
 corrected=s/'evaluation-corrected-result.json'
 final_grade=json.loads(corrected.read_text()) if corrected.exists() else json.loads((s/'evaluation-result.json').read_text()) if (s/'evaluation-result.json').exists() else None
 envblocked=(s/'evaluation/run-script-stderr.txt').exists() and 'unrecognized arguments: --reruns' in (s/'evaluation/run-script-stderr.txt').read_text()
 recovery_info=json.loads((s/'delivery-recovery.json').read_text()) if (s/'delivery-recovery.json').exists() else {}
 envblocked=envblocked or bool(recovery_info.get('initial_grade_was_not_run'))
 resolved=final_grade.get('resolved') if final_grade else None
 if envblocked and not corrected.exists():resolved=None
 actual_attempt=report['attempt'] if (s/'launch.json').exists() or admissions else 0
 metrics={'instance_id':report['instance_id'],'declared_attempt_before_launch':report['attempt'],'actual_agent_attempt':actual_attempt,'agent_status':report.get('agent_status'),'report_status':report['status'],'main_physical_requests':sum(a['role']=='main' for a in admissions),'auxiliary_physical_requests':sum(a['role']=='auxiliary' for a in admissions),'reported_usage_estimate_cny':sum(estimates) if all(x is not None for x in estimates) else None,'official_resolved':resolved,'initial_grade_environment_blocked':envblocked,'corrected_official_grade_present':corrected.exists(),'agent_dependency_isolation_revision':json.loads((result/'child-environment.json').read_text()).get('harness_revision') if (result/'child-environment.json').exists() else 1,'native_entry_sha256':hashlib.sha256((s/'case/driver/entry.py').read_bytes()).hexdigest() if (s/'case/driver/entry.py').exists() else None,'host_patch_or_model_hints':False,'record_root':str(s),'public_reasoning_removed':True}
 metrics['known_reported_usage_estimate_cny']=sum(x for x in estimates if x is not None)
 metrics['retained_unknown_request_reservation_cny']=sum(a.get('estimated_cost_reserved_cny',0) for a in admissions if any(z['attempt_id']==a['attempt_id'] and z.get('reservation_retained') for z in settlements))
 metrics['unknown_usage_request_ids']=[z['attempt_id'] for z in settlements if not z.get('usage_valid')]
 metrics['outer_status']=json.loads((s/'agent-exit.json').read_text()).get('outer_status') if (s/'agent-exit.json').exists() else None
 metrics['runtime_trace_source']='recovered_raw_journal' if (result/'runtime-recovery.json').exists() else 'native_export' if (result/'runtime.jsonl').exists() else None
 dump(dst/'task-outcome.json',metrics)
 dump(dst/'command-evidence.json',[{k:x.get(k) for k in ['tool_call_id','input','status','executed','dispatch_failed','command_failed','output']} for x in command_tools])
 if actual_attempt==0:
  (dst/'README.md').write_text(f"# 第 {s.name} 题：未运行记录\n\n实例：`{report['instance_id']}`。\n\n尚未执行 Agent，没有模型请求，没有补丁或验收成绩。状态 `{report['status']}`，原因：`{report.get('reason',report.get('error'))}`。\n\n[原始状态](report.json)和[有效口径](task-outcome.json)保留准备失败或全局止损事实。冻结题面和名单不变，不能将本项计为测试失败或通过。\n")
 # 事实式学习索引；不编造模型思考或把任意日志文字转成通过。
 if actual_attempt!=0:
  modified=list((s/'final-files').rglob('*')) if (s/'final-files').exists() else []
  names=[str(p.relative_to(s/'final-files')) for p in modified if p.is_file()]
  lines=[f'# 第 {s.name} 题：执行与学习记录','',f'实例：`{report["instance_id"]}`。Agent 状态 `{report.get("agent_status")}`；官方有效结果 `{resolved}`，首次环境阻断 `{envblocked}`。','',f'实际主/辅助请求 {metrics["main_physical_requests"]}/{metrics["auxiliary_physical_requests"]}；已返回 usage 的峰时估算 {metrics["reported_usage_estimate_cny"]} 元，非账单。', '', '改变文件：'+(', '.join(names) if names else '未取得可冻结改动。'), '', '按 [请求](provider-requests.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [有效结果](task-outcome.json) 顺序复盘。原始请求/响应哈希见 raw-identity.json，完整 Session/Artifact 私有归档位置见 archive-receipt.json。', '', '实际命令（执行与未执行分开；这里只引用本次结构化工具事实）：','']
  for x in command_tools:lines.append(f'- executed={x.get("executed")}, status={x.get("status")}, command_failed={x.get("command_failed")}: `{str(x.get("input",{}).get("command","")).replace(chr(10)," ↵ ")[:400]}`')
  lines+=['','面试复盘练习：从首次诊断找到修改依据，核对实际测试覆盖和改动版本；解释拒绝/环境失败/测试断言失败的差别；对照独立验收遗漏与最终说明，指出仍未证明的行为。受控 smoke 不计入本题，完整推理原件不公开。']
  (dst/'README.md').write_text('\n'.join(lines)+'\n')
 assert not KEY or all(KEY.encode() not in p.read_bytes() for p in dst.rglob('*') if p.is_file())
 summary.append(metrics)
dump(DOC/(STAGE+'-outcomes.json'),summary)
# 同一实例仅准备失败可被后续实际尝试替代；每一阶段的原件仍独立保留。
canonical={}
for folder in ('attempts','preparation-blocked','preparation-retry-attempts','final-unattempted'):
 for p in sorted((DOC/folder).glob('b3-*/task-outcome.json')):
  item=json.loads(p.read_text());old=canonical.get(item['instance_id'])
  if old is not None:
   assert old['actual_agent_attempt']==0, 'refuse_to_replace_paid_attempt'
  canonical[item['instance_id']]=item
order={task['instance_id']:i for i,task in enumerate(json.loads((DOC/'task-manifest.json').read_text())['tasks'])}
summary=sorted(canonical.values(),key=lambda row:order[row['instance_id']])
dump(DOC/'current-outcomes.json',summary)
print(json.dumps({"published_tasks":len(summary),"attempted":sum(x["actual_agent_attempt"]>0 for x in summary),"passed":sum(x["official_resolved"] is True for x in summary)},ensure_ascii=False))
