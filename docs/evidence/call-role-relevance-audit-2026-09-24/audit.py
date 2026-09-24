"""Observe production Python AST nodes; proposed roles never enter production DB.

The parse wrapper observes the very tree parsed by PythonAstAnalyzer, not a
second parse. RawCallRecord instrumentation observes the exact `call` node at
its production emission site. Parent metadata is ephemeral evaluator state.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from nz_coder.intelligence import analyzers
from nz_coder.intelligence.code_index import SCHEMA_VERSION
from nz_coder.intelligence.service import RepoIntelligenceService

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
HIST = ROOT / 'docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder'
CF = ROOT / 'docs/evidence/c-sidecar-fixed-diff-counterfactual-2026-09-22'


def save(name, value):
    target = OUT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def role(call, parents):
    """Immediate syntactic use only: never propagate through names or wrappers."""
    parent = parents.get(call)
    if isinstance(parent, ast.Return) and parent.value is call:
        return 'returned'
    if isinstance(parent, ast.Yield) and parent.value is call:
        return 'yielded'
    if isinstance(parent, ast.Expr) and parent.value is call:
        return 'discarded'
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and parent.value is call:
        return 'assigned'
    if isinstance(parent, ast.Call) and call in parent.args:
        return 'argument'
    if isinstance(parent, ast.keyword) and parent.value is call:
        return 'argument'
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and parent.test is call:
        return 'condition'
    # BoolOp children are not necessarily predicates: return A() or B() carries
    # values. Conservative v1 leaves such wrapped expressions unknown.
    return 'unknown'


def observe(path, relative):
    original_parse = ast.parse
    original_record = analyzers.RawCallRecord
    parents, observed = {}, []
    parses = 0

    def watch_parse(*args, **kwargs):
        nonlocal parses
        tree = original_parse(*args, **kwargs)
        parses += 1
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        return tree

    def watch_record(*args, **kwargs):
        frame = inspect.currentframe().f_back
        assert frame.f_code.co_name == 'analyze_file'
        call = frame.f_locals['call']
        assert isinstance(call, ast.Call)
        record = original_record(*args, **kwargs)
        observed.append({'raw': dataclasses.asdict(record),
                         'proposed_usage_role': role(call, parents),
                         'column': call.col_offset,
                         'direct_parent': type(parents[call]).__name__})
        return record

    with patch.object(ast, 'parse', watch_parse), patch.object(analyzers, 'RawCallRecord', watch_record):
        result = analyzers.PythonAstAnalyzer().analyze_file(
            path=path, relative=relative, source=path.read_text(), language='python')
    assert parses == 1
    assert len(observed) == len(result.calls)
    return observed


def replay(workspace, changed):
    # Observation pass uses the production parser once per file. The real index
    # independently follows its normal pipeline; no metadata is injected into it.
    observations = {p: observe(workspace / p, p) for p in sorted(changed) if p.endswith('.py')}
    service = RepoIntelligenceService(workspace)
    try:
        service.prewarm().result(timeout=15)
        assert service.state.status == 'ready'
        scope = service.changed_scope(changed_paths=changed, limit=100, max_depth=1,
                                      wait_budget_ms=0)
        output = []
        for path, nodes in observations.items():
            with service.index._connect() as db:
                rows = list(db.execute('SELECT * FROM calls WHERE path=? ORDER BY id', (path,)))
                columns = [r['name'] for r in db.execute('PRAGMA table_info(calls)')]
                version = db.execute('PRAGMA user_version').fetchone()[0]
            assert len(rows) == len(nodes)
            # SQL insertion order preserves concrete occurrences, even repeated
            # identical names on one line; not a name/line role classifier.
            for row, node in zip(rows, nodes, strict=True):
                for key in ('caller_symbol_id', 'raw_name', 'qualifier', 'line'):
                    assert row[key] == node['raw'][key]
                target_id = row['callee_symbol_id']
                target = service.symbol_context(target_id, wait_budget_ms=0)['definition'] if target_id else None
                caller = service.symbol_context(row['caller_symbol_id'], limit=100, wait_budget_ms=0)
                assert 'usage_role' not in caller['callees'][0]
                eligible = bool(target and target['kind'] == 'class'
                    and target['path'] not in changed and not target['path'].startswith('tests/')
                    and row['confidence'] >= .85 and node['proposed_usage_role'] == 'returned')
                output.append({**node, 'persisted_edge': dict(row), 'target': target,
                               'candidate_eligible': eligible, 'caller_context': caller})
        snapshot = service.index.snapshot()
        assert all(not hasattr(edge, 'usage_role') for edge in snapshot.calls)
        return {'calls': output, 'scope': scope, 'sqlite_columns': columns,
                'schema_version': version, 'snapshot_has_role': False}
    finally:
        service.close()


CASES = {
    'returned': ('def decode(value):\n    return Payload(value)\n', [('Payload', 'returned')]),
    'discarded': ('def decode(value):\n    AuditMarker()\n    return Payload(value)\n', [('AuditMarker', 'discarded'), ('Payload', 'returned')]),
    'nested': ('def decode(value):\n    return Payload(AuditMarker())\n', [('Payload', 'returned'), ('AuditMarker', 'argument')]),
    'assigned': ('def decode(value):\n    payload = Payload(value)\n    return payload\n', [('Payload', 'assigned')]),
    'assigned-unused': ('def decode(value):\n    marker = AuditMarker()\n    return Payload(value)\n', [('AuditMarker', 'assigned'), ('Payload', 'returned')]),
    'yielded': ('def stream(value):\n    yield Payload(value)\n', [('Payload', 'yielded')]),
    'condition': ('def decode(value):\n    if Validator():\n        return Payload(value)\n', [('Validator', 'condition'), ('Payload', 'returned')]),
    'argument': ('def decode(value):\n    return wrap(Payload(value))\n', [('wrap', 'returned'), ('Payload', 'argument')]),
    'multiple-returned': ('def decode(flag):\n    if flag:\n        return PayloadA()\n    return PayloadB()\n', [('PayloadA', 'returned'), ('PayloadB', 'returned')]),
    'unresolved': ('def decode(factory):\n    return factory()\n', [('factory', 'returned')]),
    'method-return': ('def decode(value):\n    return factory.build()\n', [('build', 'returned')]),
    'returned-telemetry': ('def changed():\n    return TelemetryMarker()\n', [('TelemetryMarker', 'returned')]),
    'same-name-nested': ('def decode():\n    return Payload(Payload())\n', [('Payload', 'returned'), ('Payload', 'argument')]),
    'keyword': ('def decode():\n    return wrap(value=Payload())\n', [('wrap', 'returned'), ('Payload', 'argument')]),
    'wrapped-unknown': ('def decode():\n    return Payload() or PayloadB()\n', [('Payload', 'unknown'), ('PayloadB', 'unknown')]),
    'annassign': ('def decode():\n    x: object = Payload()\n    return x\n', [('Payload', 'assigned')]),
    'namedexpr': ('def decode():\n    return (x := Payload())\n', [('Payload', 'assigned')]),
    'yield-from': ('def decode():\n    yield from Payload()\n', [('Payload', 'unknown')]),
}


def main():
    count = 0
    for name, (source, expected) in CASES.items():
        with tempfile.TemporaryDirectory(prefix='role-fixture-') as temp:
            root = Path(temp)
            (root / 'model.py').write_text('\n'.join(f'class {n}:\n    pass\n' for n in
                ('Payload', 'AuditMarker', 'Validator', 'PayloadA', 'PayloadB', 'TelemetryMarker')))
            (root / 'factory.py').write_text('def build():\n    return None\n')
            (root / 'subject.py').write_text('from model import Payload, AuditMarker, Validator, PayloadA, PayloadB, TelemetryMarker\nimport factory\ndef wrap(*args, **kwargs):\n    return None\n' + source)
            result = replay(root, ['subject.py'])
            actual = [(c['raw']['raw_name'], c['proposed_usage_role']) for c in result['calls']]
            assert actual == expected, (name, actual)
            if name in ('unresolved', 'method-return', 'assigned', 'assigned-unused'):
                assert not result['calls'][0]['candidate_eligible']
            if name == 'discarded':
                assert [c['candidate_eligible'] for c in result['calls']] == [False, True]
            if name == 'nested':
                assert [c['candidate_eligible'] for c in result['calls']] == [True, False]
            if name == 'returned-telemetry':
                assert result['calls'][0]['candidate_eligible']  # observed false-positive boundary
            save(f'fixtures/{name}.json', {'source': (root / 'subject.py').read_text(), 'expected': expected, **result})
            count += 1
    historical = json.loads((CF / 'packets/historical-verifier2-summary.json').read_text())
    paths = historical['runtime_state']['changed_files']
    with tempfile.TemporaryDirectory(prefix='role-c-') as temp:
        root = Path(temp) / 'workspace'
        shutil.copytree(HIST / 'final-files', root, ignore=shutil.ignore_patterns('.nz-coder', '__pycache__'))
        result = replay(root, paths)
        # Selection is name-independent; C-specific assertions are evaluator checks.
        selected = [c for c in result['calls'] if c['candidate_eligible']]
        config = [c for c in selected if c['target']['name'] == 'Config']
        assert len(config) == 1 and config[0]['proposed_usage_role'] == 'returned'
        save('c/proposed-role.json', {'changed_paths': paths, 'selected': selected, 'roles_persisted': False})
        save('c/config-edge.json', config[0])
        save('c/loads-context.json', config[0]['caller_context'])
        save('baseline/production-call-schema.json', {'version': SCHEMA_VERSION,
             'columns': result['sqlite_columns'], 'RawCallRecord': [f.name for f in dataclasses.fields(analyzers.RawCallRecord)],
             'snapshot_has_role': result['snapshot_has_role']})
    save('analysis/test-results.json', {'generic_cases': count, 'c_replay': 'pass', 'production_metadata_modified': False,
         'classification': 'CALL-ROLE: MIXED', 'metadata_feasibility': 'STRUCTURAL-METADATA-SUFFICIENT'})
    print(f'PASS: {count} production analyzer/index fixtures + frozen C replay; role is proposed, not persisted')


if __name__ == '__main__':
    main()
