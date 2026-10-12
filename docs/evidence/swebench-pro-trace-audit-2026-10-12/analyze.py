"""只读100题已冻结证据；按实际请求和工具结果统计，不调用模型或评分器。"""
from collections import Counter
from pathlib import Path
import ast
import hashlib
import json
import shlex

from nz_coder.swebench.policy import strict_private_tool_input_violation

ROOT = Path(__file__).resolve().parents[3]
DOC = Path(__file__).resolve().parent
BASELINE = '99eee2967ba8ff4dbfb17bf241e382b8fa022f5a'
index = json.loads((ROOT / 'docs/evidence/swebench-pro-20-batch4-2026-10-12/cumulative-100-outcomes.json').read_text())
public = {}
for folder in {x['public_batch'] for x in index['tasks']}:
    for path in (ROOT / 'docs/evidence' / folder).rglob('task-outcome.json'):
        row = json.loads(path.read_text())
        public[row['record_root']] = path.parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x]


prefixes = {'go', 'node', 'npm', 'yarn', 'pnpm', 'python', 'python3', 'pytest', 'git',
            'rg', 'grep', 'ls', 'cat', 'head', 'tail', 'wc', 'find', 'sed', 'awk',
            'pwd', 'diff', 'test', 'printf', 'echo', 'make'}
# 四批实际回调的语法树必须一致，才能对历史拒绝使用同一分类口径。
callback_versions = {}
for folder in {x['public_batch'] for x in index['tasks']}:
    source = ROOT / 'docs/evidence' / folder / 'native-entry.py'
    callback = next(x for x in ast.walk(ast.parse(source.read_text()))
                    if isinstance(x, ast.FunctionDef) and x.name == 'permission')
    callback_versions[folder] = hashlib.sha256(ast.dump(callback).encode()).hexdigest()
assert len(set(callback_versions.values())) == 1


def denied_reason(tool, content, permissions):
    name, arguments = tool['name'], tool['input']
    if tool['executed']:
        return 'command_nonzero' if tool['command_failed'] else 'dispatch_error' if tool['dispatch_failed'] else 'executed'
    if content.startswith('Denied by user'):
        matching = [x for x in permissions if x['tool'] == name and x['input'] == arguments]
        if not matching or matching[-1]['allowed'] is not False:
            return 'user_denial_without_matching_callback_record'
        violation = strict_private_tool_input_violation(name, arguments)
        if violation:
            return 'harness_private_path_or_hidden_traversal'
        try:
            tokens = shlex.split(str(arguments.get('command') or ''))
        except ValueError:
            tokens = []
        if name == 'bash' and (not tokens or Path(tokens[0]).name not in prefixes):
            return 'harness_command_prefix'
        return 'harness_denial_other'
    if content.startswith('Denied:') and 'path outside workspace' in content:
        return 'workspace_path_policy'
    if content.startswith('Denied:'):
        return 'other_permission_policy'
    if 'SWE-bench strict' in content or 'SWE-bench strict' in tool.get('output', ''):
        return 'strict_tool_policy'
    if 'stale file read' in content:
        return 'stale_read'
    return 'unexecuted_unknown'


allstats, details, hashes = [], {}, {}
for row in index['tasks']:
    p = public[row['record_root']]
    slot = Path(row['record_root'])
    runtime = lines(p / 'runtime.jsonl')
    permissions = lines(p / 'permissions.jsonl') if (p / 'permissions.jsonl').exists() else []
    state_path = p / 'state.json'
    if not state_path.exists():
        state_path = slot / 'case/result/state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    requests = lines(p / 'provider-requests.jsonl')
    responses = lines(p / 'provider-responses.jsonl')
    delivered, callturn, requestturn = {}, {}, {}
    main = 0
    for request in requests:
        if request['role'] != 'main':
            continue
        main += 1
        requestturn[request['attempt_id']] = main
        for message in request['payload']['messages']:
            if message.get('role') == 'tool':
                delivered.setdefault(message['tool_call_id'], (main, str(message.get('content') or '')))
    assert main == row['main_physical_requests']
    for response in responses:
        if response['role'] != 'main' or not response['response'].get('choices'):
            continue
        n = requestturn[response['attempt_id']]
        for call in response['response']['choices'][0]['message'].get('tool_calls') or []:
            callturn[call['id']] = n
    result_path = p / 'result.json'
    if not result_path.exists():
        result_path = slot / 'case/result/result.json'
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    finaltools = {x['tool_call_id']: str(x.get('content') or '') for x in result.get('messages', []) if x.get('role') == 'tool'}
    tools, significant = [], []
    for lineno, event in enumerate(runtime, 1):
        if event['event'] != 'tool_call':
            if event['event'] in {'verification_result', 'terminal_boundary_settled', 'transaction_rollback',
                                  'workspace_patch_created', 'requirement_completion_blocked', 'tool_failure_diagnostic',
                                  'run_start', 'run_end', 'planning_start', 'task_contract_bootstrapped'}:
                significant.append({'runtime_line': lineno, **event})
            continue
        tid = event['tool_call_id']
        visible = delivered.get(tid)
        content = visible[1] if visible else finaltools.get(tid, event.get('output', ''))
        tools.append({'runtime_line': lineno, 'call_id': tid, 'main_request': callturn.get(tid),
                      'model_received_on_request': visible[0] if visible else None,
                      'name': event['name'], 'input': event['input'], 'executed': event['executed'],
                      'dispatch_failed': event['dispatch_failed'], 'command_failed': event['command_failed'],
                      'is_write': event['is_write'], 'status': event['status'],
                      'reason': denied_reason(event, content, permissions),
                      'model_visible_summary': content[:1800], 'output_chars': len(content)})
    bash = [t for t in tools if t['name'] == 'bash']
    writes = [t for t in tools if t['is_write'] and t['main_request'] is not None]
    mutations = [t for t in writes if t['executed'] and not t['dispatch_failed']]
    verifies = [e for e in significant if e['event'] == 'verification_result']
    rootrel = str(p.relative_to(ROOT))
    stat = {**row, 'public_record': rootrel, 'runtime_state_available': bool(state),
            'tools': len(tools), 'model_tools': sum(t['main_request'] is not None for t in tools),
            'tools_executed': sum(t['executed'] for t in tools),
            'bash_calls': len(bash), 'bash_executed': sum(t['executed'] for t in bash),
            'bash_unexecuted_reasons': dict(Counter(t['reason'] for t in bash if not t['executed'])),
            'bash_command_failures': sum(t['command_failed'] for t in bash),
            'first_write_attempt_request': next((t['main_request'] for t in writes), None),
            'first_successful_write_request': next((t['main_request'] for t in mutations), None),
            'successful_write_calls': len(mutations),
            'verification_events': len(verifies), 'event_counts': dict(Counter(e['event'] for e in runtime)),
            'tool_names': dict(Counter(t['name'] for t in tools)),
            'runtime_state': {key: state.get(key) for key in ['plan_generated', 'task_contract', 'requirement_ledger',
                'verification_contract', 'verification_attempts', 'mutation_generation', 'verification_generation',
                'changed_files_verified', 'completion_review_generation', 'completion_review_rejections',
                'strict_progress_blocks', 'work_phase', 'acceptance_criteria', 'searched_patterns', 'read_files']}}
    allstats.append(stat)
    details[row['instance_id']] = {'public_record': rootrel, 'tool_sequence': tools, 'significant_events': significant}
    for file in ['runtime.jsonl', 'permissions.jsonl', 'state.json', 'model.patch', 'evaluation-result.json',
                 'report.json', 'provider-requests.jsonl', 'provider-responses.jsonl']:
        path = p / file
        if path.exists():
            hashes[str(path.relative_to(ROOT))] = sha(path)
    # 同时固定私有补丁和验收原件；分析和后续修复不得改写它们。
    for file in ['model.patch', 'evaluation-result.json', 'archive-receipt.json']:
        path = slot / file
        if path.exists():
            hashes[str(path)] = sha(path)

assert len(allstats) == 100
aggregate = {'baseline': BASELINE, 'tasks': 100,
             'outcomes': dict(Counter(str(x['official_resolved']) for x in allstats)),
             'bash_unexecuted_reasons': dict(sum((Counter(x['bash_unexecuted_reasons']) for x in allstats), Counter())),
             'bash_calls': sum(x['bash_calls'] for x in allstats),
             'bash_executed': sum(x['bash_executed'] for x in allstats),
             'event_counts': dict(sum((Counter(x['event_counts']) for x in allstats), Counter())),
             'permission_callback_versions': callback_versions, 'paid_calls_this_analysis': 0}
for name, data in [('task-statistics.json', allstats), ('tool-evidence.json', details),
                   ('aggregate.json', aggregate), ('historical-evidence-sha256.json', hashes)]:
    (DOC / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({key: value for key, value in aggregate.items() if key not in ['event_counts', 'permission_callback_versions']}, ensure_ascii=False))
