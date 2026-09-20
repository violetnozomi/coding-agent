"""Build paired fixtures and freeze independent checks. Never calls a model."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
NODE = '/home/pyh/.local/lib/python3.13/site-packages/playwright/driver/node'
ISOLATE = ROOT / 'tests/evaluation/fixtures/offline_exec.py'


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip('\n'))


TASKS = {
    'M': {
        'title': 'Money API migration across five consumers',
        'prompt': '''Implement the invoice pricing migration described in REQUIREMENTS.md. Replace float-based totals with an immutable Quote returned by billing.quote.quote_order; migrate every in-repository consumer and its tests to the new API, and document it. Preserve the documented consumer output formats while adding currency correctness, quantity/price validation and discounts. Run python -m pytest -q tests and report the actual result and remaining limitations.''',
        'requirements': '''# Invoice pricing migration
Add a frozen dataclass Quote in billing/quote.py with currency, subtotal_minor, discount_minor, total_minor. quote_order(lines, *, currency="USD", discount_bps=0) accepts an iterable of dictionaries with unit_price (decimal string) and quantity (positive integer; bool is invalid). Ignore unrelated line keys; do not mutate caller input. Empty orders are valid.
Supported currencies: USD and EUR (2 decimal places), JPY (0). Validate currency and discount even for empty orders. Reject unsupported currencies, invalid/non-finite/negative prices, invalid quantities and discount_bps outside integer 0..10000 with ValueError (bool is invalid for discount). Price string parsing may accept normal decimal notation; no float input.
For each line, multiply exact decimal unit price by quantity then round HALF_UP to currency minor units; sum rounded lines. Round the total-order discount HALF_UP to minor units. total_minor=subtotal_minor-discount_minor. Do not round each unit price before multiplying.
Migrate web.checkout, cli.receipt, jobs.export, mobile.summary and reporting.monthly to call quote_order, not the old total_amount API. Keep the old billing.legacy.total_amount callable for external users, but no internal consumer may call it. Existing tests should remain meaningful; add coverage for the new contract.
All five consumers accept the same lines and keyword-only currency/discount_bps defaults. web.checkout returns {"currency": currency, "total_minor": integer}; CLI returns e.g. "USD 12.30" (JPY no decimals); export returns "currency,total_minor\\nUSD,1230\\n"; mobile returns the formatted numeric amount without currency; monthly returns integer minor units. Outputs must come from the central Quote, not duplicate pricing arithmetic.
Document rounding, validation, the legacy compatibility boundary and usage in README.md. Do not install dependencies or use network. Verification: python -m pytest -q tests.
''',
        'files': {
            'billing/__init__.py': '',
            'billing/legacy.py': '''
                def total_amount(lines):
                    return sum(float(line["unit_price"]) * line["quantity"] for line in lines)
                ''',
            'web.py': '''
                from billing.legacy import total_amount
                def checkout(lines):
                    return {"total": total_amount(lines)}
                ''',
            'cli.py': '''
                from billing.legacy import total_amount
                def receipt(lines):
                    return f"USD {total_amount(lines):.2f}"
                ''',
            'jobs.py': '''
                from billing.legacy import total_amount
                def export(lines):
                    return f"total\\n{total_amount(lines):.2f}\\n"
                ''',
            'mobile.py': '''
                from billing.legacy import total_amount
                def summary(lines):
                    return f"{total_amount(lines):.2f}"
                ''',
            'reporting.py': '''
                from billing.legacy import total_amount
                def monthly(lines):
                    return total_amount(lines)
                ''',
            'tests/test_consumers.py': '''
                from web import checkout
                from cli import receipt
                from jobs import export
                from mobile import summary
                from reporting import monthly
                def test_legacy_consumers():
                    lines = [{"unit_price": "1.25", "quantity": 2}]
                    assert checkout(lines) == {"total": 2.5}
                    assert receipt(lines) == "USD 2.50"
                    assert export(lines) == "total\\n2.50\\n"
                    assert summary(lines) == "2.50"
                    assert monthly(lines) == 2.5
                ''',
            'README.md': '# Invoice desk\nPricing is currently centralized in billing.legacy.total_amount.\n',
        },
    },
    'Q': {
        'title': 'Bounded async worker pool, failure drain and cancellation',
        'prompt': '''Repair lib/pool.cjs and its batch consumer according to REQUIREMENTS.md. Implement bounded concurrency, stable result ordering, failure draining and AbortSignal cancellation without breaking the public API. Add meaningful tests and update README.md. Run node --test tests/pool.test.cjs tests/batch.test.cjs and report the actual result and remaining limitations.''',
        'requirements': '''# Worker pool contract
mapLimit(items, limit, worker, {signal}={}) is exported from lib/pool.cjs and returns a Promise of results in input order. items is an array, limit a positive integer, worker callable; reject invalid arguments with TypeError before invoking worker (validate even for empty input). Do not mutate items. worker receives (item, index, signal), may return a value or Promise, and may throw synchronously. Empty input resolves [] when not aborted.
Never have more than limit workers in flight. Fill available slots without waiting for the whole batch; start each item exactly once. Once a worker error is observed, stop starting new work; wait for all already-started workers to settle, then reject with the first observed worker error, preserving object identity. Attach rejection handlers so secondary failures do not produce unhandled rejections.
If signal is already aborted, start no work and reject with signal.reason (preserve identity, including non-Error reasons). If abort arrives mid-run, stop scheduling and wait for in-flight work to settle, then reject with that reason. The first observed failure/abort wins. Do not pretend an uncooperative worker has stopped; do not use arbitrary delays or polling as correctness machinery. Remove installed abort listeners after settlement. Do not overwrite worker results that are falsy.
Update lib/batch.cjs: processBatch(records, send, options={}) calls mapLimit with options.concurrency default 2 and options.signal, returns ordered {id, value} records, sends each record at most once, and propagates rejection. Keep index.cjs exports compatible. Add tests and document concurrency, cancellation and drain semantics in README.md. Use Node built-ins only; no dependency install/network. Verification: node --test tests/pool.test.cjs tests/batch.test.cjs.
''',
        'files': {
            'lib/pool.cjs': '''
                async function mapLimit(items, limit, worker, options = {}) {
                  const results = [];
                  for (let i = 0; i < items.length; i++) results.push(await worker(items[i], i));
                  return results;
                }
                module.exports = {mapLimit};
                ''',
            'lib/batch.cjs': '''
                const {mapLimit} = require('./pool.cjs');
                async function processBatch(records, send, options = {}) {
                  return mapLimit(records, 2, async record => ({id: record.id, value: await send(record)}));
                }
                module.exports = {processBatch};
                ''',
            'index.cjs': "module.exports = {...require('./lib/pool.cjs'), ...require('./lib/batch.cjs')};\n",
            'package.json': '{"name":"delivery-pool","private":true,"scripts":{"test":"node --test tests/pool.test.cjs tests/batch.test.cjs"}}\n',
            'tests/pool.test.cjs': '''
                const test = require('node:test');
                const assert = require('node:assert/strict');
                const {mapLimit} = require('../index.cjs');
                test('maps values and preserves falsy results', async () => {
                  assert.deepEqual(await mapLimit([1, 2], 2, x => x - 1), [0, 1]);
                });
                test('empty input', async () => assert.deepEqual(await mapLimit([], 2, x => x), []));
                ''',
            'tests/batch.test.cjs': '''
                const test = require('node:test');
                const assert = require('node:assert/strict');
                const {processBatch} = require('../index.cjs');
                test('batch maps ids', async () => {
                  assert.deepEqual(await processBatch([{id:'a'}, {id:'b'}], async x => x.id),
                    [{id:'a',value:'a'}, {id:'b',value:'b'}]);
                });
                ''',
            'README.md': '# Delivery pool\nExports mapLimit and processBatch. Current implementation is sequential.\n',
        },
    },
    'S': {
        'title': 'Versioned JSON store with migration and atomic replacement',
        'prompt': '''Upgrade the settings store and both callers using REQUIREMENTS.md. Support legacy-file migration, strict validation, revision conflicts and atomic saves without corrupting existing data. Add failure-path tests and document the format and concurrency boundary. Run python -m pytest -q tests and report the actual result and remaining limitations.''',
        'requirements': '''# Settings store v2
Keep store.load(path) returning an independent data dictionary; missing files return {}. Add store.load_snapshot(path) returning a frozen Snapshot with revision (int) and data (independent dict), and store.save(path, data, *, expected_revision=None) returning the new revision. Add StoreError, CorruptStoreError(StoreError), ConflictError(StoreError). Data is a dictionary with string keys and JSON scalar values (str/int/finite float/bool/None); reject nested containers/non-string keys/non-finite values with ValueError. No caller mutation.
Legacy v1 files are flat data dictionaries, revision=0. V2 has exactly {"version":2,"revision":positive_int,"data":data_dict}. A dictionary containing all three reserved keys is treated as an envelope; unsupported version, non-positive/non-integer revision (bool invalid), invalid envelope data, extra envelope fields, malformed JSON, duplicate keys at any level, or non-object top-level must raise CorruptStoreError without rewriting. Other flat dictionaries (even one with only key "version") remain legacy data.
Missing file revision=0. save validates data and expected_revision first; expected_revision must be None or a nonnegative integer (not bool). It reads/validates the current file; if expected_revision is given and mismatches, raise ConflictError without touching bytes. A successful save upgrades legacy content to v2 and increments revision. Existing corrupt files must never be silently replaced.
Write UTF-8 JSON to a unique temporary file in the destination directory, flush and fsync that file, then os.replace it over the destination. On any failure before replace succeeds, preserve old bytes (or keep destination absent) and clean up the temp file. Propagate the original I/O exception. If replacing an existing file, preserve its permission bits. No persistent sidecar files after settlement. Concurrent writers are NOT promised serializability: document that expected_revision detects sequential stale writes but is not a cross-process lock.
Keep cli.update(path,key,value) returning the new data dict and add expected_revision forwarding. Add sync.merge(path,updates,expected_revision=None), which merges keys, returns the new revision, and preserves unrelated entries. Both use the central store API. Document v1/v2, errors, rollback and concurrency limitations in README.md. Add tests including failure injection. No dependencies/network. Verification: python -m pytest -q tests.
''',
        'files': {
            'store.py': '''
                import json
                from pathlib import Path
                def load(path):
                    path = Path(path)
                    return json.loads(path.read_text()) if path.exists() else {}
                def save(path, data):
                    Path(path).write_text(json.dumps(data))
                ''',
            'cli.py': '''
                import store
                def update(path, key, value):
                    data = store.load(path)
                    data[key] = value
                    store.save(path, data)
                    return data
                ''',
            'sync.py': '''
                import store
                def merge(path, updates):
                    data = store.load(path)
                    data.update(updates)
                    store.save(path, data)
                ''',
            'tests/test_store.py': '''
                import store
                from cli import update
                def test_missing(tmp_path):
                    assert store.load(tmp_path / "settings.json") == {}
                def test_roundtrip(tmp_path):
                    path = tmp_path / "settings.json"
                    store.save(path, {"name": "雪", "active": True})
                    assert store.load(path) == {"name": "雪", "active": True}
                def test_cli_preserves_keys(tmp_path):
                    path = tmp_path / "settings.json"
                    store.save(path, {"a": 1})
                    assert update(path, "b", 2) == {"a": 1, "b": 2}
                ''',
            'README.md': '# Settings store\nFlat JSON settings with CLI and sync consumers.\n',
        },
    },
}


def hashes(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob('*')) if p.is_file()}


def main():
    results = []
    for key, task in TASKS.items():
        initial = OUT / 'initial' / key
        initial.mkdir(parents=True, exist_ok=False)
        for name, content in task['files'].items():
            put(initial, name, content)
        put(initial, 'REQUIREMENTS.md', task['requirements'])
        cmd = ([NODE, '--test', 'tests/pool.test.cjs', 'tests/batch.test.cjs'] if key == 'Q'
               else [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests'])
        acceptance_cmd = ([NODE, '--test', str(OUT / 'acceptance/Q.cjs')] if key == 'Q'
                          else [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(OUT / f'acceptance/{key}.py')])
        with tempfile.TemporaryDirectory(prefix='complex-pair-check-') as tmp:
            ws = Path(tmp) / 'workspace'
            shutil.copytree(initial, ws)
            env = {'PATH': f'{Path(NODE).parent}:{Path(sys.executable).parent}:/usr/bin:/bin',
                   'PYTHONPATH': str(ws), 'TASK_WORKSPACE': str(ws), 'LANG': 'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
            checks = {}
            for name, argv in [('baseline', cmd), ('acceptance_on_initial', acceptance_cmd)]:
                proc = subprocess.run([sys.executable, str(ISOLATE), *argv], cwd=ws, env=env,
                                      text=True, capture_output=True, timeout=30)
                checks[name] = {'argv': argv, 'exit_code': proc.returncode,
                                'stdout': proc.stdout, 'stderr': proc.stderr}
            assert checks['baseline']['exit_code'] == 0, checks
            assert checks['acceptance_on_initial']['exit_code'] == 1, checks
            assert hashes(ws) == hashes(initial), 'Initial checks mutated fixture'
        results.append({'id': key, 'title': task['title'], 'prompt': task['prompt'],
                        'initial_hashes': hashes(initial), **checks})
    manifest = {'status': 'prepared_not_run_pending_new_batch_paid_authorization',
                'nz_commit': '0885c874b949792723c515ff098c24d31cd238c9',
                'infcodex_commit': 'd3a812379b589597347f5be12d5b68477e577f02',
                'model': 'deepseek-v4-flash', 'repetitions_per_task_side': 1,
                'planned_runs': 6, 'main_request_cap_per_run': 24,
                'auxiliary_request_cap_per_run': 8, 'main_request_cap_total': 144,
                'auxiliary_request_cap_total': 48, 'all_paid_request_cap_total': 192,
                'retry_accounting': 'each physical request consumes its purpose cap',
                'automatic_reruns': False, 'reasoning_effort': 'main field omitted',
                'output_limits': {'nzcoder': 64000, 'infcodex': 32768},
                'cost': 'cost unknown; request caps are not a monetary cap',
                'real_requests': 0, 'tasks': results}
    (OUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({x['id']: {'baseline': x['baseline']['exit_code'],
                              'acceptance_on_initial': x['acceptance_on_initial']['exit_code']}
                      for x in results}, indent=2))


if __name__ == '__main__':
    main()
