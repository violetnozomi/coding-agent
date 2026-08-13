# Legacy and compatibility product-code audit

_Source-level consumer audit performed after Terminal Product Parity closure on
2026-08-13. “Legacy” in a symbol name is not treated as proof that code is dead._

## Removed because no consumer remained

| Removed seam | Evidence and replacement |
| --- | --- |
| `handle_revert_last()` | No registry, product surface, test, or import referenced it. `/undo` is the supported command and already provides the intended behavior. |
| SDK `_build_production_agent()` | No caller remained after `AgentClient` moved its default path to `build_native_sdk_runner()`. The obsolete factory duplicated composition and model-runtime ownership. |

The SDK `_tool_allowlist()` helper was initially adjacent to the dead factory,
but was retained after focused regression proved that
`runtime/native_sdk.py::build_product_run_environment()` dynamically imports it.
This is a live native composition dependency, not legacy ownership.

The controller's incomplete-environment branch was also initially classified as
dead, but full regression proved it is the public-facade seam used by injected
embedders and the CLI cancellation/recovery contract. It was restored; normal
product composition still always enters `AgentClient -> NativeSDKRunner`.

## Retained compatibility seams with live consumers

| Seam | Current consumers | Why it remains safe |
| --- | --- | --- |
| `nz_coder/cli.py` module alias | `nz_coder.__main__`, existing Python imports and compatibility smoke | It aliases `nz_coder.interface.cli`; it does not implement a second CLI. |
| `nz_coder/loop.py` module alias | Existing integrations and the mature AgentLoop regression suite | It aliases `nz_coder.runtime.loop`; it does not own another Agent loop. |
| `runtime.adapters.runner.run_request_from_legacy_host()` | `runtime.loop.AgentLoop`, `runtime.runner` compatibility entry, and `interface.session_controller` | It is the single boundary translating the mature coding facade into immutable `RunRequest`; execution still enters the native `AgentRunner`. |
| `runner_context_from_legacy_host()` and focused context/model/tool/lifecycle/memory adapters | `AgentLoop`, `AgentRunner._run_legacy()`, ToolRuntime pipeline | These bind existing owners into typed runtime ports. They do not create stores, tools, Sessions, or model clients. |
| `LegacyMessageRuntime`, `LegacyPlanningRuntime`, `LegacySnapshotRuntime` | Constructed only by `runner_context_from_legacy_host()` | They expose focused ports over the current AgentLoop owner while migration remains active; no second persistence truth is created. |
| `message_schema.legacy_messages()` | HTTP `Session.messages()`, CLI/session projections and message compatibility tests | It is a bounded outward projection from canonical structured messages, not a second message store. |
| Timeline whole-document Markdown and text fallback | Timeline rendering and loading Sessions written before structured Parts | Needed for persisted-session compatibility; new Sessions continue using structured message Parts. |
| `TerminalRunRenderer.on_tool()` callback fallback | Embedded callback path when a provider/tool emits no completed event | It renders only when the canonical event drain produced no card, preventing duplicate cards while preserving older callback integrations. |
| `TerminalSessionController.run()` public-facade fallback | Injected embedders and the CLI cancelled-turn recovery contract | Selected only when the supplied facade lacks `runtime_services`; normal product construction always uses the native Runner. |
| `evaluation.swebench_lite` shim | Existing documented invocation and external scripts | It forwards to the current evaluation package and owns no evaluation state. |

## Explicitly absent duplicate product owners

The audit found no `RemoteRuntimeV2`, `RemoteSessionStore`,
`RemoteProcessRegistry`, or `RemoteMemoryStore`. Remote Session, Process,
Workflow, interaction, Memory, command, Skill, MCP, and extension controls remain
projections over daemon/runtime owners. No unused old Remote adapter or second
renderer was found.

## Removal gate for the retained adapters

The remaining adapters can be deleted only after all three conditions hold:

1. `AgentLoop` construction itself consumes native typed ports without host
   projection;
2. persisted pre-Parts Sessions have an explicit one-way migration; and
3. the public `nz_coder.loop` and `nz_coder.cli` compatibility window is ended
   with a documented breaking release.

Removing them earlier would break live consumers without eliminating a second
runtime, because no second runtime currently exists behind these adapters.
