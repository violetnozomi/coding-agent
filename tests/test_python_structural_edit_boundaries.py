"""结构化编辑保护完整定义、原始版本和失败时的原件。"""
from __future__ import annotations

import pytest

from nz_coder.state.workdir import scoped_workdir
from nz_coder.tools.python_ast import python_structural_edit


@pytest.mark.parametrize('decorator', ['@mark', '@(\n    mark\n)', '@ \\\n    mark'])
def test_insertion_keeps_decorator_attached_to_original_function(tmp_path, decorator):
    file = tmp_path / 'demo.py'
    file.write_text('def mark(fn):\n    fn.marked = True\n    return fn\n\n' + decorator + '\ndef original():\n    return 42\n')
    with scoped_workdir(tmp_path):
        result = python_structural_edit('demo.py', insertions=[{
            'before_symbol': 'original', 'code': 'def helper():\n    return 7',
        }])
    assert result.startswith('Applied'), result
    scope = {}
    exec(compile(file.read_text(), str(file), 'exec'), scope)
    assert scope['original'].marked
    assert not hasattr(scope['helper'], 'marked')


def test_replacement_replaces_old_decorators_for_method_and_class(tmp_path):
    file = tmp_path / 'demo.py'
    file.write_text('class C:\n    @staticmethod\n    def value():\n        return 1\n')
    with scoped_workdir(tmp_path):
        result = python_structural_edit('demo.py', replacements=[{
            'target': 'C.value', 'code': '@classmethod\ndef value(cls):\n    return cls.__name__',
        }])
    assert result.startswith('Applied'), result
    scope = {}
    exec(file.read_text(), scope)
    assert scope['C'].value() == 'C'
    assert '@staticmethod' not in file.read_text()


@pytest.mark.parametrize('edits', [
    {'replacements': [{'target': 'C', 'code': 'class C: pass'}, {'target': 'C.m', 'code': 'def m(self): pass'}]},
    {'replacements': [{'target': 'C', 'code': 'class C: pass'}], 'insertions': [{'before_symbol': 'C.m', 'code': 'x=1'}]},
    {'insertions': [{'before_symbol': 'C.m', 'code': 'x=1'}]},
    {'replacements': [{'target': 'C', 'code': 'return 7'}]},
])
def test_invalid_or_overlapping_batch_keeps_bytes(tmp_path, edits):
    file = tmp_path / 'demo.py'
    original = b'class C:\n    def m(self):\n        return 1\n'
    file.write_bytes(original)
    with scoped_workdir(tmp_path):
        result = python_structural_edit('demo.py', **edits)
    assert 'Applied' not in result
    assert file.read_bytes() == original


def test_same_anchor_insertions_preserve_input_order(tmp_path):
    file = tmp_path / 'demo.py'
    file.write_text('def original(): pass\n')
    with scoped_workdir(tmp_path):
        result = python_structural_edit('demo.py', insertions=[
            {'before_symbol': 'original', 'code': 'a = 1'},
            {'before_symbol': 'original', 'code': 'b = a + 1'},
        ])
    assert result.startswith('Applied'), result
    scope = {}
    exec(file.read_text(), scope)
    assert scope['b'] == 2


@pytest.mark.parametrize("transactional", [False, True])
def test_external_edit_between_ast_and_commit_is_not_overwritten(tmp_path, monkeypatch, transactional):
    from nz_coder.tools import python_ast
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.state.transaction import TransactionManager

    file = tmp_path / 'demo.py'
    file.write_text('def original(): return 1\n')
    original_prepare = python_ast._prepare_code_lines
    def prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        file.write_text('def external_edit(): return 99\n')
        return result
    monkeypatch.setattr(python_ast, '_prepare_code_lines', prepare)
    transaction = TransactionManager()
    with scoped_workdir(tmp_path), bind_tool_state(txn=transaction if transactional else None):
        if transactional:
            transaction.begin()
        result = python_structural_edit('demo.py', replacements=[{'target': 'original', 'code': 'def original(): return 2'}])
        if transactional:
            transaction.rollback()
    assert 'Applied' not in result
    assert file.read_text() == 'def external_edit(): return 99\n'
