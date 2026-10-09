"""真实修改与审查边界的证据归属回归；所有模型边界离线。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nz_coder.runtime.verification import sidecar_verifier as sidecar
from nz_coder.runtime.verification.hooks import StopHookContext
from nz_coder.state.changes import ChangeTracker
from nz_coder.state.workdir import scoped_workdir


def test_tracker_packet_does_not_confuse_prefixes_or_body_paths(tmp_path):
    tracker = ChangeTracker(change_dir=tmp_path / '.changes')
    changes = [('model.pyi', 'STUB'), ('model.py', 'IMPLEMENTATION'),
               ('a.py', 'BODY b/z.py'), ('z.py', 'OWN Z')]
    with scoped_workdir(tmp_path):
        for path, text in changes:
            target = tmp_path / path
            target.write_text('old\n')
            tracker.record_before(path, True, 'old\n')
            target.write_text(text + '\n')
            tracker.record_after(path, True, text + '\n')
        host = SimpleNamespace(change_tracker=tracker, workdir=tmp_path)
        hook = sidecar.SidecarVerifierHook(host, None, env={})
        context, _, _ = hook._evidence(StopHookContext(
            transcript=({'role': 'user', 'content': 'Update implementation'},),
            last_assistant_text='Done', runtime_state={}))
    for path, own in changes:
        hint = dict(context.file_edit_summary)[path]
        assert '+' + own in hint
        for other, text in changes:
            if other != path:
                assert '+' + text not in hint


@pytest.mark.parametrize('header,path', [
    ('--- a/model.pyi\n+++ b/model.pyi', 'model.py'),
    ('--- a/a.py\n+++ b/a.py', 'z.py'),
])
def test_unmatched_single_path_never_receives_another_section(header, path):
    assert sidecar._bounded_diff_hints(header + '\n@@ -1 +1 @@\n-old\n+BODY b/z.py\n', [path]) == {}


@pytest.mark.parametrize('path', ['dir with spaces/a.py', '.hidden.py', '目录/文件.py', 'a"quote.py'])
def test_diff_header_roundtrips_exact_paths(path):
    import json
    old, new = json.dumps('a/' + path), json.dumps('b/' + path)
    diff = f'diff --git {old} {new}\n--- {old}\n+++ {new}\n@@ -1 +1 @@\n-old\n+new\n'
    assert sidecar._bounded_diff_hints(diff, [path]) == {path: diff}
    assert sidecar._bounded_diff_hints(diff, ['different.py']) == {}


def test_rename_creation_deletion_and_ambiguous_paths():
    renamed = 'diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n'
    assert set(sidecar._bounded_diff_hints(renamed, ['old.py', 'new.py'])) == {'old.py', 'new.py'}
    for before, after, path in [('/dev/null', 'b/new.py', 'new.py'), ('a/old.py', '/dev/null', 'old.py')]:
        diff = f'--- {before}\n+++ {after}\n@@ -1 +1 @@\n-old\n+new\n'
        assert set(sidecar._bounded_diff_hints(diff, [path])) == {path}
        assert sidecar._bounded_diff_hints(diff + diff, [path]) == {}


@pytest.mark.parametrize('gate,verdict', [
    ('allow_refund', 'accept'), ('allow_pattern', 'accept'),
    ('allow_refund', 'revise'), ('allow_refund', 'invalid'),
])
def test_authorized_gate_change_reaches_semantic_reviewer(monkeypatch, tmp_path, gate, verdict):
    from tests.runtime.test_requirement_scope_runtime import _run
    (tmp_path / 'payment.py').write_text(
        f'def amount(value, {gate}=False):\n'
        '    if value < 0:\n        raise ValueError("negative")\n    return value\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_amount.py').write_text(
        'import pytest\nfrom payment import amount\n'
        f'def test_new(): assert amount(-3, {gate}=True) == -3\n'
        'def test_default():\n    with pytest.raises(ValueError): amount(-3)\n'
        'def test_positive(): assert amount(3) == 3\n')
    result, state, trace, requests, reviews = _run(monkeypatch, tmp_path,
        f'Fix payment.py: permit negative values only when {gate}=True. Preserve default rejection and positive behavior. Run python -m pytest -q tests.',
        [[('read_file', {'path': 'payment.py'})],
         [('edit_file', {'path': 'payment.py', 'old_text': 'if value < 0:',
                         'new_text': f'if value < 0 and not {gate}:'})],
         [('bash', {'command': 'python -m pytest -q tests'})], 'Implemented and verified.'],
        semantic=(verdict,) * 8, label='authorized-gate-' + gate + '-' + verdict)
    assert reviews, 'risk must not replace the semantic review'
    assert (result.status.value == 'completed') is (verdict == 'accept')
    assert any(e.get('status') == 'passed' for e in trace if e.get('event') == 'verification_result')
    assert gate + '=True' in str(reviews[0]['messages'])
    if verdict != 'accept':
        compatibility = next(item for item in state['requirement_ledger']['items']
                             if item['requirement']['kind'] == 'compatibility')
        assert compatibility['status'] == 'candidate'
        assert not any(e['type'] == 'semantic_review' for e in compatibility['evidence'])
