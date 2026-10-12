"""真实Runner回归：同批自身写入不能冒充外部陈旧版本。"""
from pathlib import Path
import asyncio
import os
import json

import pytest

import test_node_verification_runtime as node_runtime
from test_node_verification_runtime import READ, TEST, _run, _tool

# 直接复用已有生产入口夹具，不再创建一套Runner测试环境。
node_project = node_runtime.node_project


INITIAL = 'const left = 1;\nconst right = 1;\nmodule.exports = () => left + right;\n'
EXPECTED = 'const left = 2;\nconst right = 3;\nmodule.exports = Object.assign(() => left + right, { left, right });\n'
TASK = 'Update index.js to use left=2 and right=3, and expose left/right on the exported function. Run node --test literal.test.cjs.'
BATCH = [
    _tool('edit_file', path='index.js', old_text='const left = 1;', new_text='const left = 2;'),
    _tool('edit_file', path='index.js', old_text='const right = 1;', new_text='const right = 3;'),
    _tool('edit_file', path='index.js', old_text='module.exports = () => left + right;',
           new_text='module.exports = Object.assign(() => left + right, { left, right });'),
]


def project(workspace):
    (workspace / 'index.js').write_text(INITIAL)
    (workspace / 'literal.test.cjs').write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "const value = require('./index.js');\n"
        "test('value and exported fields', () => {\n"
        " assert.equal(value(), 5); assert.equal(value.left, 2); assert.equal(value.right, 3);\n"
        "});\n"
    )


def test_same_batch_exact_edits_commit_and_reach_real_node_verification(monkeypatch, node_project):
    project(node_project)
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, BATCH, TEST], task=TASK,
    )
    edits = [x for x in trace if x['event'] == 'tool_call' and x['name'] == 'edit_file']
    assert len(edits) == 3 and edits[0]['executed'] and not edits[0]['dispatch_failed']
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert result.status.value == 'completed'
    assert len(requests) == 3
    assert 'STALE_READ' not in str(requests[2]['messages'])
    assert 'stale file read' not in str(requests[2]['messages'])
    assert not any(x['event'] == 'transaction_rollback' for x in trace)
    assert not any(x['event'] == 'stale_read_rejected' for x in trace)
    assert state['verification_contract']['passed'] is True
    assert state['verification_generation'] == state['mutation_generation']
    assert any(x['event'] == 'verification_result' and x['status'] == 'passed' for x in trace)


def test_failed_second_edit_rolls_back_then_same_plan_can_recover(monkeypatch, node_project):
    project(node_project)
    bad = _tool('edit_file', path='index.js', old_text='not present', new_text='wrong')
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, [BATCH[0], bad], BATCH, TEST], task=TASK,
    )
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert result.status.value == 'completed'
    assert 'old_text not found' in str(requests[2]['messages'])
    assert 'stale file read' not in str(requests[2]['messages'])
    assert any(x['event'] == 'transaction_rollback' for x in trace)
    assert state['verification_contract']['passed'] is True


@pytest.mark.parametrize('replace_object', [True, False])
def test_external_replacement_between_edits_stays_stale_and_is_preserved(monkeypatch, node_project, replace_object):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    project(node_project)
    original = WorkspaceFileAccess.write_text
    external = INITIAL.replace('const left = 1;', 'const left = 2;') + '// external change\n'
    changed = False

    def write_then_external(access, path, content, **kwargs):
        nonlocal changed
        value = original(access, path, content, **kwargs)
        if path == 'index.js' and not changed:
            changed = True
            if replace_object:
                temporary = node_project / 'external.tmp'
                temporary.write_text(external)
                os.replace(temporary, node_project / 'index.js')
            else:
                (node_project / 'index.js').write_text(external)
        return value

    monkeypatch.setattr(WorkspaceFileAccess, 'write_text', write_then_external)
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, BATCH, TEST], task=TASK,
    )
    assert changed
    assert (node_project / 'index.js').read_text() == external
    assert result.status.value != 'completed'
    assert 'stale file read' in str(requests[2]['messages'])
    assert any(x['event'] == 'stale_read_rejected' for x in trace)
    assert state['verification_contract']['passed'] is not True


def test_cancel_after_first_mutation_rolls_back_without_replaying(monkeypatch, node_project):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    project(node_project)
    original = WorkspaceFileAccess.write_text
    writes = []

    def cancel_after_write(access, path, content, **kwargs):
        original(access, path, content, **kwargs)
        writes.append(path)
        raise asyncio.CancelledError

    monkeypatch.setattr(WorkspaceFileAccess, 'write_text', cancel_after_write)
    result, *_ = _run(monkeypatch, node_project, [READ, BATCH, TEST], task=TASK)
    assert result.status.value == 'cancelled'
    assert writes == ['index.js']
    assert (node_project / 'index.js').read_text() == INITIAL


def test_committed_batches_continue_without_unnecessary_reread(monkeypatch, node_project):
    project(node_project)
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project, [READ, *[[edit] for edit in BATCH], TEST], task=TASK,
    )
    assert result.status.value == 'completed'
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert not any(x['event'] == 'stale_read_rejected' for x in trace)
    assert state['verification_contract']['passed'] is True


def test_own_delete_then_create_in_same_batch_uses_owned_missing_version(monkeypatch, node_project):
    project(node_project)
    actions = [
        _tool('apply_patch', changes=[{'path': 'index.js', 'op': 'delete'}]),
        _tool('write_file', path='index.js', content=EXPECTED),
    ]
    result, state, trace, requests, *_ = _run(monkeypatch, node_project, [READ, actions, TEST], task=TASK)
    assert result.status.value == 'completed'
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert not any(x['event'] == 'stale_read_rejected' for x in trace)
    assert state['verification_contract']['passed'] is True


def test_dry_run_has_no_published_mutation_and_does_not_change_expected_anchor(monkeypatch, node_project):
    project(node_project)
    dry = _tool('apply_patch', changes=[{'path': 'index.js', 'op': 'replace',
        'old_text': 'const left = 1;', 'new_text': 'const left = 99;'}], dry_run=True)
    result, state, trace, requests, *_ = _run(monkeypatch, node_project, [READ, [dry, *BATCH], TEST], task=TASK)
    assert result.status.value == 'completed'
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert state['verification_contract']['passed'] is True


def test_same_batch_success_cannot_cover_failed_or_denied_verification(monkeypatch, node_project):
    project(node_project)
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, [READ, BATCH, TEST], task=TASK, permission=False,
    )
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert result.status.value != 'completed'
    assert state['verification_contract']['passed'] is not True
    bash = [x for x in trace if x['event'] == 'tool_call' and x['name'] == 'bash']
    assert bash and all(not x['executed'] for x in bash)
    assert permissions and all(not x[2] for x in permissions)


def test_real_failed_test_after_batch_is_not_completed(monkeypatch, node_project):
    project(node_project)
    test = node_project / 'literal.test.cjs'
    test.write_text(test.read_text().replace('value(), 5', 'value(), 7'))
    result, state, trace, requests, *_ = _run(monkeypatch, node_project, [READ, BATCH, TEST], task=TASK)
    assert (node_project / 'index.js').read_text() == EXPECTED
    assert result.status.value != 'completed'
    assert state['verification_contract']['passed'] is False
    assert any(x['event'] == 'tool_call' and x['name'] == 'bash' and x['command_failed'] for x in trace)


def test_recorded_b4_09_unmasks_real_anchor_failure_and_rolls_back(monkeypatch, node_project):
    import hashlib

    root = Path(__file__).resolve().parents[2]
    fixture = json.loads((root / 'tests/fixtures/paid_b4_09_same_batch_edits.json').read_text())
    before = root / fixture['source_public_record'] / fixture['initial_file']
    assert hashlib.sha256(before.read_bytes()).hexdigest() == fixture['initial_sha256']
    target = node_project / 'packages/metrics/Metrics.ts'
    target.parent.mkdir(parents=True)
    target.write_bytes(before.read_bytes())
    edits = [(call['function']['name'], json.loads(call['function']['arguments'])) for call in fixture['calls']]
    result, state, trace, requests, *_ = _run(monkeypatch, node_project,
        [[_tool('read_file', path='packages/metrics/Metrics.ts')], edits, 'Applied the local edit fragment.'],
        task='Update packages/metrics/Metrics.ts: add the mechanism metric import, field and constructor initialization.')
    assert len(requests) == 3
    outcomes = [x for x in trace if x['event'] == 'tool_call' and x['name'] == 'edit_file']
    assert len(outcomes) == 3
    assert all(x['executed'] and not x['dispatch_failed'] for x in outcomes[:2])
    assert outcomes[2]['executed'] and outcomes[2]['dispatch_failed']
    assert target.read_bytes() == before.read_bytes()
    assert 'stale file read' not in str(requests[2]['messages'])
    assert 'old_text not found' in str(requests[2]['messages'])
    assert 'candidate_lines' in str(requests[2]['messages'])
    assert any(x['event'] == 'transaction_rollback' for x in trace)
    assert not any(x['event'] == 'stale_read_rejected' for x in trace)
