# NZ-Coder Package Structure Reorganization Design

## Status

Approved for implementation on 2026-08-31.

## Problem

NZ-Coder has grown to more than 117,000 physical lines of product Python.
The package root contains 47 Python files, including 25 historical
compatibility wrappers, while `nz_coder/runtime/` contains roughly 70 flat
modules before its existing subpackages are counted. Responsibilities such as
message protocol, media handling, recovery, workflow execution, process
management, and public entrypoints are therefore visually mixed.

The structure makes ownership difficult to infer and encourages new modules to
be added to whichever directory is already imported nearby. It also hides one
unreferenced scratch module, `nz_coder/test.py`, which imports undeclared
`numpy` and is not part of the product.

## Reference Baseline

The organization uses the real `references/InfCodeX` source and the local
`Claude-Code-main` only as structural references:

- InfCodeX separates agent runtime, resilience, context, media, permissions,
  providers, repo intelligence, tools, and workflows.
- Claude Code separates assistant behavior, CLI, context, services, state,
  tasks, and tools.
- NZ-Coder keeps its own framework-free architecture. It does not copy their
  monorepo packaging, TypeScript conventions, or runtime abstractions.

## Compatibility Boundary

The following are formal compatibility surfaces and remain importable or
invokable from their current paths:

- `nz_coder.sdk`
- `nz_coder.cli`
- `nz_coder.loop`
- `nz_coder.permissions`
- `nz_coder.benchmark`
- `nz_coder.aider_benchmark`
- `nz_coder.eval_runner`
- `nz_coder.swebench_lite`
- `python -m nz_coder`
- all configured console scripts in `pyproject.toml`
- all registered tool names, schemas, side effects, handlers, and string
  result conventions

Historical root imports that expose internal implementation modules are not a
compatibility requirement. Repository code, tests, and documentation migrate
to canonical package paths before their wrappers are removed.

## Target Top-Level Structure

```text
nz_coder/
├── __init__.py
├── __main__.py
├── cli.py
├── loop.py
├── permissions.py
├── sdk.py
├── benchmark.py
├── aider_benchmark.py
├── eval_runner.py
├── swebench_lite.py
├── foundation/
├── protocol/
├── capabilities/
├── runtime/
├── state/
├── intelligence/
├── interface/
├── tool_platform/
├── tools/
├── providers/
├── swebench/
├── evaluation/
├── http_service/
├── project_creation/
├── mcp/
└── lsp/
```

## Root Module Ownership

### Foundation

`nz_coder/foundation/` owns dependency-light primitives:

- `config.py`
- `async_utils.py`
- `json_safety.py`
- `private_paths.py`

Foundation must not import runtime, interface, evaluation, SWE, or tool
implementations.

### Protocol

`nz_coder/protocol/` owns data exchanged across terminal, HTTP, session, and
runtime boundaries:

- `message_schema.py`
- `attachments.py`
- `session_events.py`

Protocol may depend on foundation and the standard library. It must not depend
on runtime orchestration.

### Capabilities

`nz_coder/capabilities/` owns product capabilities that are usable without the
Agent loop:

- `documents.py`
- `vision.py`
- `web_search.py`
- `ripgrep.py`

### Existing Domain Packages

- `doctor.py` and `initializer.py` move to `interface/setup/`.
- `reviewer.py` moves to `intelligence/reviewer.py`.
- `session_stats.py` moves to `state/session_stats.py`.
- `run_evidence.py` moves to `runtime/observability/run_evidence.py`.
- `verification_evidence.py` moves to
  `runtime/verification/evidence.py`.
- `recovery.py` moves to `runtime/verification/recovery.py`.
- `nz_coder/test.py` is deleted.

## Runtime Structure

After the root migration, flat runtime modules are grouped without changing
their behavior:

- `runtime/agent/`: admission, agent management, child execution, handoffs,
  lineage, planning, subagents, and resilience.
- `runtime/conversation/`: prompts, context, message projection, structured
  output, input preflight, think-tag handling, and usage history.
- `runtime/execution/`: loop, runner, host, composition, lifecycle, services,
  runtime state, budgets, and the concrete tool executor.
- `runtime/verification/`: recovery, completion gates, hooks, Sidecar, stalls,
  judge logic, verification contracts, and scheduling.
- `runtime/workflows/`: every current `workflow_*` implementation.
- `runtime/process/`: process backends, process service, platform runtime,
  workdir, snapshots, and workspace state.
- `runtime/session/`: the existing session package plus current flat
  `session_*` modules.
- `runtime/core/`: retains the execution-local override context beside its
  other dependency-light runtime contracts.
- `runtime/tool_runtime/`: owns scheduling, policy, result projection, the
  batch pipeline, and tool observers.
- Existing `adapters/`, `model_gateway/`, and `worktree/` retain their names.
- `tool_platform/execution.py` owns the tool result, workspace-mutation
  classification, and command-failure contracts shared by runtime execution
  and leaf packages.

Package `__init__.py` files remain dependency-light. They do not eagerly import
large surfaces or trigger tool registration.

## Dependency Direction

```text
foundation
    -> protocol
    -> state / intelligence / capabilities
    -> tool_platform / providers
    -> runtime leaf packages
    -> runtime.execution
    -> interface / http_service / evaluation / swebench
```

The arrows mean “may be imported by”. Lower-level packages must not import
higher-level orchestration packages. Runtime cycles are prevented by moving
modules in cohesive groups, retaining dependency injection, and avoiding
re-export-heavy package initializers.

Two lazy imports are explicit composition seams rather than shared primitive
dependencies:

- `runtime/adapters/runner.py` may construct
  `runtime.execution.run_lifecycle.ProductionRunLifecycle`.
- `runtime/agent/subagent.py` may obtain the declared production runtime from
  `runtime.execution.composition` when no runner has been injected.

An AST guard allowlists exactly those file/module pairs. No other runtime leaf
module may import `runtime.execution`; shared contracts belong in lower-level
packages.

## Migration Strategy

The work is split into two independently testable implementation plans:

1. Root package cleanup and canonical imports.
2. Runtime subpackage decomposition.

Each move follows the same sequence:

1. Add or update an architecture/import test that fails against the old
   structure.
2. Move one cohesive group.
3. Rewrite repository-owned imports and monkeypatch targets.
4. Run focused tests for that group.
5. Run package compilation and broader regression tests.

No feature behavior, prompt content, tool schema, verification policy, or
provider behavior changes during structural migration.

## Verification

The final structure must satisfy all of the following:

- package-root allowlist test passes;
- dependency-boundary AST test passes;
- the runtime-leaf AST guard reports no execution imports outside the two
  exact lazy composition seams;
- no repository-owned Python import references a removed internal root module;
- formal public imports succeed;
- console-script parsers and `python -m` entrypoints succeed;
- tool registration/catalog tests show no lost or duplicate tools;
- `python -m compileall -q nz_coder` succeeds;
- full `pytest -q` succeeds with no new failures;
- tests skipped before the migration remain the only expected skips.

## Safety and Worktree Policy

The current worktree contains substantial existing user changes. The migration
must not reset, clean, or overwrite unrelated changes. Automated commits are
omitted because moved files already contain user-owned modifications that
cannot safely be separated into structural-only commits.
