"""模型边界离线替换，实际命令、工作区结算与普通收尾保持生产实现。"""
from test_node_verification_runtime import _run, _tool


def test_normal_delivery_with_links_find_and_compound_test(monkeypatch, tmp_path):
    for key in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED',
                'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM', 'NZ_STRICT_LOCAL_TOOLS'):
        monkeypatch.setenv(key, '0')
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / 'source-link').symlink_to('app.py')
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git' / 'private-fixture').write_text('not for enumeration\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_app.py').write_text(
        'import unittest\nfrom app import value\n'
        'class AppTest(unittest.TestCase):\n'
        ' def test_value(self): self.assertEqual(value, 2)\n')
    inspect = "find . -path './.git' -prune -o -type f -print"
    check = 'cd . && python -m unittest discover -s tests 2>&1 | tail -4'
    actions = [
        [_tool('bash', command=inspect)],
        [_tool('edit_file', path='app.py', old_text='value = 1', new_text='value = 2')],
        [_tool('bash', command=check)],
        'Changed the value to 2 and checked it with the local test.',
    ]
    result, state, trace, requests, auxiliary, _permissions = _run(
        monkeypatch, tmp_path, actions,
        task='Change app.py value to 2, inspect the files and verify the behavior.',
        allowed_commands=(inspect, check), limit=8,
    )
    assert result.status.value == 'completed', (result.error, result.final_text)
    verification = result.metadata['verification']
    assert not verification['mutation_scope_unknown']
    assert verification['last_verification']['status'] == 'passed'
    assert state['edits_this_run'] == 1
    assert len(requests) == len(actions)
    assert any('Ran 1 test' in str(request['messages']) for request in requests)
    assert not any(event.get('event') == 'verification_result_ignored' for event in trace)
    assert auxiliary  # 普通候选确实到达交付审查；审查意见由离线边界固定。
