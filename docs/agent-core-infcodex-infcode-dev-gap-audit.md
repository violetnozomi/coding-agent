# NZ-Coder Agent Core / InfCodeX / infcode-dev gap audit

Date: 2026-09-13
Fix round: 2026-09-13 — corrected source/runtime availability, added external package/doc references, and separated historical benchmark snapshots.
Scope: runtime capability parity and product-host parity, kept as separate comparisons.

This is a repository evidence audit, not a claim that a similarly named file implies
behavior. NZ-Coder evidence below points to concrete symbols and tests. The ignored
reference trees are present at `references/InfCodeX` and `infcode-dev/infcode-dev`;
source inspection and runtime availability are tracked separately. The InfCodeX
runtime probe is available through `InfCodeXReferenceAdapter.probe`; the infcode-dev
probe is unavailable because Bun 1.3.13 is not installed. Paths in this report are
root-relative and point to concrete packages, source directories, docs, or symbols.

Statuses are `implemented`, `partial`, `planned`, `different-by-design`, and
`unavailable`. `unavailable` describes a probe or execution path, never an absent
source tree. Priorities are P0 (blocking core parity), P1 (near-term), P2 (useful
product parity), and P3 (deferred polish).

## Benchmark snapshots

The two InfCodeX measurements are historical reference snapshots and are intentionally
not NZ-Coder scores:

| Snapshot | Recorded value | Interpretation |
| --- | ---: | --- |
| InfCodeX v2 | `0.8666666666666667` | recorded reference success rate |
| older InfCodeX snapshot | `0.0` | legacy snapshot; not comparable as a current score |

No current NZ-Coder score is inferred from either value. The local adapter only probes
whether a reference checkout can run and preserves `unavailable` when it cannot
(`nz_coder/evaluation/reference_adapter.py:InfCodeXReferenceAdapter.probe`). The
InfCodeX probe is available; infcode-dev is blocked only by the Bun 1.3.13 requirement
(`nz_coder/evaluation/reference_adapter.py:OpenCodeReferenceAdapter.probe`).

## Three-way capability matrix

| Area | NZ-Coder status / evidence | InfCodeX runtime parity | infcode-dev product-host parity | Priority | Bounded gap, target, and acceptance |
| --- | --- | --- | --- | :---: | --- |
| Core runner, session, and events | `implemented`: `runtime/execution/runner.py`, `runtime/core/state.py:RunState`, `protocol/session_events.py`; lifecycle tests `tests/test_session_lifecycle.py`, `tests/test_session_events.py` | `partial`: source and runtime probe are available at `references/InfCodeX/packages/coding/src/session.ts`, `references/InfCodeX/packages/coding/src/managed-protocol.ts`, and `references/InfCodeX/src/runtime-daemon/`; terminal-event behavior still needs a parity run | `partial`: HTTP/session host exists in `http_service/manager.py` and `tests/test_http_service.py`; infcode-dev protocol sources are in `infcode-dev/infcode-dev/packages/opencode/src/` and `docs/` | P0 | Gap is an evidence gap plus host protocol confirmation. Target `runtime/execution/runner.py`, `http_service/manager.py`. Acceptance: `pytest tests/test_session_lifecycle.py tests/test_session_events.py tests/test_http_service.py` and a checked-out reference run that emits a terminal event. |
| Providers and model capabilities | `implemented`: `providers/registry.py`, `providers/models.py`, `providers/capabilities.py`; tests `tests/test_providers.py`, `tests/test_model_capabilities.py` | `partial`: provider sources are available in `references/InfCodeX/packages/llm/src/` and `references/InfCodeX/packages/coding/src/provider-policy.ts`; live credentials are not available | `partial`: connect/discovery CLI in `providers/connect.py`, `interface/setup/doctor.py`; infcode-dev provider sources are in `infcode-dev/infcode-dev/packages/opencode/src/` and `docs/` | P0 | Target provider capability snapshot and explicit unsupported behavior in `providers/`. Acceptance: `pytest tests/test_providers.py tests/test_provider_connect.py tests/test_model_capabilities.py`; live reference probe remains opt-in. |
| Tool registry, exposure, and permissions | `implemented`: `tool_platform/catalog.py:ToolCatalog`, `tool_platform/exposure.py`, `permissions.py`; tests `tests/tool_platform/test_exposure.py`, `tests/test_permissions.py`, `tests/test_tool_schema.py` | `partial`: tool and MCP sources are available in `references/InfCodeX/packages/coding/src/tools/` and `references/InfCodeX/config-templates/integrations/mcp.example.jsonc`; parity is not yet established | `partial`: extensions and MCP tools are discoverable through `extensions/registry.py` and `mcp/`; infcode-dev references are `packages/opencode/src/`, `packages/plugin/src/`, and `docs/` | P0 | Target a versioned cross-product tool catalog and permission contract. Acceptance: `pytest tests/tool_platform/test_exposure.py tests/test_permissions.py tests/test_mcp_catalog.py`; compare the same catalog against a reference checkout. |
| Coding, repository intelligence, and indexing | `implemented`: `intelligence/code_index.py`, `repository_graph.py`, `service.py`, `lsp/`; tests `tests/test_repo_map.py`, `tests/test_repository_graph.py`, `tests/test_repo_intelligence_closure.py`, `tests/test_lsp.py` | `partial`: indexer sources are available in `references/InfCodeX/packages/coding/src/repo-intelligence/index.ts` and `protocol.ts`; transport semantics still need comparison | `partial`: indexing sources are in `infcode-dev/infcode-dev/packages/kilo-indexing/`; GUI/editor integration references are in `packages/gui/`, `apps/vscode/`, and `docs/` | P0 | Target language/index protocol and freshness guarantees. Acceptance: `pytest tests/test_repo_map.py tests/test_repository_graph.py tests/test_repo_intelligence_closure.py tests/test_lsp.py`; add fixture comparison once infcode-dev is available. |
| Child agents, background work, handoff, and worktrees | `implemented`: `runtime/agent/subagent.py`, `handoffs.py`, `child_result.py`, `runtime/worktree/`; tests `tests/test_subagent.py`, `tests/test_handoffs.py`, `tests/test_child_result.py`, `tests/test_agent_manager.py` | `partial`: agent sources are available in `references/InfCodeX/packages/coding/src/agents/` and `child-fallback.ts`; scheduler/worktree parity remains unverified | `partial`: host surfaces expose runs; infcode-dev run/agent sources are in `packages/opencode/` and `docs/` | P0 | Target stable child result and handoff wire schema across hosts. Acceptance: `pytest tests/test_subagent.py tests/test_handoffs.py tests/test_child_result.py tests/test_agent_manager.py`; then replay an external reference trajectory. |
| Planning, verification, and recovery | `implemented`: `runtime/agent/planning_runtime.py`, `runtime/verification/`, `runtime/agent/agent_resilience.py`; tests `tests/test_plan_mode.py`, `tests/test_verification.py`, `tests/test_recovery.py`, `tests/test_gap_recovery_phase3.py` | `partial`: translated InfCodeX contracts for stall detector, sidecar verifier, and LLM judge are present (`tests/test_stall_detector.py`, `tests/test_sidecar_verifier.py`, `tests/test_llm_judge.py`), but no upstream execution is available | `partial`: product workflow verifier exists, but no IDE review/repair host evidence | P0 | Target end-to-end evidence propagation from plan to verification and recovery. Acceptance: `pytest tests/test_plan_mode.py tests/test_verification.py tests/test_recovery.py tests/test_stall_detector.py tests/test_sidecar_verifier.py`. |
| Memory | `implemented`: `runtime/adapters/memory.py`, `runtime/conversation/context_manager.py`, `tests/test_memory.py`, `tests/test_memory_control.py`, `tests/test_scratchpad.py` | `partial`: session/memory sources are available in `references/InfCodeX/packages/coding/src/session.ts`; retention parity remains unverified | `partial`: session persistence is host-visible, but product memory UX and migration contract are unverified | P1 | Target documented scope/retention and export/import contract. Acceptance: `pytest tests/test_memory.py tests/test_memory_control.py tests/test_scratchpad.py tests/test_session_revert.py`. |
| Skills, MCP, and extensions | `implemented`: `extensions/registry.py`, `mcp/runtime.py`, `mcp/config.py`, bundled skills; tests `tests/test_extensions.py`, `tests/test_mcp.py`, `tests/test_skill_loading.py`, `tests/test_skill_governance.py` | `partial`: extension/MCP references are available in `references/InfCodeX/config-templates/integrations/`; registry parity remains unverified | `partial`: registry and CLI exist; infcode-dev plugin/host install references are in `infcode-dev/infcode-dev/packages/plugin/src/` and `docs/` | P1 | Target portable extension manifest and trust/permission mapping. Acceptance: `pytest tests/test_extensions.py tests/test_mcp.py tests/test_skill_loading.py tests/test_skill_governance.py`. |
| Workflows | `implemented`: `runtime/workflows/workflow_runtime.py`, workflow handlers and capsules; tests `tests/test_workflow_runtime.py`, `tests/test_workflow_advanced.py`, `tests/test_workflow_parity_contracts.py`, `tests/test_workflow_capsules.py` | `partial`: parity contracts are explicitly translated from InfCodeX semantics (`tests/test_workflow_parity_contracts.py`), upstream source is present; runtime parity is unverified | `partial`: terminal workflow host is implemented; infcode-dev visual/editor workflow references are in `infcode-dev/infcode-dev/packages/gui/`, `apps/vscode/`, `apps/jetbrains/`, and `docs/` | P1 | Target one canonical workflow state/event model for runtime and hosts. Acceptance: `pytest tests/test_workflow_runtime.py tests/test_workflow_advanced.py tests/test_workflow_parity_contracts.py tests/test_workflow_capsules.py`. |
| SDK, HTTP, and events | `implemented`: `runtime/execution/native_sdk.py`, `http_service/server.py`, `protocol/session_events.py`; tests `tests/test_http_service.py`, `tests/test_mcp_http.py`, `tests/test_openai_responses.py`, `tests/test_message_schema.py` | `partial`: client and daemon sources are available in `references/InfCodeX/packages/coding/src/client.ts` and `references/InfCodeX/src/runtime-daemon/`; event schema parity remains unverified | `partial`: HTTP daemon and native SDK exist, but official infcode-dev client/stream compatibility is unverified | P1 | Target versioned public event schema and compatibility fixture. Acceptance: `pytest tests/test_http_service.py tests/test_openai_responses.py tests/test_message_schema.py tests/test_event_publisher_identity_phase3.py`; run a client fixture against `/health` and event endpoints. |
| CLI, TUI, VS Code, and JetBrains host surfaces | `implemented` for CLI/TUI: `interface/cli.py`, `interface/fullscreen.py`, `interface/run_renderer.py`; tests `tests/test_terminal_interactions.py`, `tests/test_tui_product_frames.py`, `tests/test_headless_cli.py` | `partial`: REPL/client sources are available in `references/InfCodeX/packages/repl/src/` and `references/InfCodeX/clients/`; host parity remains unverified | `partial`: terminal and loopback HTTP hosts are tested; official product references are present at `infcode-dev/infcode-dev/packages/gui/`, `apps/vscode/`, `apps/jetbrains/`, and `docs/` | P1 | Target a host adapter contract before claiming editor parity. Files `interface/remote.py`, `runtime/execution/product_surfaces.py`, new editor adapter module. Acceptance: existing CLI/TUI tests plus a deterministic VS Code/JetBrains protocol fixture; do not mark implemented from package presence. |


### infcode-dev product references

The product-host comparison uses the checked-out sources and official product documentation in `infcode-dev/infcode-dev`: GUI surfaces (`packages/gui/`), VS Code integration (`apps/vscode/`), JetBrains integration (`apps/jetbrains/`), HTTP/SSE and session server contracts (`packages/opencode/` plus `docs/`), and indexing (`packages/kilo-indexing/`). These are direct source/doc references; they do not imply that NZ-Coder already implements those consumers.

## Ordered implementation backlog

1. P0: pin the inspected InfCodeX and infcode-dev revisions, install the missing Bun 1.3.13 runtime, and rerun runtime probes; preserve both historical benchmark snapshots separately.
2. P0: freeze the shared run/session/event, tool/permission, child-result, and verification schemas; add cross-runtime fixture tests.
3. P0: publish repository-intelligence/index freshness and provider capability matrices with negative cases.
4. P1: document memory, extension/MCP trust, workflow, and public SDK compatibility contracts.
5. P1: implement host adapters and conformance fixtures for product clients; only then assess VS Code/JetBrains parity.

## Evidence limits

The matrix distinguishes runtime parity from product-host parity. A test translated
from InfCodeX demonstrates a local contract decision, not successful execution of
InfCodeX. Source trees are available, but only the InfCodeX runtime probe currently
runs; the infcode-dev probe is blocked specifically by the missing Bun 1.3.13 runtime.
The infcode-dev GUI, VS Code, JetBrains, HTTP/SSE, indexing, and product contracts
are cited from their concrete package and documentation paths.
