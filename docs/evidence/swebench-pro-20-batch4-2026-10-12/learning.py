"""复用既有事实式学习索引，不编造推理或测试结论。"""
from pathlib import Path
import json
root=Path('/home/pyh/nzcoder/docs/evidence/swebench-pro-20-batch4-2026-10-12')
metrics=json.loads((root/'current-outcomes.json').read_text())
for n,r in enumerate(metrics,1):
 if not r['main_model_was_called']:continue
 stage={'live':'attempts','p2':'preparation-retry-attempts','p3':'final-unattempted','supplement':'supplement-attempts','supplement-memory':'memory-attempts'}[Path(r['record_root']).parent.name]
 n=int(Path(r['record_root']).name.split('-')[-1])
 folder=root/stage/Path(r['record_root']).name
 tools=[json.loads(x) for x in (folder/'runtime.jsonl').read_text().splitlines() if json.loads(x).get('event')=='tool_call']
 failed=next((x for x in tools if x.get('name')=='bash' and x.get('executed') is True and x.get('command_failed') is True),None)
 write=next((x for x in tools if x.get('is_write') and x.get('executed') is True and x.get('status')=='ok'),None)
 lines=[f'# 第 {n:02} 题复盘练习','',f"实例 `{r['instance_id']}`。真实主/辅助请求 {r['main_physical_requests']}/{r['auxiliary_physical_requests']}，Agent 终态 `{r['agent_status']}`，官方独立结果 `{r['official_resolved']}`。",'', '按实际记录阅读：[请求](provider-requests.jsonl) → [响应](provider-responses.jsonl) → [工具事实](runtime.jsonl) → [补丁](model.patch) → [独立验收](task-outcome.json)。原始请求/响应的哈希配对见 [原件身份](raw-identity.json)。', '']
 if failed:lines.append(f"首次记录的真实 Bash 非零结果：工具调用 `{failed.get('tool_call_id')}`。非零退出不自动证明测试断言失败；结合对应命令及输出分类。")
 else:lines.append('没有记录到 executed=true 且 command_failed=true 的 Bash 结果；不能据此推断所有必需测试已运行。核对拒绝、工具失败和独立验收。')
 if write:lines.append(f"首个成功写工具结果：`{write.get('name')}` / `{write.get('tool_call_id')}`。核对实际 diff，工具成功不自动证明需求满足。")
 else:lines.append('没有记录到成功执行的写工具；核对补丁和是否需要修改，不能用 completed 推断有有效修复。')
 lines+=['','面试练习：','', '1. 在原始请求中找出模型修改前实际获得的定位证据；哪些信息不足，下一步该读哪里？', '2. 对照 patch 和真实测试命令，解释修改假设、验证覆盖及未证明的边界。', '3. 比较 Agent 终态与冻结补丁的独立验收；指出权限拒绝、环境失败、测试失败与功能未完成的区别。', '', '公开文件不含私有推理正文。这里的提问用于复盘，不替模型补造推理或测试事实。']
 (folder/'learning.md').write_text('\n'.join(lines)+'\n')
print('learning records updated')
