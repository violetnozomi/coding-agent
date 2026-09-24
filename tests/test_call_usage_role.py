"""Production call-site metadata: no role inference in tests or downstream APIs."""
import ast
import dataclasses
import json
import os
from pathlib import Path
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from nz_coder.intelligence.analyzers import PythonAstAnalyzer, RawCallRecord, LexicalFallbackAnalyzer
from nz_coder.intelligence.code_index import CallEdge, PersistentCodeIndex, ResolvedCallLocation
from nz_coder.intelligence.service import RepoIntelligenceService

ROOT = Path(__file__).resolve().parents[1]


def record(name, data):
    if output := os.environ.get('NZ_CALL_ROLE_OUTPUT'):
        p = Path(output) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2) + '\n')


CASES = [
    ('return Payload()', ['returned']),
    ('AuditMarker()', ['discarded']),
    ('return Payload(AuditMarker())', ['returned', 'argument']),
    ('return Payload(Payload())', ['returned', 'argument']),
    ('payload = Payload()\n    return payload', ['assigned']),
    ('marker = AuditMarker()\n    return Payload()', ['assigned', 'returned']),
    ('yield Payload()', ['yielded']),
    ('if Validator():\n        return Payload()', ['condition', 'returned']),
    ('while Validator():\n        break', ['condition']),
    ('return 1 if Validator() else 2', ['condition']),
    ('assert Validator()', ['condition']),
    ('return wrap(Payload())', ['returned', 'argument']),
    ('return wrap(value=Payload())', ['returned', 'argument']),
    ('if flag:\n        return Payload()\n    return AuditMarker()', ['returned', 'returned']),
    ('return unknown_factory()', ['returned']),
    ('return factory.build()', ['returned']),
    ('return Payload() or AuditMarker()', ['unknown', 'unknown']),
    ('if Payload() and AuditMarker():\n        pass', ['unknown', 'unknown']),
    ('yield from Payload()', ['unknown']),
    ('return (x := Payload())', ['assigned']),
    ('x: object = Payload()', ['assigned']),
    ('return [Payload()]', ['unknown']),
]


def analyze(source):
    return PythonAstAnalyzer().analyze_file(path=Path('subject.py'), relative='subject.py',
                                           source=source, language='python')


@pytest.mark.parametrize('body,expected', CASES)
def test_ast_roles(body, expected, monkeypatch):
    original = ast.parse
    counts = []
    def parse(*a, **kw):
        counts.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(ast, 'parse', parse)
    calls = analyze('def decode(flag=False):\n    ' + body + '\n').calls
    assert [c.usage_role for c in calls] == expected
    assert len(counts) == 1


@pytest.fixture
def indexed(tmp_path):
    (tmp_path / 'model.py').write_text('class Payload:\n    pass\nclass AuditMarker:\n    pass\n')
    (tmp_path / 'subject.py').write_text('from model import Payload, AuditMarker\ndef decode():\n    AuditMarker()\n    return Payload(Payload())\n')
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=50)
    return tmp_path, index


@pytest.mark.parametrize('api', ['sqlite', 'file_calls', 'callers', 'callees', 'snapshot', 'edge_dict', 'symbol_context', 'process_context'])
def test_role_api_surfaces(indexed, api):
    root, index = indexed
    expected = ['discarded', 'returned', 'argument']
    if api == 'sqlite':
        with sqlite3.connect(index.database_path) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 4
            rows = db.execute('SELECT usage_role FROM calls ORDER BY id').fetchall()
        actual = [r[0] for r in rows]
    elif api == 'file_calls':
        actual = [x.usage_role for x in index.file_calls('subject.py')]
    elif api == 'callers':
        actual = [x.usage_role for x in index.callers('Payload')]
        expected = ['returned', 'argument']
    elif api == 'callees':
        actual = [x.usage_role for x in index.callees('decode')]
    elif api == 'snapshot':
        actual = [x.usage_role for x in index.snapshot().calls]
    elif api == 'edge_dict':
        actual = [index._edge_dict(x)['usage_role'] for x in index.callees('decode')]
    elif api == 'symbol_context':
        actual = [x['usage_role'] for x in index.symbol_context('decode')['callees']]
    else:
        context = index.process_context('decode', max_depth=1, time_budget_ms=1000)
        actual = [x['usage_role'] for x in context['edges']]
        assert [s['usage_role'] for s in context['steps'] if s['kind'] == 'call'] == expected
    assert actual == expected
    record('generic-edges.json', [dataclasses.asdict(x) for x in index.file_calls('subject.py')])


def test_positional_compatibility():
    assert RawCallRecord('id', 'f', 'x', '', 'a.py', 1, .9, 'test').usage_role == 'unknown'
    assert CallEdge('f', 'x', 'a.py', 1).usage_role == 'unknown'


def test_v3_cache_rebuild_and_warm_reuse(indexed):
    root, index = indexed
    with sqlite3.connect(index.database_path) as db:
        # On baseline this is already the real v3 schema; on v4 remove the new field.
        columns = [r[1] for r in db.execute('PRAGMA table_info(calls)')]
        if 'usage_role' in columns:
            db.execute('ALTER TABLE calls DROP COLUMN usage_role')
        db.execute('PRAGMA user_version=3')
        assert db.execute('SELECT count(*) FROM files').fetchone()[0] == 2
    rebuilt = PersistentCodeIndex(root)
    with sqlite3.connect(rebuilt.database_path) as db:
        assert db.execute('SELECT count(*) FROM files').fetchone()[0] == 0
    _, cold = rebuilt.scan(root, max_files=50)
    _, warm = rebuilt.scan(root, max_files=50)
    assert cold.indexed == 2 and warm.reused == 2 and warm.indexed == 0
    assert rebuilt.callees('decode')[1].usage_role == 'returned'
    record('warm-reuse.json', {'cold': dataclasses.asdict(cold), 'warm': dataclasses.asdict(warm)})


def test_incremental_service_cache(indexed):
    root, _ = indexed
    service = RepoIntelligenceService(root)
    try:
        service.prewarm().result(timeout=15)
        before = service.symbol_context('decode')
        before_generation = service.state.generation
        assert service.symbol_context('decode')['cache_hit']
        (root / 'subject.py').write_text('from model import Payload, AuditMarker\ndef decode():\n    return AuditMarker()\n')
        service._apply_incremental(('subject.py',), 20)
        after = service.symbol_context('decode')
        assert service.state.generation > before_generation and not after['cache_hit']
        assert before['callees'][0]['usage_role'] == 'discarded'
        assert after['callees'][0]['usage_role'] == 'returned'
        assert service.process_context('decode')['edges'][0]['usage_role'] == 'returned'
        record('incremental-role-change.json', {'before': before, 'after': after, 'before_generation': before_generation, 'after_generation': service.state.generation})
    finally:
        service.close()


@pytest.mark.parametrize('expression,expected', [('return missing()', 'returned'), ('return [missing()]', 'unknown')])
def test_lsp_preserves_role(tmp_path, expression, expected):
    (tmp_path / 'caller.py').write_text('def entry():\n    ' + expression + '\n')
    (tmp_path / 'target.py').write_text('def resolved():\n    pass\n')
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=50)
    before = index.callees('entry')[0]
    assert before.callee_symbol_id is None and before.usage_role == expected
    resolver = SimpleNamespace(resolve=lambda request: ResolvedCallLocation('target.py', 1, name='resolved'))
    stats = index.augment_call_targets(resolver, time_budget_ms=1000)
    after = index.callees('entry')[0]
    assert stats.resolved == 1 and after.callee_symbol_id
    assert after.usage_role == expected and after.source == 'lsp-definition'
    record(f'lsp-{expected}.json', {'before': dataclasses.asdict(before), 'after': dataclasses.asdict(after)})


def test_other_analyzers_unknown(tmp_path):
    source = 'function entry() { return missing(); }'
    result = LexicalFallbackAnalyzer().analyze_file(path=tmp_path / 'a.js', relative='a.js', source=source, language='javascript')
    assert result.calls and all(c.usage_role == 'unknown' for c in result.calls)
    (tmp_path / 'a.js').write_text(source)
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=50)
    assert index.snapshot().calls
    assert all(c.usage_role == 'unknown' for c in index.snapshot().calls)


def test_role_does_not_resolve_type_or_execute_source(tmp_path):
    (tmp_path / 'factory.py').write_text('def build():\n    return None\n')
    (tmp_path / 'subject.py').write_text('import factory\nraise RuntimeError("MUST NOT EXECUTE")\ndef run():\n    mystery()\n    return factory.build()\n')
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=50)
    unknown, method = index.callees('run')
    assert unknown.usage_role == 'discarded' and unknown.callee_symbol_id is None
    assert method.usage_role == 'returned'
    assert index.symbol_context(method.callee_symbol_id)['definition']['kind'] == 'function'


def test_frozen_c_production_roles(tmp_path):
    history = ROOT / 'docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder'
    cf = ROOT / 'docs/evidence/c-sidecar-fixed-diff-counterfactual-2026-09-22'
    workspace = tmp_path / 'workspace'
    shutil.copytree(history / 'final-files', workspace, ignore=shutil.ignore_patterns('.nz-coder', '__pycache__'))
    paths = json.loads((cf / 'packets/historical-verifier2-summary.json').read_text())['runtime_state']['changed_files']
    service = RepoIntelligenceService(workspace)
    try:
        service.prewarm().result(timeout=15)
        scope = service.changed_scope(changed_paths=paths, limit=100, wait_budget_ms=0)
        edges = [e for identity in scope['changed_symbol_ids'] for e in service.symbol_context(identity, limit=100)['callees']]
        # Only assertion names C; production query uses historical changed identities.
        config = next(e for e in edges if e['callee'] == 'Config')
        assert config['usage_role'] == 'returned'
        assert config['resolution_kind'] == 'imported-binding'
        assert service.symbol_context(config['callee_symbol_id'])['definition']['kind'] == 'class'
        record('frozen-c-config-edge.json', config)
    finally:
        service.close()


def test_nested_scope_roles_remain_owned():
    calls = analyze('def outer():\n    def inner():\n        return Payload()\n    sink = lambda: Payload()\n    return inner()\n').calls
    assert [(c.caller_name, c.raw_name, c.usage_role) for c in calls] == [
        ('outer', 'inner', 'returned'), ('inner', 'Payload', 'returned')]


def test_nonpython_registry_defaults(tmp_path):
    from nz_coder.intelligence.analyzers import AnalyzerRegistry
    examples = {
        'typescript': ('a.ts', 'function entry() { return missing(); }'),
        'go': ('a.go', 'package main\nfunc entry() { missing() }\n'),
    }
    for language, (filename, source) in examples.items():
        analyzer = AnalyzerRegistry().analyzer_for(language)
        result = analyzer.analyze_file(path=tmp_path / filename, relative=filename,
                                      source=source, language=language)
        assert result.calls and all(c.usage_role == 'unknown' for c in result.calls)


def test_resolve_later_does_not_change_role(tmp_path):
    (tmp_path / 'subject.py').write_text('from target import create\ndef decode():\n    return create()\n')
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=10)
    assert index.callees('decode')[0].callee_symbol_id is None
    assert index.callees('decode')[0].usage_role == 'returned'
    (tmp_path / 'target.py').write_text('class create:\n    pass\n')
    index.update_paths(['target.py'])
    after = index.callees('decode')[0]
    assert after.callee_symbol_id and after.usage_role == 'returned'
