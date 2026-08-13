# Phase 7: Product Runtime Convergence

## Outcome

Interactive CLI, Headless CLI, Python SDK and HTTP Service now enter one
Production execution chain:

```text
                    Product surfaces

       Interactive   Headless      SDK       HTTP
            │           │           │          │
            └───────────┴───────────┴──────────┘
                            │
                        RunRequest
                            │
                       AgentClient
                            │
                    NativeSDKRunner
                            │
                 ProductRunEnvironment
                            │
             ProductionRuntimeHost resource scope
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
     Session              Model               Tools
        │                   │                   │
        └───────────────────┼───────────────────┘
                            │
     RuntimeEventMiddleware + ToolExposureMiddleware
                            │
                       AgentRunner
```

`AgentLoop` is no longer in this product flow. It is a deprecated subclass of
`ProductRunEnvironment` retained for explicit compatibility only.

## Proven split before the change

The old source had two independently composed capability graphs:

- Interactive and HTTP called `build_coding_agent()` and then `AgentLoop.run()`.
  `ProductionRuntimeHost` bound MCP, skills, memory, dynamic tools, background
  agents, questions, workflow approval, plan mode, transaction state and events.
- Headless and SDK called `NativeSDKRunner`, whose private `_NativeEnvironment`
  installed `_Memory`, `_Verifier`, `_Planning`, `_Snapshots` and `_Inputs`
  no-op implementations. It had its own model, tool, lifecycle and message code.

That made the old native path a native execution skeleton, not a Production
runtime. The duplicate implementation was deleted rather than extended.

## Production capability parity

All four surfaces declare the same contract in
`runtime/product_surfaces.py`. Surface differences are presentation and
transport choices, not silent Agent capability removal.

| Capability | Interactive | Headless | SDK | HTTP |
|---|---|---|---|---|
| MCP startup, tools and dynamic provider | Aligned | Aligned | Aligned | Aligned |
| Skills, activation and allowed-tools policy | Aligned | Aligned | Aligned | Aligned |
| Memory recall, extraction and lineage receipt | Aligned | Aligned | Aligned | Aligned |
| Tool catalog/search/progressive exposure | Aligned | Aligned | Aligned | Aligned |
| Permission port | Terminal selector | Headless policy | SDK callback/policy | HTTP broker |
| Question port | Terminal selector | Fail closed | SDK callback | HTTP broker |
| Guardrails and Agent transitions | Aligned | Aligned | Aligned | Aligned |
| Planning, replanning and plan mode | Aligned | Aligned | Aligned | Aligned |
| Reflection and completion verification | Aligned | Aligned | Aligned | Aligned |
| Snapshot, patch and ChangeTracker | Aligned | Aligned | Aligned | Aligned |
| Image/PDF/DOCX preflight | Aligned | Aligned | Aligned | Aligned |
| Background/child Agents and messaging | Aligned | Aligned | Aligned | Aligned |
| Workflow runtime | Aligned | Aligned | Aligned | Aligned |
| Context compaction and tool projection | Aligned | Aligned | Aligned | Aligned |
| Runtime state, scratchpad and lineage | Aligned | Aligned | Aligned | Aligned |
| Events, trace, usage and cancellation | Aligned | Aligned | Aligned | Aligned |
| Durable session | Yes | Optional with `--no-session` | Request option | Yes |

The interaction rows intentionally differ at the host edge. For example, an
unattended Headless run cannot open a terminal question picker; it fails closed
instead of silently inventing an answer.

## Source changes

- The mature capability owner is now `ProductRunEnvironment`; `AgentLoop` is a
  compatibility name only.
- `build_product_environment()` is the default product composition root.
- `native_sdk.py` now builds real `ProductionTurnModelRuntime`,
  `ProductionToolRuntime`, `ProductionContextManager`,
  `ProductionMemoryService`, `ProductionCompletionVerifier`,
  `ProductionRunLifecycle`, `ProductionGuardrailRuntime`,
  `ProductionInputPreflight`, `ProductionAgentTransitionRuntime` and
  `ToolExposureMiddleware`. The reduced private implementations were removed.
- `RunOptions` now carries permission, question, workflow approval and caller-
  owned event-bus ports.
- HTTP owns only session protocol state, its durable event journal and
  interaction broker. Its default path builds `RunRequest` and calls
  `AgentClient`; `agent_factory=` remains a test/compatibility seam.
- Terminal execution is owned by `TerminalSessionController`. Existing TUI,
  renderer and command UX remain intact. Run/cancel and the first status,
  trace, memory, skills, MCP, compact, diff, undo/redo and scratchpad controls
  now have explicit controller operations.
- SWE-bench and benchmark composition now instantiate
  `ProductRunEnvironment`, not `AgentLoop`.

## Observable test evidence

The new architecture tests cover:

- default SDK and Headless Model → Tool → Model without constructing AgentLoop;
- the real Production service graph plus ToolExposure middleware;
- absence of native memory/verifier/planning/snapshot/input no-op classes;
- HTTP default ownership of AgentClient rather than AgentLoop;
- Interactive execution through TerminalSessionController and AgentClient;
- a common four-surface capability fingerprint and common resource bindings;
- Headless image attachment reaching the provider's vision input;
- unchanged HTTP CRUD, SSE replay, cursor repair, snapshot, abort, permission
  and question behavior.

## Legacy remaining

| Consumer | Category | Status |
|---|---|---|
| `runtime.loop.AgentLoop` | Deprecated API | Thin compatibility subclass |
| `composition.build_coding_agent()` | Compatibility | Explicit opt-in only |
| `http_service.manager.build_http_agent()` | Compatibility/tests | Not the manager default |
| `sdk._build_production_agent()` | Compatibility | Not the AgentClient default |
| Characterization tests and `parallel_benchmark` | Tests/diagnostics | May instantiate the compatibility type |
| Old comments/type-shape adapters | Deprecated naming | No separate runtime graph |

Production product consumers: **0**. Removal should happen only after one
deprecation cycle and after downstream users stop importing `nz_coder.loop` or
passing `agent_factory=` compatibility implementations.

## Three-way terminal product matrix

This matrix distinguishes runtime depth from product UX and avoids marking a
whole subsystem missing when only its control surface is partial.

| Area | NZ-Coder | InfCodeX | infcode-dev/OpenCode |
|---|---|---|---|
| Cross-surface runtime parity | Aligned | Aligned | Aligned through server/SDK |
| Headless coding run | Aligned | Aligned | Aligned |
| Interactive TUI basics | Mostly aligned | Aligned | Aligned |
| HTTP/server session API | Mostly aligned | Aligned | Aligned |
| Daemon lifecycle | Missing | Aligned | Partial/different design |
| Remote attach | Missing | SDK/daemon transport | Aligned |
| Persistent PTY | Missing | Partial/different design | Aligned |
| Session UX | Mostly aligned | Aligned | Aligned |
| Memory backend | Aligned | Aligned | Different by design |
| Memory review UX | Partial | Aligned | Partial/different design |
| Extension registry | Mostly aligned | Aligned | Aligned |
| Extension lifecycle/install UX | Partial | Aligned | Aligned |
| Custom Markdown commands | Missing | Mostly aligned | Aligned |
| Web fetch | Aligned | Aligned | Aligned |
| Web search | Missing | Aligned | Aligned |
| Structural repo intelligence | Mostly aligned | Aligned | Aligned |
| True semantic repo search | Missing | Aligned | Aligned |
| ACP/A2A | Missing | Partial | Aligned/experimental |
| Upgrade/uninstall UX | Partial | Aligned | Aligned |

### Required nuance

- Memory is **Backend Aligned / Product Partial**. `MemoryControlPlane` already
  supports pending, approve, reject and ledger, while `/memory` still mainly
  lists memories. Pending/show/approve/reject/curate/rebuild UX remains.
- Extension registry is **Mostly aligned**: skills, hooks, tool packs and MCP
  share a projection. Lifecycle UX is partial and there is no equivalent
  plugin marketplace/install ecosystem.
- Session product is **Mostly aligned**, with new/resume/rename/delete/fork,
  undo/redo/timeline/export/copy already present. Missing items are remote
  attach, session import and a top-level automation control plane.
- TUI is not being rewritten. Its remaining gaps are backend-independent remote
  attach, persistent PTY and specialized rendering polish.

## Remaining top 10 after convergence

1. Daemon ownership/lifecycle and Remote Attach.
2. Persistent PTY sessions.
3. True semantic repository indexing and retrieval.
4. Web search with source-aware results.
5. Memory review and curation UX.
6. Extension install/update/disable lifecycle UX.
7. Custom Markdown commands.
8. First-class Agent definition/discovery UX.
9. Session import and automation control plane.
10. ACP/A2A interoperability.

The ordering follows current source gaps: remote operation is now the largest
missing product architecture layer; PTY blocks a full remote terminal; semantic
and web retrieval affect task reach; the remaining items mostly improve control
and ecosystem UX.

## Explicitly deferred

Persistent PTY, semantic search, web search, marketplace/plugin installation,
custom Markdown commands and ACP/A2A were not added in this phase. Implementing
them here would violate the phase boundary and create new product paths before
the shared runtime was verified.
