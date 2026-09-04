"""Machine-verifiable inventory of NZ-Coder security boundary entry points."""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = tuple(
    ROOT / "nz_coder" / name
    for name in ("runtime", "tools", "capabilities", "providers", "mcp", "lsp")
)
CLASSIFICATIONS = {
    "run-scoped", "host-process-only", "static product default",
    "test/compatibility-only",
}


# Exact module totals and key sets make line motion harmless while still
# rejecting a new read in an already-inventoried module.
CONFIG_READS = {
    'nz_coder/mcp/cli.py': (3, {'MCP_SERVERS_JSON', 'MCP_TRUST_STORE'}),
    'nz_coder/mcp/config.py': (7, {'MCP_PROJECT_CONFIG', 'MCP_SERVERS_JSON', 'MCP_TRUST_STORE', 'MCP_USER_CONFIG'}),
    'nz_coder/providers/__init__.py': (2, {'ANTHROPIC_API_VERSION', 'MODEL_PROVIDER'}),
    'nz_coder/providers/capabilities.py': (4, {'MAX_CONTEXT_TOKENS', 'MAX_OUTPUT_TOKENS', 'MODEL_ID', 'MODEL_PROVIDER'}),
    'nz_coder/providers/models.py': (5, {'ANTHROPIC_API_VERSION', 'MODEL_CATALOG_PATH', 'MODEL_ID', 'MODEL_PROVIDER', 'MODEL_VARIANT'}),
    'nz_coder/runtime/agent/subagent.py': (4, {'MODEL_ID'}),
    'nz_coder/runtime/core/execution_context.py': (2, {'MAX_AGENT_TURNS', 'MAX_PARALLEL_TASKS'}),
    'nz_coder/runtime/execution/loop.py': (3, {'MODEL_ID', 'PLANNING_TASK_MODES'}),
    'nz_coder/tools/bash.py': (4, {'BASH_TIMEOUT_SECONDS', 'CONTEXT_TRUNCATE_CHARS'}),
    'nz_coder/tools/files.py': (2, {'CONTEXT_TRUNCATE_CHARS'}),
    'nz_coder/tools/process.py': (1, {'PROCESS_READ_MAX_BYTES'}),
}

# Every surviving direct read is an explicitly reviewed compatibility, host, or
# schema/default read.  A formal run-scoped consumer must use RunSettings.
DIRECT_CONFIG_ALLOWLIST = {
    ('nz_coder/mcp/cli.py', 'MCP_SERVERS_JSON'): 'host-process-only',
    ('nz_coder/mcp/cli.py', 'MCP_TRUST_STORE'): 'host-process-only',
    **{
        ('nz_coder/mcp/config.py', key): 'test/compatibility-only'
        for key in {'MCP_PROJECT_CONFIG', 'MCP_SERVERS_JSON', 'MCP_TRUST_STORE', 'MCP_USER_CONFIG'}
    },
    **{
        ('nz_coder/providers/__init__.py', key): 'test/compatibility-only'
        for key in {'ANTHROPIC_API_VERSION', 'MODEL_PROVIDER'}
    },
    **{
        ('nz_coder/providers/capabilities.py', key): 'test/compatibility-only'
        for key in {'MAX_CONTEXT_TOKENS', 'MAX_OUTPUT_TOKENS', 'MODEL_ID', 'MODEL_PROVIDER'}
    },
    **{
        ('nz_coder/providers/models.py', key): 'test/compatibility-only'
        for key in {'ANTHROPIC_API_VERSION', 'MODEL_CATALOG_PATH', 'MODEL_ID', 'MODEL_PROVIDER', 'MODEL_VARIANT'}
    },
    ('nz_coder/runtime/agent/subagent.py', 'MODEL_ID'): 'test/compatibility-only',
    ('nz_coder/runtime/core/execution_context.py', 'MAX_AGENT_TURNS'): 'test/compatibility-only',
    ('nz_coder/runtime/core/execution_context.py', 'MAX_PARALLEL_TASKS'): 'test/compatibility-only',
    ('nz_coder/runtime/execution/loop.py', 'MODEL_ID'): 'test/compatibility-only',
    ('nz_coder/runtime/execution/loop.py', 'PLANNING_TASK_MODES'): 'static product default',
    ('nz_coder/tools/bash.py', 'BASH_TIMEOUT_SECONDS'): 'static product default',
    ('nz_coder/tools/bash.py', 'CONTEXT_TRUNCATE_CHARS'): 'static product default',
    ('nz_coder/tools/files.py', 'CONTEXT_TRUNCATE_CHARS'): 'static product default',
    ('nz_coder/tools/process.py', 'PROCESS_READ_MAX_BYTES'): 'static product default',
}


WORKSPACE_INPUTS = {
    '.env': 'Project Authority',
    'AGENTS.md': 'Project Authority',
    'CLAUDE.md': 'Project Authority',
    '.nz-coder/settings.json': 'Project Authority',
    '.nz-coder/mcp.json': 'Project Authority',
    '.nz-coder/skills/**': 'Project Authority',
    '.nz-coder/commands/**': 'Project Authority',
    '.nz-coder/workflows/**': 'Project Authority',
    '.nz-coder/rules/**': 'Project Authority',
    '.nz-coder/instruction-file-state.json': 'Project Authority',
    '.nz-coder/models/selection.json': 'Untrusted repository data',
    '.nz-coder/models/registry.json': 'Untrusted repository data',
    '.nz-coder/memory/**': 'Untrusted repository data',
}


@dataclass(frozen=True)
class ProcessBoundary:
    count: int
    identity: str
    trust: str
    environment: str
    cwd: str
    owner: str


PROCESS_SITES = {
    ('nz_coder/capabilities/documents.py', 'subprocess.Popen'): ProcessBoundary(2, 'product helper binary', 'product installation', 'scrubbed helper', 'run workspace', 'lexical helper'),
    ('nz_coder/capabilities/ripgrep.py', 'subprocess.Popen'): ProcessBoundary(1, 'resolved ripgrep binary', 'product installation', 'scrubbed helper', 'run workspace', 'lexical helper'),
    ('nz_coder/lsp/client.py', 'subprocess.Popen'): ProcessBoundary(1, 'LSP ExecutionIdentity', 'per-server execution trust', 'scrubbed protocol', 'snapshotted workspace', 'LSPClient'),
    ('nz_coder/mcp/client.py', 'subprocess.Popen'): ProcessBoundary(1, 'MCP ExecutionIdentity', 'per-server execution trust', 'scrubbed protocol', 'snapshotted workspace', 'MCPClient'),
    ('nz_coder/runtime/agent/child_contracts.py', 'subprocess.run'): ProcessBoundary(1, 'resolved git binary', 'host operation', 'scrubbed host', 'explicit repository', 'lexical call'),
    ('nz_coder/runtime/process/process_backends.py', 'subprocess.Popen'): ProcessBoundary(2, 'model command identity', 'permission decision', 'scrubbed model subprocess', 'run workspace', 'ProcessService'),
    ('nz_coder/runtime/process/process_backends.py', 'subprocess.run'): ProcessBoundary(1, 'host taskkill identity', 'platform runtime', 'scrubbed host', 'none', 'lexical call'),
    ('nz_coder/runtime/worktree/manager.py', 'subprocess.run'): ProcessBoundary(1, 'resolved git binary', 'host operation', 'scrubbed host', 'explicit repository', 'lexical call'),
    ('nz_coder/runtime/worktree/setup.py', 'subprocess.run'): ProcessBoundary(2, 'resolved git binary', 'host operation', 'scrubbed host', 'explicit worktree', 'lexical call'),
    ('nz_coder/tools/bash.py', 'subprocess.Popen'): ProcessBoundary(1, 'model shell identity', 'permission decision', 'scrubbed model subprocess', 'run workspace', 'tool invocation'),
    ('nz_coder/tools/repo_intel.py', 'subprocess.run'): ProcessBoundary(3, 'resolved git/ripgrep binary', 'read-only helper', 'scrubbed helper', 'run workspace', 'lexical call'),
}


# Direct filesystem calls are deliberately coarse here: the count is an alarm,
# while P0-E's focused contract rejects specific calls in model tool modules.
MODEL_IO_MODULE_COUNTS = {
    'nz_coder/capabilities/documents.py': ('legacy-model-reachable', 9),
    'nz_coder/capabilities/web_search.py': ('remote-transport', 3),
    'nz_coder/tools/bash.py': ('legacy-model-reachable', 1),
    'nz_coder/tools/files.py': ('legacy-model-reachable', 13),
    'nz_coder/tools/plan_mode.py': ('legacy-model-reachable', 3),
    'nz_coder/tools/python_ast.py': ('legacy-model-reachable', 2),
    'nz_coder/tools/read_support.py': ('legacy-model-reachable', 3),
    'nz_coder/tools/repo_intel.py': ('legacy-model-reachable', 5),
    'nz_coder/tools/search.py': ('legacy-model-reachable', 2),
    'nz_coder/tools/scratchpad.py': ('private-user-state', 1),
    'nz_coder/tools/todo.py': ('private-user-state', 1),
    'nz_coder/tools/webfetch.py': ('remote-transport', 1),
}


PUBLIC_ERROR_MODULE_COUNTS = {
    'nz_coder/capabilities/documents.py': ('public/model-visible', 2),
    'nz_coder/capabilities/vision.py': ('public/model-visible', 1),
    'nz_coder/lsp/client.py': ('trusted local validation', 1),
    'nz_coder/lsp/manager.py': ('public/model-visible', 1),
    'nz_coder/runtime/agent/agent_manager.py': ('private diagnostic', 1),
    'nz_coder/runtime/agent/agent_resilience.py': ('trusted local validation', 1),
    'nz_coder/runtime/agent/subagent.py': ('public/model-visible', 1),
    'nz_coder/runtime/conversation/input_preflight.py': ('public/model-visible', 1),
    'nz_coder/runtime/core/state.py': ('public/model-visible', 1),
    'nz_coder/runtime/execution/loop.py': ('private diagnostic', 20),
    'nz_coder/runtime/execution/provider_stream.py': ('private diagnostic', 1),
    'nz_coder/runtime/execution/run_lifecycle.py': ('private diagnostic', 2),
    'nz_coder/runtime/execution/runner.py': ('private diagnostic', 2),
    'nz_coder/runtime/execution/services.py': ('trusted local validation', 1),
    'nz_coder/runtime/model_gateway/errors.py': ('private diagnostic', 3),
    'nz_coder/runtime/model_gateway/gateway.py': ('private diagnostic', 16),
    'nz_coder/runtime/model_gateway/models.py': ('private diagnostic', 4),
    'nz_coder/runtime/process/process_service.py': ('public/model-visible', 1),
    'nz_coder/runtime/tool_runtime/pipeline.py': ('public/model-visible', 2),
    'nz_coder/runtime/verification/hooks.py': ('private diagnostic', 4),
    'nz_coder/runtime/verification/recovery.py': ('private diagnostic', 3),
    'nz_coder/runtime/verification/stall_sidecar.py': ('private diagnostic', 1),
    'nz_coder/runtime/workflows/workflow_generation.py': ('trusted local validation', 1),
    'nz_coder/runtime/workflows/workflow_runtime.py': ('public/model-visible', 5),
    'nz_coder/runtime/worktree/manager.py': ('private diagnostic', 1),
    'nz_coder/tools/bash.py': ('trusted local validation', 1),
    'nz_coder/tools/question.py': ('private diagnostic', 1),
    'nz_coder/tools/read_support.py': ('trusted local validation', 1),
    'nz_coder/tools/scratchpad.py': ('public/model-visible', 1),
    'nz_coder/tools/todo.py': ('public/model-visible', 1),
    'nz_coder/tools/webfetch.py': ('private diagnostic', 1),
}


CROSS_RUN_RESOURCES = {
    'persistent process': ('ProcessService', 'workspace+control+run', 'process close ledger', 'report/revoke'),
    'background agent': ('BackgroundAgentManager', 'workspace+control+interaction', 'cancel/join ledger', 'report/revoke'),
    'workflow child': ('Workflow runtime', 'workspace+workflow+run', 'workflow close ledger', 'invalidate/revoke'),
    'MCP runtime': ('RunControlBundle', 'workspace+execution identity', 'MCP close stage', 'rotate'),
    'LSP client': ('LSPManager', 'workspace+execution identity', 'shutdown/kill', 'rotate'),
    'Provider runtime': ('RunControlBundle', 'provider+config+run', 'provider close stage', 'rotate'),
    'sidecar': ('RunControlBundle', 'workspace+control+run', 'sidecar close stage', 'rotate'),
    'repo watcher': ('Repo intelligence', 'workspace', 'environment close ledger', 'invalidate'),
    'event bus': ('ProductRunEnvironment', 'session+run', 'environment close ledger', 'do not inherit authority'),
}


def _trees():
    for base in PRODUCTION_ROOTS:
        for path in sorted(base.rglob('*.py')):
            yield path, ast.parse(path.read_text(encoding='utf-8'))


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


def test_all_direct_config_reads_are_classified():
    actual: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for path, tree in _trees():
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == 'config' and node.attr.isupper()):
                actual[relative].append((node.lineno, node.attr))
    problems: list[str] = []
    for path in sorted(set(actual) | set(CONFIG_READS)):
        rows = actual.get(path, [])
        expected = CONFIG_READS.get(path)
        if expected is None:
            problems.extend(f'{path}:{line}: config.{key} is unclassified; use RunSettings or add a reviewed inventory entry' for line, key in rows)
            continue
        expected_count, expected_keys = expected
        keys = {key for _line, key in rows}
        if len(rows) != expected_count or keys != expected_keys:
            problems.append(f'{path}: expected {expected_count} reads/{sorted(expected_keys)}, found {len(rows)} reads/{sorted(keys)}; use RunSettings or update the reviewed inventory')
        for line, key in rows:
            classification = DIRECT_CONFIG_ALLOWLIST.get((path, key))
            if classification is None:
                problems.append(
                    f'{path}:{line}: config.{key} lacks a reviewed classification; '
                    'formal run-scoped consumers must use RunSettings'
                )
            elif classification == 'run-scoped':
                problems.append(
                    f'{path}:{line}: config.{key} remains run-scoped; use RunSettings'
                )
            elif classification not in CLASSIFICATIONS:
                problems.append(f'{path}:{line}: config.{key} has invalid classification')
    inventoried_pairs = {
        (path, key)
        for path, (_count, keys) in CONFIG_READS.items()
        for key in keys
    }
    if inventoried_pairs != set(DIRECT_CONFIG_ALLOWLIST):
        problems.append('direct config classification allowlist does not match inventory')
    assert not problems, '\n'.join(problems)


def test_workspace_behavior_inputs_are_classified():
    allowed = {'Project Authority', 'User-owned state', 'Derived cache', 'Untrusted repository data'}
    assert set(WORKSPACE_INPUTS.values()) <= allowed
    required = {'.env', 'AGENTS.md', 'CLAUDE.md', '.nz-coder/instruction-file-state.json', '.nz-coder/models/registry.json', '.nz-coder/memory/**'}
    assert required <= set(WORKSPACE_INPUTS)


def test_all_direct_subprocess_launches_are_classified():
    names = {'subprocess.Popen', 'subprocess.run', 'asyncio.create_subprocess_exec', 'multiprocessing.Process'}
    actual: Counter[tuple[str, str]] = Counter()
    lines: dict[tuple[str, str], list[int]] = defaultdict(list)
    for path, tree in _trees():
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func) in names:
                key = (relative, _dotted(node.func))
                actual[key] += 1
                lines[key].append(node.lineno)
    expected = {key: value.count for key, value in PROCESS_SITES.items()}
    problems = [f'{path}:{lines[(path, call)]}: {call} is unclassified or changed; declare identity/trust/env/cwd/owner' for path, call in sorted(set(actual) | set(expected)) if actual.get((path, call), 0) != expected.get((path, call), 0)]
    assert not problems, '\n'.join(problems)
    for boundary in PROCESS_SITES.values():
        assert all((boundary.identity, boundary.trust, boundary.environment, boundary.cwd, boundary.owner))


def test_model_reachable_direct_io_is_inventoried():
    method_names = {'read_text', 'read_bytes', 'write_text', 'write_bytes', 'open', 'unlink'}
    actual: Counter[str] = Counter()
    for path, tree in _trees():
        relative = path.relative_to(ROOT).as_posix()
        if not relative.startswith(('nz_coder/tools/', 'nz_coder/capabilities/')):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted(node.func).split('.')[-1] in method_names:
                actual[relative] += 1
    expected = {path: count for path, (_kind, count) in MODEL_IO_MODULE_COUNTS.items()}
    assert actual == expected, f'direct model-reachable I/O changed: expected {expected}, found {dict(actual)}; route through WorkspaceFileAccess or update the reviewed inventory'


def test_raw_exception_projection_sites_are_classified():
    actual: Counter[str] = Counter()
    for path, tree in _trees():
        relative = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _dotted(node.func) not in {'str', 'repr'} or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in {'exc', 'error', 'e'}:
                actual[relative] += 1
    expected = {path: count for path, (_kind, count) in PUBLIC_ERROR_MODULE_COUNTS.items()}
    assert actual == expected, f'raw exception projection inventory changed: expected {expected}, found {dict(actual)}; use PublicError or classify the reviewed site'
    assert {kind for kind, _count in PUBLIC_ERROR_MODULE_COUNTS.values()} <= {'private diagnostic', 'trusted local validation', 'public/model-visible'}


def test_cross_run_resources_declare_ownership_and_revocation():
    required = {'persistent process', 'background agent', 'workflow child', 'MCP runtime', 'LSP client', 'Provider runtime', 'sidecar', 'repo watcher', 'event bus'}
    assert set(CROSS_RUN_RESOURCES) == required
    assert all(len(fields) == 4 and all(fields) for fields in CROSS_RUN_RESOURCES.values())
