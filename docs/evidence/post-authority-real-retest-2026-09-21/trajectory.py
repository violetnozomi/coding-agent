"""Human-readable request tables from the frozen raw evidence."""
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def cell(text,limit=220):
    text=str(text).replace('\n',' ').replace('|','\\|')
    return text if len(text)<=limit else text[:limit]+'…'


def describe(call):
    try:args=json.loads(call['arguments'])
    except (ValueError,TypeError):return call['name']+' (invalid arguments)'
    target=args.get('path') or args.get('command') or args.get('subject') or ''
    if not target and isinstance(args.get('files'),list):
        target=', '.join(str(f.get('path','')) for f in args['files'] if isinstance(f,dict))
    return call['name']+(' '+cell(target,130) if target else '')


def main():
    for case in (tuple(sys.argv[1:]) or ('M','Q')):
        for side in ('infcodex','nzcoder'):
            p=ROOT/case/side
            if not (p/'summary.json').exists():continue
            summary=json.loads((p/'summary.json').read_text())
            rows=json.loads((p/'causal-table.json').read_text())
            lines=[f'# {case} / {side}：实际逐请求轨迹','',
                '编号为实际 HTTP 请求，辅助单列。V 是请求前文件版本；G/AG/VG 分别为 mutation、acceptance mutation 和 verification generation。工具结果不足以判断退出事实时保留 unknown。每行完整参数、原工具输出、模型可见历史和需求状态见 causal-table.json。','',
                '| 请求 / purpose / 主预算余额 | trigger / finish_reason | 模型动作 | 工具事实与状态 | 请求前版本 / ledger | Provider prompt / completion |',
                '|---|---|---|---|---|---|']
            for row in rows:
                response=row['response'] or {};usage=response.get('usage') or {}
                calls=response.get('tool_calls') or []
                actions='; '.join(describe(c) for c in calls) or ('自然语言 final' if response.get('content') else '无文本/无工具')
                facts=[]
                for t in row['tool_executions']:
                    if side=='nzcoder':
                        runtime=any(k.startswith('_nz_runtime') for k in (t.get('tool_input') or {}))
                        status=('未执行' if not t['executed'] else '执行')
                        if t['dispatch_failed']:status+=' / dispatch failed'
                        if t['command_failed']:status+=' / command failed'
                        code=(t.get('metadata') or {}).get('exit')
                        if code is not None:status+=' / exit='+str(code)
                        captured=t.get('recorded_capture_request_id')
                        if captured is not None and captured!=row['request_id']:
                            status+=f' / 完成于 HTTP #{captured} 后'
                        facts.append(t['name']+(' [runtime]' if runtime else '')+': '+status)
                    else:
                        code=t.get('exit_code')
                        facts.append(t['name']+': '+('exit='+str(code) if code is not None else 'tool.result；exit unknown'))
                if not facts and calls:
                    if row['purpose'] != 'coding':
                        facts=['辅助模型结构化 verdict；消费结果见 runtime-full.jsonl']
                    elif all(c['name'].startswith('todo_') for c in calls):
                        facts=['无公开 tool.result（todo 隐藏执行状态不可由此判断）']
                    else:
                        facts=['无公开 tool.result；执行状态 unknown']
                st=row.get('state_before') or {}
                version='V'+str(row.get('workspace_version_before'))
                if st:
                    version+=f"; G/AG/VG={st.get('mutation_generation')}/{st.get('acceptance_mutation_generation')}/{st.get('verification_generation')}"
                    ledger=(st.get('requirement_ledger') or {}).get('items') or []
                    version+='; '+','.join(x['requirement']['id']+'='+x['status'] for x in ledger)
                else:version+='; ledger=N/A'
                lines.append('| '+ ' | '.join([
                    f"{row['request_id']} / {row['purpose']} #{row['ordinal']} / {row['main_budget_before']}",
                    cell(str(row.get('trigger'))+' / '+','.join(response.get('finish_reason') or [])),
                    cell(actions,650),cell('; '.join(facts),650),cell(version,350),
                    f"{usage.get('prompt_tokens','unknown')} / {usage.get('completion_tokens','unknown')}"
                ])+' |')
            lines+=['','主模型后续实际能看到哪些工具结果，以下一请求的 model_visible_tool_results 为准。终答后的 runtime-owned 验证可能不再进入任何主请求；不能倒推模型看见了最终 ledger。','',
                    '最终终端原事实：','```json',json.dumps(summary['terminal'],ensure_ascii=False,indent=2),'```','',
                    f"冻结独立验收整体通过：{summary['acceptance_passed']}。usage 见 usage.json；费用 cost unknown。"]
            (p/'trajectory.md').write_text('\n'.join(lines)+'\n')


if __name__=='__main__':main()
