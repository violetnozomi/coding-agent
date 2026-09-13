"""Machine-verifiable inventory of NZ-Coder security boundary entry points."""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "nz_coder",)
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

HOST_CONFIG_KEYS = {
    'nz_coder/evaluation/aider_benchmark.py': {'WORKDIR'},
    'nz_coder/evaluation/benchmark.py': {'MODEL_ID', 'WORKDIR'},
    'nz_coder/evaluation/eval_runner.py': {'API_KEY', 'MAX_AGENT_TURNS'},
    'nz_coder/evaluation/provider_smoke.py': {'MODEL_ID', 'MODEL_PROVIDER'},
    'nz_coder/http_service/cli.py': {'API_KEY'},
    'nz_coder/http_service/manager.py': {'PERMISSION_MODE'},
    'nz_coder/intelligence/code_index.py': {'REPO_MAP_MAX_FILE_BYTES'},
    'nz_coder/interface/agent_catalog.py': {'MODEL_ID', 'SUBAGENT_EXPLORE_MODEL'},
    'nz_coder/interface/cli.py': {'AUTO_MODE_CLASSIFIER_ENABLED', 'PERMISSION_MODE'},
    'nz_coder/interface/config_cli.py': {'PERMISSION_MODE'},
    'nz_coder/interface/setup/doctor.py': {'LSP_ENABLED', 'MCP_ENABLED', 'PERMISSION_MODE'},
    'nz_coder/interface/remote.py': {'REMOTE_EVENT_QUEUE_SIZE'},
    'nz_coder/project_creation/verifier.py': {'PROJECT_VERIFY_TIMEOUT_SECONDS'},
    'nz_coder/providers/capabilities.py': {
        'MAX_CONTEXT_TOKENS', 'MAX_OUTPUT_TOKENS', 'MODEL_CAPABILITIES_JSON',
        'MODEL_CATALOG_JSON', 'MODEL_CATALOG_PATH', 'MODEL_ID', 'MODEL_PROVIDER',
        'MODEL_VARIANT',
    },
    'nz_coder/providers/models.py': {
        'ANTHROPIC_API_VERSION', 'MODEL_CATALOG_JSON', 'MODEL_CATALOG_PATH',
        'MODEL_ID', 'MODEL_PROVIDER', 'MODEL_VARIANT',
    },
    'nz_coder/providers/registry.py': {
        'MODEL_REGISTRY_PATH', 'MODEL_REGISTRY_TTL_SECONDS', 'MODEL_REGISTRY_URL',
    },
    'nz_coder/runtime/conversation/message_projection.py': {'PASS_REASONING_CONTENT'},
    'nz_coder/runtime/core/execution_context.py': {
        'AGENT_TIMEOUT_SECONDS', 'MAX_AGENT_TURNS', 'MAX_PARALLEL_TASKS',
        'NOMINAL_AGENT_TURNS',
    },
    'nz_coder/state/context.py': {
        'CONTEXT_REPLAY_COMPACTION_TOKENS', 'MAX_CONTEXT_TOKENS',
        'MAX_OUTPUT_TOKENS', 'PERSIST_OUTPUT_TRIGGER', 'PERSIST_PREVIEW_CHARS',
    },
    'nz_coder/state/memory.py': {
        'MEMORY_AUTO_DREAM', 'MEMORY_AUTO_DREAM_MIN_HOURS',
        'MEMORY_AUTO_DREAM_MIN_NEW_SESSIONS', 'MEMORY_AUTO_EXTRACT',
        'MEMORY_CLEANUP_DAYS',
    },
    'nz_coder/state/sessions.py': {'PERMISSION_MODE', 'SESSION_DIR'},
    'nz_coder/state/skills.py': {'CONFIG_SNAPSHOT', 'SKILLS_DIR'},
    'nz_coder/state/trace.py': {'TRACE_DIR'},
    'nz_coder/state/workdir.py': {'WORKDIR'},
    'nz_coder/state/workspace.py': {'PERMISSION_MODE'},
    'nz_coder/swebench/cli.py': {
        'MAX_AGENT_TURNS', 'MODEL_ID', 'MODEL_PROVIDER', 'SWE_NOMINAL_AGENT_TURNS',
    },
    'nz_coder/tool_platform/permissioning/manager.py': {'PERMISSION_MODE'},
}

CONFIG_SITE_INVENTORY_SHA256 = '3356a0c9eee42e2d0e1502da118627e6eb04b4037841509ed46f219e9babf40a'


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
    ('nz_coder/evaluation/aider_benchmark.py', 'subprocess.run'): ProcessBoundary(2, 'benchmark command', 'operator invocation', 'evaluation profile', 'fixture workspace', 'evaluation harness'),
    ('nz_coder/evaluation/behavioral.py', 'subprocess.run'): ProcessBoundary(3, 'test command', 'evaluation fixture', 'evaluation profile', 'fixture workspace', 'evaluation harness'),
    ('nz_coder/evaluation/benchmark.py', 'subprocess.run'): ProcessBoundary(12, 'benchmark command', 'operator invocation', 'evaluation profile', 'fixture workspace', 'evaluation harness'),
    ('nz_coder/evaluation/core_capability.py', 'subprocess.run'): ProcessBoundary(2, 'test command', 'evaluation fixture', 'evaluation profile', 'fixture workspace', 'evaluation harness'),
    ('nz_coder/evaluation/eval_runner.py', 'subprocess.run'): ProcessBoundary(5, 'evaluation command', 'operator invocation', 'evaluation profile', 'fixture workspace', 'evaluation harness'),
    ('nz_coder/evaluation/reference_adapter.py', 'subprocess.Popen'): ProcessBoundary(1, 'reference product', 'operator configuration', 'evaluation profile', 'fixture workspace', 'evaluation adapter'),
    ('nz_coder/evaluation/reference_adapter.py', 'subprocess.run'): ProcessBoundary(3, 'reference command', 'operator configuration', 'evaluation profile', 'fixture workspace', 'evaluation adapter'),
    ('nz_coder/evaluation/terminal_product.py', 'subprocess.run'): ProcessBoundary(1, 'terminal smoke command', 'product installation', 'evaluation profile', 'fixture workspace', 'evaluation harness'),
    ('nz_coder/http_service/daemon.py', 'subprocess.Popen'): ProcessBoundary(1, 'daemon executable', 'host operation', 'scrubbed service', 'explicit workspace', 'daemon launcher'),
    ('nz_coder/intelligence/code_index.py', 'subprocess.run'): ProcessBoundary(1, 'git helper', 'host operation', 'scrubbed helper', 'repository', 'code index'),
    ('nz_coder/intelligence/impact_analyzer.py', 'subprocess.run'): ProcessBoundary(3, 'git helper', 'host operation', 'scrubbed helper', 'repository', 'impact analyzer'),
    ('nz_coder/intelligence/repository_graph.py', 'subprocess.run'): ProcessBoundary(1, 'git helper', 'host operation', 'scrubbed helper', 'repository', 'repository graph'),
    ('nz_coder/intelligence/verification_planner.py', 'subprocess.run'): ProcessBoundary(3, 'test helper', 'verification plan', 'scrubbed helper', 'repository', 'verification planner'),
    ('nz_coder/interface/clipboard.py', 'subprocess.run'): ProcessBoundary(1, 'clipboard helper', 'interactive user action', 'scrubbed host', 'none', 'clipboard adapter'),
    ('nz_coder/project_creation/verifier.py', 'subprocess.run'): ProcessBoundary(1, 'verification command', 'generated project', 'scrubbed helper', 'project', 'project verifier'),
    ('nz_coder/state/context.py', 'subprocess.run'): ProcessBoundary(1, 'compression helper', 'product configuration', 'scrubbed helper', 'run workspace', 'context manager'),
    ('nz_coder/state/instructions.py', 'subprocess.run'): ProcessBoundary(1, 'git helper', 'host operation', 'scrubbed helper', 'repository', 'instruction discovery'),
    ('nz_coder/state/workspace.py', 'subprocess.run'): ProcessBoundary(1, 'git helper', 'host operation', 'scrubbed helper', 'repository', 'workspace state'),
    ('nz_coder/swebench/adapter.py', 'subprocess.run'): ProcessBoundary(3, 'SWE harness command', 'evaluation fixture', 'evaluation profile', 'instance workspace', 'SWE adapter'),
    ('nz_coder/swebench/orchestrator.py', 'subprocess.run'): ProcessBoundary(4, 'SWE harness command', 'operator invocation', 'evaluation profile', 'instance workspace', 'SWE orchestrator'),
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
    'nz_coder/capabilities/documents.py': ('derived-cache-and-attachment-compat', 9),
    'nz_coder/capabilities/web_search.py': ('remote-transport', 3),
    'nz_coder/tools/bash.py': ('model-shell-workspace-probe', 1),
    'nz_coder/tools/files.py': ('workspace-file-access', 0),
    'nz_coder/tools/plan_mode.py': ('legacy-model-reachable', 3),
    'nz_coder/tools/python_ast.py': ('workspace-file-access', 0),
    'nz_coder/tools/read_support.py': ('legacy-model-reachable', 3),
    'nz_coder/tools/repo_intel.py': ('workspace-file-access', 0),
    'nz_coder/tools/search.py': ('workspace-file-access', 0),
    'nz_coder/tools/scratchpad.py': ('private-user-state', 1),
    'nz_coder/tools/todo.py': ('private-user-state', 1),
    'nz_coder/tools/webfetch.py': ('remote-transport', 1),
}


PUBLIC_ERROR_MODULE_COUNTS = {
    'nz_coder/evaluation/eval_runner.py': ('trusted local validation', 2),
    'nz_coder/evaluation/provider_smoke.py': ('trusted local validation', 1),
    'nz_coder/foundation/error_classification.py': ('private diagnostic', 1),
    'nz_coder/foundation/private_paths.py': ('trusted local validation', 1),
    'nz_coder/foundation/workspace_paths.py': ('trusted local validation', 1),
    'nz_coder/foundation/workspace_trust.py': ('trusted local validation', 1),
    'nz_coder/intelligence/semantic.py': ('trusted local validation', 1),
    'nz_coder/intelligence/verification.py': ('trusted local validation', 1),
    'nz_coder/interface/commands/handlers/core.py': ('trusted local validation', 8),
    'nz_coder/interface/commands/handlers/workflow.py': ('trusted local validation', 3),
    'nz_coder/interface/custom_commands.py': ('trusted local validation', 1),
    'nz_coder/interface/setup/doctor.py': ('trusted local validation', 2),
    'nz_coder/project_creation/verifier.py': ('trusted local validation', 1),
    'nz_coder/runtime/agent/agent_manager.py': ('private diagnostic', 1),
    'nz_coder/runtime/agent/agent_resilience.py': ('trusted local validation', 1),
    'nz_coder/runtime/execution/loop.py': ('private diagnostic', 20),
    'nz_coder/runtime/execution/provider_stream.py': ('private diagnostic', 1),
    'nz_coder/runtime/execution/run_lifecycle.py': ('private diagnostic', 2),
    'nz_coder/runtime/execution/runner.py': ('private diagnostic', 2),
    'nz_coder/runtime/execution/services.py': ('trusted local validation', 1),
    'nz_coder/runtime/model_gateway/errors.py': ('private diagnostic', 3),
    'nz_coder/runtime/model_gateway/gateway.py': ('private diagnostic', 12),
    'nz_coder/runtime/model_gateway/models.py': ('private diagnostic', 4),
    'nz_coder/runtime/verification/hooks.py': ('private diagnostic', 4),
    'nz_coder/runtime/verification/recovery.py': ('private diagnostic', 3),
    'nz_coder/runtime/verification/stall_sidecar.py': ('private diagnostic', 1),
    'nz_coder/runtime/workflows/workflow_generation.py': ('trusted local validation', 1),
    'nz_coder/state/context.py': ('private diagnostic', 1),
    'nz_coder/state/trace.py': ('private diagnostic', 2),
    'nz_coder/state/workspace.py': ('trusted local validation', 1),
    'nz_coder/swebench/orchestrator.py': ('private diagnostic', 5),
    'nz_coder/swebench/submission.py': ('trusted local validation', 1),
    'nz_coder/tool_platform/results.py': ('private diagnostic', 1),
    'nz_coder/tools/bash.py': ('trusted local validation', 1),
    'nz_coder/tools/question.py': ('private diagnostic', 1),
    'nz_coder/tools/read_support.py': ('trusted local validation', 1),
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


class _ConfigSiteVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.config_aliases = {'config'}
        self.direct_names: dict[str, str] = {}
        self.owners: list[str] = ['<module>']
        self.sites: set[tuple[str, str, str, str]] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.endswith(('.config', 'foundation.config')):
                self.config_aliases.add(alias.asname or alias.name.split('.')[-1])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ''
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name == 'config' or module.endswith('.config') and alias.name == '*':
                self.config_aliases.add(local)
            elif module.endswith(('config', 'foundation.config')) and alias.name.isupper():
                self.direct_names[local] = alias.name

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in self.config_aliases
            and node.attr.isupper()
        ):
            self.sites.add((self.path, '.'.join(self.owners), node.attr, 'attribute'))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        key = self.direct_names.get(node.id)
        if key:
            self.sites.add((self.path, '.'.join(self.owners), key, 'from-import'))

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == 'getattr'
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in self.config_aliases
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value.isupper()
        ):
            self.sites.add((
                self.path, '.'.join(self.owners), node.args[1].value, 'getattr',
            ))
        self.generic_visit(node)


def _config_sites(source: str, path: str = 'fixture.py') -> set[tuple[str, str, str, str]]:
    visitor = _ConfigSiteVisitor(path)
    visitor.visit(ast.parse(source))
    return visitor.sites


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return '.'.join(reversed(parts))


def _raw_exception_projection_kinds(source: str) -> set[str]:
    tree = ast.parse(source)
    names = {'exc', 'error', 'e'}
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and any(
            isinstance(child, ast.FormattedValue)
            and isinstance(child.value, ast.Name)
            and child.value.id in names
            for child in node.values
        ):
            kinds.add('fstring')
        if isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if (dotted == 'format' or dotted.endswith('.format')) and any(
                isinstance(arg, ast.Name) and arg.id in names for arg in node.args
            ):
                kinds.add('format')
            if dotted.startswith('logging.') and any(
                isinstance(arg, ast.Name) and arg.id in names for arg in node.args
            ):
                kinds.add('logging')
        if (
            isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
            and isinstance(node.right, ast.Name) and node.right.id in names
        ):
            kinds.add('percent')
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Name) and value.id in names and any(
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value in {'error', 'metadata'}
                for target in targets
            ):
                kinds.add('field-assignment')
    return kinds


def test_all_direct_config_reads_are_classified():
    sites: set[tuple[str, str, str, str]] = set()
    for path, tree in _trees():
        relative = path.relative_to(ROOT).as_posix()
        visitor = _ConfigSiteVisitor(relative)
        visitor.visit(tree)
        sites.update(visitor.sites)
    allowed = {
        path: keys for path, (_count, keys) in CONFIG_READS.items()
    }
    allowed.update(HOST_CONFIG_KEYS)
    problems: list[str] = []
    for path, owner, key, kind in sorted(sites):
        if key not in allowed.get(path, set()):
            problems.append(
                f'{path}:{owner}: config {kind} {key} is not in the reviewed inventory'
            )
    payload = '\n'.join('|'.join(site) for site in sorted(sites)).encode()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != CONFIG_SITE_INVENTORY_SHA256:
        problems.append(
            f'per-site config inventory changed: {digest}; review path/owner/key/kind'
        )
    assert not problems, '\n'.join(problems)


def test_gate_detects_config_alias():
    sites = _config_sites(
        'from nz_coder.foundation import config as cfg\nvalue = cfg.API_KEY\n'
    )
    assert ('fixture.py', '<module>', 'API_KEY', 'attribute') in sites


def test_gate_detects_from_import_config():
    sites = _config_sites(
        'from nz_coder.foundation.config import API_KEY as key\nvalue = key\n'
    )
    assert ('fixture.py', '<module>', 'API_KEY', 'from-import') in sites


def test_gate_detects_getattr_config():
    sites = _config_sites(
        'from nz_coder.foundation import config as cfg\nvalue = getattr(cfg, "API_KEY")\n'
    )
    assert ('fixture.py', '<module>', 'API_KEY', 'getattr') in sites


def test_gate_detects_same_count_location_swap():
    first = _config_sites(
        'from nz_coder.foundation import config\ndef safe():\n return config.API_KEY\n'
    )
    second = _config_sites(
        'from nz_coder.foundation import config\ndef unsafe():\n return config.API_KEY\n'
    )
    assert first != second


def test_gate_scans_all_production_roots():
    discovered = {path.relative_to(ROOT).parts[1] for path, _tree in _trees()}
    assert {
        'foundation', 'state', 'interface', 'http_service', 'intelligence',
        'extensions', 'runtime', 'tools', 'capabilities', 'providers', 'mcp', 'lsp',
    } <= discovered


def test_gate_detects_lsp_direct_path_read():
    client = (ROOT / 'nz_coder/lsp/client.py').read_text(encoding='utf-8')
    tree = ast.parse(client)
    forbidden = {
        _dotted(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _dotted(node.func).split('.')[-1] in {'read_text', 'read_bytes'}
    }
    assert forbidden == set()


def test_gate_detects_indirect_exception_fstring():
    source = '''
import logging
def leak(exc):
    message = f"failed: {exc}"
    other = "{}".format(exc)
    old = "%s" % exc
    logging.error("failed", exc)
    payload = {}
    payload["error"] = exc
    return message, other, old, payload
'''
    assert _raw_exception_projection_kinds(source) == {
        'fstring', 'format', 'percent', 'logging', 'field-assignment',
    }


def test_secret_like_runtime_dataclass_fields_are_repr_safe():
    expectations = {
        'nz_coder/runtime/core/run_settings.py': {'image_api_key'},
        'nz_coder/providers/configuration.py': {'api_key'},
        'nz_coder/mcp/config.py': {'environment', 'headers', 'execution_identity'},
        'nz_coder/foundation/execution_identity.py': {'command', 'argv_semantics'},
        'nz_coder/runtime/execution/run_control.py': {
            'config_snapshot', 'mcp_runtime', 'model_runtime', 'provider_runtimes',
            'run_settings', 'image_describer', 'sidecar_verifier',
        },
    }
    missing: list[str] = []
    for relative, fields in expectations.items():
        tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'))
        assignments: dict[str, list[ast.expr | None]] = defaultdict(list)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
            ):
                assignments[node.target.id].append(node.value)
        for name in fields:
            values = assignments.get(name, [])
            if not any(
                isinstance(value, ast.Call)
                and _dotted(value.func).endswith('field')
                and any(
                    keyword.arg == 'repr'
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                    for keyword in value.keywords
                )
                for value in values
            ):
                missing.append(f'{relative}:{name}')
    assert not missing, f'secret-bearing dataclass fields expose repr: {missing}'


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
    method_names = {
        'read_text', 'read_bytes', 'write_text', 'write_bytes', 'open', 'unlink',
        'iterdir', 'scandir',
    }
    actual: Counter[str] = Counter()
    for path, tree in _trees():
        relative = path.relative_to(ROOT).as_posix()
        if not relative.startswith(('nz_coder/tools/', 'nz_coder/capabilities/')):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _dotted(node.func).split('.')[-1] in method_names
                and not _is_workspace_file_access_call(node)
            ):
                actual[relative] += 1
    expected = {
        path: count
        for path, (_kind, count) in MODEL_IO_MODULE_COUNTS.items()
        if count
    }
    assert actual == expected, f'direct model-reachable I/O changed: expected {expected}, found {dict(actual)}; route through WorkspaceFileAccess or update the reviewed inventory'


def _is_workspace_file_access_call(node: ast.Call) -> bool:
    if not isinstance(node.func, ast.Attribute):
        return False
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id in {'access', 'file_access'}:
        return True
    return (
        isinstance(owner, ast.Call)
        and _dotted(owner.func).endswith('WorkspaceFileAccess')
    )


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


def test_public_error_contract_rejects_raw_exception_output():
    """No reviewed raw exception projection may target a public surface."""
    public = {
        path: count for path, (kind, count) in PUBLIC_ERROR_MODULE_COUNTS.items()
        if kind == 'public/model-visible' and count
    }
    assert public == {}, (
        f'raw exception output remains on public surfaces: {public}; '
        'project through PublicError, TrustedPublicMessage, or PublicInputError'
    )


def test_cross_run_resources_declare_ownership_and_revocation():
    required = {'persistent process', 'background agent', 'workflow child', 'MCP runtime', 'LSP client', 'Provider runtime', 'sidecar', 'repo watcher', 'event bus'}
    assert set(CROSS_RUN_RESOURCES) == required
    assert all(len(fields) == 4 and all(fields) for fields in CROSS_RUN_RESOURCES.values())
