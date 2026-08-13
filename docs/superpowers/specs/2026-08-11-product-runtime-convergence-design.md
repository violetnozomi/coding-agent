# Product Runtime Convergence Design

## Proven split

The current product has one Runner state machine but two capability graphs.
Interactive and HTTP construct `AgentLoop`, whose `ProductionRuntimeHost` binds
MCP, skills, memory, tools, background agents, interactions and events and whose
focused adapters invoke real planning, verification, snapshots and media
preflight. SDK and headless construct `_NativeEnvironment`, which duplicates
model/tool/message logic and installs no-op memory, verifier, planning, snapshot
and input services. Therefore native execution is a skeleton, not production
parity.

## Selected architecture

The mature implementation currently embedded in `AgentLoop` becomes the
canonical `ProductRunEnvironment`. `AgentLoop` remains a deprecated subclass for
explicit compatibility and tests only. This is an ownership migration, not a
renamed fake: all product composition roots instantiate the new type directly,
and the compatibility type has no product consumer.

`NativeSDKRunner` stops composing private services. For each request it builds a
full `ProductRunEnvironment` with the request's model route, Agent graph,
allowlist, permissions, interactions, event bus and Session store. It invokes
the environment's existing `AgentRunner` with the immutable `RunRequest` while
the extracted product resource scope binds MCP, skills, memory, dynamic tools,
background agents, questions, workflow, plan mode, transactions and tracing.
The environment's focused execution contexts therefore select the same real
Production services as the former legacy path.

## Surface adapters

- SDK and headless continue to call `AgentClient`; their default runner becomes
  the full product runner without importing or instantiating `AgentLoop`.
- HTTP `ManagedSession` owns protocol state, event journal and interaction
  broker, but no Agent object. Every accepted message creates a `RunRequest` and
  calls `AgentClient`; RunOptions inject the HTTP event bus and interaction ports.
- Interactive owns a `TerminalSessionController`. It builds submissions and
  RunRequests, invokes AgentClient, and exposes explicit control operations.
  Existing slash handlers retain a temporary `agent` compatibility view while
  the first status/config/session groups move to `controller`; the execution
  path never calls `build_coding_agent()` or `AgentLoop.run()`.

## Resource and lifecycle rules

The resource scope is independent of the execution state machine and accepts a
`ProductRunEnvironment`. Event buses can be caller-owned (HTTP) or environment-
owned (SDK/headless/interactive). Environment cleanup closes only owned
resources. `--no-session` still swaps only the SessionStore. Max-turns is a
context-local runtime override. Dynamic MCP changes flow through the same tool
catalog/exposure middleware on the next model turn.

## Error and compatibility policy

No native capability may silently degrade. Missing required ports fail during
composition. Interaction requests without an injected asker retain the current
permission policy's fail-closed behavior. HTTP protocol, SSE schema, replay,
snapshot, abort and CRUD routes remain unchanged. `AgentLoop`,
`build_coding_agent()` and `agent_factory=` remain explicit compatibility APIs,
not defaults.

## Verification

Tests first prove the existing split, then assert the new architecture and
feature fingerprint: real Production services, ToolExposure middleware, MCP and
skill bindings, memory recall/finalization, verification, snapshots, media
preflight, Session events and cancellation. Differential tests compare
observable calls rather than LLM prose. HTTP and interactive architecture tests
make AgentLoop construction and `build_coding_agent()` fail. Existing terminal
and HTTP contract suites must remain green, followed by real CLI/HTTP smoke,
complete pytest, Ruff, compileall, import-boundary and diff checks.

## Deferred scope

Daemon/attach receives a source-backed MVP plan only after convergence is
verified. Persistent PTY, semantic search, web search, marketplace, custom
commands and memory review UI remain deferred exactly as required.
