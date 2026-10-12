"""追加授权闭账：只读原件；不调用模型，不执行测试，不覆盖原授权账目。"""
from pathlib import Path
from collections import Counter
import hashlib
import json
import math
import os
import shutil
from dotenv import dotenv_values

ROOT = Path('/home/pyh/nzcoder')
DOC = Path(__file__).resolve().parent
OUT = Path('/home/pyh/.codex/artifacts/nz-swebench-pro-v2-40-batch3-2026-10-11')
STAGE = OUT / 'supplement'


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def load(path):
    return json.loads(path.read_text())


def save(name, value):
    (DOC / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


original = load(DOC / 'original-current-outcomes.json')
current = load(DOC / 'current-outcomes.json')
authorization = load(DOC / 'supplement-authorization.json')
original_account = load(DOC / 'final-accounting-and-integrity.json')
assert sha(DOC / 'authorization-closed.json') == authorization['closed_original_authorization_sha256']
manifest = load(OUT / 'task-manifest.json')
assert [row['instance_id'] for row in current] == [row['instance_id'] for row in manifest['tasks']]
scope = {row['instance_id'] for row in original if not row['main_model_was_called']}
grant = load(STAGE / 'authorization-used.json')
assert set(grant['scope']) == scope == set(authorization['scope'])
assert grant['cost_limit'] == grant['new_authorization_cny'] == 50
assert grant['closed_original_unused_not_carried'] is True
phases = ['supplement'] + (['supplement-memory'] if (OUT / 'supplement-memory').exists() else [])
folders = {'supplement': 'supplement-attempts', 'supplement-memory': 'memory-attempts'}
phase_records = []
admissions, settlements = [], []
fee = 0
for phase in phases:
    stage = OUT / phase
    summary = load(stage / 'summary.json')
    phase_grant = load(stage / 'authorization-used.json')
    rows = [json.loads(line) for line in (stage / 'ledger.jsonl').read_text().splitlines()]
    received = [{**row, '_phase': phase} for row in rows if row['event'] == 'admitted']
    returned = [row for row in rows if row['event'] == 'settled']
    assert Counter(row['attempt_id'] for row in received) == Counter(row['attempt_id'] for row in returned)
    assert all(n == 1 for n in Counter(row['attempt_id'] for row in received).values())
    assert all(row['unit'] in scope for row in received)
    settled = {row['attempt_id']: row for row in returned}
    phase_fee = sum(settled[row['attempt_id']]['estimated_cost_reported_cny']
                    if settled[row['attempt_id']]['estimated_cost_reported_cny'] is not None
                    else row['estimated_cost_reserved_cny'] for row in received)
    assert math.isclose(phase_fee, summary['estimated_cost_occupied_cny'], abs_tol=1e-8)
    assert phase_fee <= phase_grant['cost_limit'] + 1e-8
    if phase == 'supplement':
        assert {row['instance_id'] for row in summary['tasks']} == scope
    else:
        assert math.isclose(phase_grant['cost_limit'] + fee, 50, abs_tol=1e-8)
        assert math.isclose(phase_grant['previous_used_cny'], fee, abs_tol=1e-8)
        assert phase_grant['predecessor_ledger_sha256'] == sha(STAGE / 'ledger.jsonl')
        assert not {row['unit'] for row in received} & {row['unit'] for row in admissions}
    admissions += received
    settlements += returned
    fee += phase_fee
    phase_records.append({'stage': phase, 'ledger_sha256': sha(stage / 'ledger.jsonl'),
                          'estimated_occupied_cny': phase_fee, 'cost_limit_cny': phase_grant['cost_limit'],
                          'accounting_failures': summary['accounting_failures']})
assert len({row['attempt_id'] for row in admissions}) == len(admissions)
assert fee <= 50 + 1e-8
old_config = load(OUT / 'live/execution-config.json')
config = load(STAGE / 'execution-config.json')
assert config['code_files_sha256'] == old_config['code_files_sha256']
assert config['native_entry_sha256'] == old_config['native_entry_sha256']
wire = Counter()
responses = 0
for admission in admissions:
    slot = OUT / admission['_phase'] / admission['run']
    request = slot / 'private-provider' / (admission['attempt_id'] + '.request.json')
    response = slot / 'private-provider' / (admission['attempt_id'] + '.response.txt')
    index = {row['attempt_id']: row for row in load(DOC / folders[admission['_phase']] / admission['run'] / 'raw-identity.json')}
    assert sha(request) == admission['payload_sha256'] == index[admission['attempt_id']]['private_request_sha256']
    assert (sha(response) if response.exists() else None) == index[admission['attempt_id']]['private_response_sha256']
    try:
        if response.exists() and isinstance(load(response), dict):
            responses += 1
    except json.JSONDecodeError:
        pass
    payload = load(request)
    wire[json.dumps({key: payload.get(key, 'unset') for key in
                    ('model', 'stream', 'thinking', 'reasoning_effort', 'max_tokens', 'max_completion_tokens')}, sort_keys=True)] += 1
verified = []
for before, after in zip(original, current):
    assert before['instance_id'] == after['instance_id']
    if before['main_model_was_called']:
        assert before == after
    if after['instance_id'] not in scope or not after['sdk_launched']:
        continue
    slot = Path(after['record_root'])
    assert slot.parent.name in phases
    report = load(slot / 'report.json')
    assert sha(slot / 'model.patch') == report['patch_sha256']
    receipt = load(slot / 'archive-receipt.json')
    assert sha(Path(receipt['bundle'])) == receipt['sha256']
    verdict = load(slot / 'evaluation-result.json')
    assert verdict['resolved'] is after['official_resolved']
    assert verdict['unchanged_official_verifier'] is True
    verified.append({'instance_id': after['instance_id'], 'patch_sha256': report['patch_sha256'],
                     'bundle_sha256': receipt['sha256'], 'official_resolved': verdict['resolved']})
for phase in phases:
    assert load(OUT / phase / 'execution-config.json')['code_files_sha256'] == old_config['code_files_sha256']
    for name in ('summary.json', 'authorization-used.json', 'execution-config.json', 'continuation-plan.json', 'resource-order.json'):
        shutil.copy2(OUT / phase / name, DOC / (phase + '-' + name))
unknown = [row['attempt_id'] for row in settlements if not row.get('usage_valid')
           or not isinstance(row.get('usage'), dict) or row.get('estimated_cost_reported_cny') is None
           or row.get('reservation_retained')]
main = sum(row['role'] == 'main' for row in admissions)
aux = len(admissions) - main
passed = sum(row['main_model_was_called'] and row['official_resolved'] is True for row in current)
failed = sum(row['main_model_was_called'] and row['official_resolved'] is False for row in current)
unrun = [n for n, row in enumerate(current, 1) if not row['main_model_was_called']]
account = {'new_authorization_cny': 50, 'estimated_occupied_cny': fee, 'unused_cny': 50 - fee,
           'original_estimated_occupied_cny': original_account['estimated_occupied_cny'],
           'combined_estimated_occupied_cny': original_account['estimated_occupied_cny'] + fee,
           'original_unused_not_carried': True, 'actual_provider_bill_cny': None,
           'main_physical_requests': main, 'auxiliary_physical_requests': aux,
           'all_admissions_settled': True, 'unknown_usage_request_ids': unknown,
           'ledger_sha256': sha(STAGE / 'ledger.jsonl'), 'complete_json_responses': responses,
           'wire_configurations': [{**json.loads(key), 'requests': n} for key, n in wire.items()],
           'phases': phase_records, 'patch_and_archive_records_verified': verified,
           'official_pass': passed, 'official_fail': failed, 'unrun_numbers': unrun}
save('supplement-accounting-and-integrity.json', account)
save('supplement-authorization-closed.json', {**account, 'authorization_closed': True,
     'unused_not_authorized_for_future_tasks': True})
lines = ['# 第三批固定40题：追加授权后的结果', '',
         f'实际主模型任务 {40-len(unrun)} 题；官方独立验收 {passed} 通过、{failed} 未通过；未调用模型题号 {unrun}。未运行不计模型失败。', '',
         f'原授权已闭账估算 {original_account["estimated_occupied_cny"]:.8f} 元；追加50元估算占用 {fee:.8f} 元，余款不转为后续授权。供应商实际账单未知。', '',
         f'新增真实主/辅助请求 {main}/{aux}，原主请求819、辅助0。只补零模型任务，原29题不重跑。被冻结的 Core 与原生入口哈希一致。', '',
         '| 题号 / 轨迹 | 主请求 | Agent终态 | 官方独立验收 |', '|---|---:|---|---|']
for n, row in enumerate(current, 1):
    folder = {'live': 'attempts', 'p2': 'preparation-retry-attempts', 'supplement': 'supplement-attempts', 'supplement-memory': 'memory-attempts'}[Path(row['record_root']).parent.name]
    verdict = '未运行模型' if not row['main_model_was_called'] else '通过' if row['official_resolved'] is True else '未通过' if row['official_resolved'] is False else '未知'
    lines.append(f'| [{n:02}]({folder}/b3-{n:02}/README.md) | {row["main_physical_requests"]} | {row["agent_status"]} | {verdict} |')
lines += ['', '[原授权报告](original-final-report.md)、[原闭账账目](final-accounting-and-integrity.json)保持不变。新增账目见 [独立核验](supplement-accounting-and-integrity.json)。', '',
          '公开请求/响应脱敏；完整原件及Session/Artifact保留本机并公开哈希。真实工具验证与独立官方验收分别记录，completed不等于验收通过。', '',
          '未运行全仓或Windows，不报告整体SWE成功率、对齐比例或线上效率提升。历史compaction fake失败保留，预算/Core不扩建。']
(DOC / 'supplement-report.md').write_text('\n'.join(lines) + '\n')
keys = [key for key in (dotenv_values(ROOT / '.env').get('API_KEY'), os.environ.get('NZ_REVIEW_DEEPSEEK_API_KEY')) if key]
for path in DOC.rglob('*'):
    if path.is_file() and '__pycache__' not in path.parts:
        assert all(key.encode() not in path.read_bytes() for key in keys)
print(json.dumps({'main': main, 'aux': aux, 'supplement_estimate_cny': fee,
                  'official_pass': passed, 'official_fail': failed, 'unrun': unrun}))
