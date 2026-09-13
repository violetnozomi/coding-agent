# NZ-Coder Agent Core / InfCodeX / infcode-dev gap audit

Date: 2026-09-13  
Scope: runtime capability parity and product-host parity, kept as separate comparisons.

This is a repository evidence audit, not a claim that a similarly named file implies
behavior. NZ-Coder evidence below points to concrete symbols and tests. The InfCodeX
and infcode-dev source trees are not present in this worktree, so their current
implementation is marked `unavailable`; translated contracts and the non-invasive
reference adapter are recorded as evidence of an intended protocol only. A future
run with checked-out references can replace those observations without changing the
matrix schema.

Statuses are `implemented`, `partial`, `planned`, `different-by-design`, and
`unavailable`. Priorities are P0 (blocking core parity), P1 (near-term), P2 (useful
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
(`nz_coder/evaluation/reference_adapter.py:InfCodeXReferenceAdapter.probe`).

## Three-way capability matrix

| Area | NZ-Coder status / evidence | InfCodeX runtime parity | infcode-dev product-host parity | Priority | Bounded gap, target, and acceptance |
| --- | --- | --- | --- | :---: | --- |
| Core runner, session, and events | `implemented`: `runtime/execution/runner.py`, `runtime/core/state.py:RunState`, `protocol/session_events.py`; lifecycle tests `tests/test_session_lifecycle.py`, `tests/test_session_events.py` | `unavailable`: reference runtime is not checked out; translated lifecycle contracts exist in `tests/test_reference_adapter.py` | `partial`: HTTP/session host exists in `http_service/manager.py` and `tests/test_http_service.py`, but no checked-out product protocol to compare | P0 | Gap is an evidence gap plus host protocol confirmation. Target `runtime/execution/runner.py`, `http_service/manager.py`. Acceptance: `pytest tests/test_session_lifecycle.py tests/test_session_events.py tests/test_http_service.py` and a checked-out reference run that emits a terminal event. |
| Providers and model capabilities | `implemented`: `providers/registry.py`, `providers/models.py`, `providers/capabilities.py`; tests `tests/test_providers.py`, `tests/test_model_capabilities.py` | `unavailable`: no InfCodeX provider implementation or live credentials in worktree | `partial`: connect/discovery CLI in `providers/connect.py`, `interface/setup/doctor.py`; no product-provider matrix | P0 | Target provider capability snapshot and explicit unsupported behavior in `providers/`. Acceptance: `pytest tests/test_providers.py tests/test_provider_connect.py tests/test_model_capabilities.py`; live reference probe remains opt-in. |
| Tool registry, exposure, and permissions | `implemented`: `tool_platform/catalog.py:ToolCatalog`, `tool_platform/exposure.py`, `permissions.py`; tests `tests/tool_platform/test_exposure.py`, `tests/test_permissions.py`, `tests/test_tool_schema.py` | `unavailable`: no authoritative InfCodeX catalog in checkout; translated mutation catalog comments are not proof of full parity | `partial`: extensions and MCP tools are discoverable through `extensions/registry.py` and `mcp/`; product consent UX is only terminal/HTTP | P0 | Target a versioned cross-product tool catalog and permission contract. Acceptance: `pytest tests/tool_platform/test_exposure.py tests/test_permissions.py tests/test_mcp_catalog.py`; compare the same catalog against a reference checkout. |
| Coding, repository intelligence, and indexing | `implemented`: `intelligence/code_index.py`, `repository_graph.py`, `service.py`, `lsp/`; tests `tests/test_repo_map.py`, `tests/test_repository_graph.py`, `tests/test_repo_intelligence_closure.py`, `tests/test_lsp.py` | `unavailable`: source/indexer behavior cannot be inspected here | `partial`: README documents an `infcode-dev` path, but that checkout is absent; no IDE index transport is proven | P0 | Target language/index protocol and freshness guarantees. Acceptance: `pytest tests/test_repo_map.py tests/test_repository_graph.py tests/test_repo_intelligence_closure.py tests/test_lsp.py`; add fixture comparison once infcode-dev is available. |
| Child agents, background work, handoff, and worktrees | `implemented`: `runtime/agent/subagent.py`, `handoffs.py`, `child_result.py`, `runtime/worktree/`; tests `tests/test_subagent.py`, `tests/test_handoffs.py`, `tests/test_child_result.py`, `tests/test_agent_manager.py` | `unavailable`: no InfCodeX scheduler/worktree source available | `partial`: host surfaces expose runs, but background/handoff UX has no checked-out infcode-dev contract | P0 | Target stable child result and handoff wire schema across hosts. Acceptance: `pytest tests/test_subagent.py tests/test_handoffs.py tests/test_child_result.py tests/test_agent_manager.py`; then replay an external reference trajectory. |
| Planning, verification, and recovery | `implemented`: `runtime/agent/planning_runtime.py`, `runtime/verification/`, `runtime/agent/agent_resilience.py`; tests `tests/test_plan_mode.py`, `tests/test_verification.py`, `tests/test_recovery.py`, `tests/test_gap_recovery_phase3.py` | `partial`: translated InfCodeX contracts for stall detector, sidecar verifier, and LLM judge are present (`tests/test_stall_detector.py`, `tests/test_sidecar_verifier.py`, `tests/test_llm_judge.py`), but no upstream execution is available | `partial`: product workflow verifier exists, but no IDE review/repair host evidence | P0 | Target end-to-end evidence propagation from plan to verification and recovery. Acceptance: `pytest tests/test_plan_mode.py tests/test_verification.py tests/test_recovery.py tests/test_stall_detector.py tests/test_sidecar_verifier.py`. |
| Memory | `implemented`: `runtime/adapters/memory.py`, `runtime/conversation/context_manager.py`, `tests/test_memory.py`, `tests/test_memory_control.py`, `tests/test_scratchpad.py` | `unavailable`: no InfCodeX memory store or retention policy in checkout | `partial`: session persistence is host-visible, but product memory UX and migration contract are unverified | P1 | Target documented scope/retention and export/import contract. Acceptance: `pytest tests/test_memory.py tests/test_memory_control.py tests/test_scratchpad.py tests/test_session_revert.py`. |
| Skills, MCP, and extensions | `implemented`: `extensions/registry.py`, `mcp/runtime.py`, `mcp/config.py`, bundled skills; tests `tests/test_extensions.py`, `tests/test_mcp.py`, `tests/test_skill_loading.py`, `tests/test_skill_governance.py` | `unavailable`: no InfCodeX extension registry to compare | `partial`: registry and CLI exist, but infcode-dev marketplace/host install semantics are absent | P1 | Target portable extension manifest and trust/permission mapping. Acceptance: `pytest tests/test_extensions.py tests/test_mcp.py tests/test_skill_loading.py tests/test_skill_governance.py`. |
| Workflows | `implemented`: `runtime/workflows/workflow_runtime.py`, workflow handlers and capsules; tests `tests/test_workflow_runtime.py`, `tests/test_workflow_advanced.py`, `tests/test_workflow_parity_contracts.py`, `tests/test_workflow_capsules.py` | `partial`: parity contracts are explicitly translated from InfCodeX semantics (`tests/test_workflow_parity_contracts.py`), upstream behavior unavailable | `partial`: terminal workflow host is implemented; no infcode-dev visual/editor workflow host evidence | P1 | Target one canonical workflow state/event model for runtime and hosts. Acceptance: `pytest tests/test_workflow_runtime.py tests/test_workflow_advanced.py tests/test_workflow_parity_contracts.py tests/test_workflow_capsules.py`. |
| SDK, HTTP, and events | `implemented`: `runtime/execution/native_sdk.py`, `http_service/server.py`, `protocol/session_events.py`; tests `tests/test_http_service.py`, `tests/test_mcp_http.py`, `tests/test_openai_responses.py`, `tests/test_message_schema.py` | `unavailable`: no reference SDK schema or event stream checkout | `partial`: HTTP daemon and native SDK exist, but official infcode-dev client/stream compatibility is unverified | P1 | Target versioned public event schema and compatibility fixture. Acceptance: `pytest tests/test_http_service.py tests/test_openai_responses.py tests/test_message_schema.py tests/test_event_publisher_identity_phase3.py`; run a client fixture against `/health` and event endpoints. |
| CLI, TUI, VS Code, and JetBrains host surfaces | `implemented` for CLI/TUI: `interface/cli.py`, `interface/fullscreen.py`, `interface/run_renderer.py`; tests `tests/test_terminal_interactions.py`, `tests/test_tui_product_frames.py`, `tests/test_headless_cli.py` | `unavailable`: no InfCodeX host packages in checkout | `partial`: terminal and loopback HTTP hosts are tested; README explicitly says there is no official GUI/VS Code/JetBrains consumer | P1 | Target a host adapter contract before claiming editor parity. Files `interface/remote.py`, `runtime/execution/product_surfaces.py`, new editor adapter module. Acceptance: existing CLI/TUI tests plus a deterministic VS Code/JetBrains protocol fixture; do not mark implemented from package presence. |

## Ordered implementation backlog

1. P0: check out pinned InfCodeX and infcode-dev revisions and rerun unavailable rows using the existing reference adapter; preserve both historical benchmark snapshots separately.
2. P0: freeze the shared run/session/event, tool/permission, child-result, and verification schemas; add cross-runtime fixture tests.
3. P0: publish repository-intelligence/index freshness and provider capability matrices with negative cases.
4. P1: document memory, extension/MCP trust, workflow, and public SDK compatibility contracts.
5. P1: implement host adapters and conformance fixtures for product clients; only then assess VS Code/JetBrains parity.

## Evidence limits

The matrix distinguishes runtime parity from product-host parity. A test translated
from InfCodeX demonstrates a local contract decision, not successful execution of
InfCodeX. Likewise, a README path mentioning `infcode-dev` is not evidence that the
product checkout or its editor integrations are available.
