"""真实 AST、持久索引及增量更新中的词法身份，不执行仓库源码。"""
import sqlite3
from types import SimpleNamespace

import pytest

from nz_coder.intelligence.code_index import PersistentCodeIndex, ResolvedCallLocation


def make_index(root, source):
    (root / 'model.py').write_text('class Payload:\n    pass\n')
    (root / 'other.py').write_text('class Payload:\n    pass\n')
    (root / 'factory.py').write_text(source)
    index = PersistentCodeIndex(root)
    index.scan(root, max_files=20)
    return index


def relations(index):
    with sqlite3.connect(index.database_path) as db:
        return (
            db.execute("SELECT raw_name, callee_symbol_id, resolution_kind FROM calls WHERE path='factory.py' ORDER BY line").fetchall(),
            db.execute("SELECT raw_name, target_symbol_id, resolution_kind FROM refs WHERE path='factory.py' ORDER BY line").fetchall(),
        )


@pytest.mark.parametrize('body', [
    'def build(Payload):\n    return Payload()\n',
    'def build():\n    return Payload()\n    Payload = 0\n',
    'def build():\n    global Payload\n    return Payload()\n',
    'def outer(Payload):\n    def build():\n        return Payload()\n    return build\n',
    'def build(flag):\n    if flag:\n        from other import Payload\n    return Payload()\n',
    'def build():\n    [(Payload := x) for x in ()]\n    return Payload()\n',
])
def test_unproven_local_binding_cannot_fall_back_to_import(tmp_path, body):
    index = make_index(tmp_path, 'from model import Payload\n' + body)
    calls, refs = relations(index)
    for rows in (calls, refs):
        selected = [r for r in rows if r[0] == 'Payload']
        assert selected and all(r[1] is None and r[2] == 'lexical-shadowing' for r in selected)
    stats = index.augment_call_targets(SimpleNamespace(resolve=lambda request: ResolvedCallLocation(
        file_path='model.py', line=1, name='Payload')), paths=['factory.py'])
    assert stats.attempted == 0


def test_scoped_imports_and_definitions_do_not_leak(tmp_path):
    index = make_index(tmp_path, '''from model import Payload
def normal():
    return Payload()
def local():
    from other import Payload
    return Payload()
def nested():
    def Payload(): return 42
    return Payload()
def shadow(Payload):
    return Payload()
''')
    calls, refs = relations(index)
    targets = [r[1] for r in calls]
    assert targets[0].startswith('symbol:model.py::')
    assert targets[1].startswith('symbol:other.py::')
    assert 'nested.Payload' in targets[2]
    assert targets[3] is None
    assert [r[1] for r in refs if r[0] == 'Payload'] == targets
    assert relations(PersistentCodeIndex(tmp_path)) == (calls, refs)
    path = tmp_path / 'factory.py'
    path.write_text('from model import Payload\ndef build(Payload):\n    return Payload()\n')
    index.update_paths(['factory.py'])
    assert index.file_calls('factory.py')[0].callee_symbol_id is None
    path.write_text('from model import Payload\ndef build():\n    return Payload()\n')
    index.update_paths(['factory.py'])
    assert index.file_calls('factory.py')[0].callee_symbol_id.startswith('symbol:model.py::')


def test_method_receivers_remain_resolvable_but_static_parameter_is_unknown(tmp_path):
    index = make_index(tmp_path, '''class Runner:
    def run(self):
        return self.step()
    def step(self): return 1
    @staticmethod
    def injected(self):
        return self.step()
def launch():
    return Runner.run()
''')
    calls = index.file_calls('factory.py')
    assert calls[0].resolution_kind == 'self-method'
    assert calls[0].callee_symbol_id.endswith('Runner.step::method')
    assert calls[1].callee_symbol_id is None
    assert calls[2].callee_symbol_id.endswith('Runner.run::method')


def test_old_cache_is_rebuilt_not_backfilled_with_today_hash(tmp_path):
    index = make_index(tmp_path, 'from model import Payload\ndef build():\n    return Payload()\n')
    with sqlite3.connect(index.database_path) as db:
        db.execute('ALTER TABLE files DROP COLUMN content_hash')
        db.execute('ALTER TABLE calls DROP COLUMN lexical_binding_json')
        db.execute('ALTER TABLE refs DROP COLUMN lexical_binding_json')
        db.execute('PRAGMA user_version=5')
    (tmp_path / 'factory.py').write_text('from model import Payload\ndef build(Payload):\n    return Payload()\n')
    rebuilt = PersistentCodeIndex(tmp_path)
    assert not rebuilt.snapshot(['factory.py']).files
    rebuilt.scan(tmp_path, max_files=20)
    assert rebuilt.file_calls('factory.py')[0].callee_symbol_id is None
    assert rebuilt.snapshot(['factory.py']).files[0].content_hash
