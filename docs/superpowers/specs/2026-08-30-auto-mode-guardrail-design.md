# Auto Mode Guardrail Design

Date: 2026-08-30

## Goal

Turn NZ-Coder's interactive terminal `auto` permission mode from blanket
authorization into a context-aware admission policy without adding hard
classifier refusals, changing headless/SWE behavior, or moving Provider calls
into the synchronous permission checker.

The classifier is a product guardrail, not an Agent framework feature. It must
reuse the existing tool registry, permission rules, Model Gateway, terminal
interaction bridge, transaction manager, and trace accounting.

## Product Decisions

- The classifier is eligible only for the local interactive terminal.
- Eligibility does not activate it by itself. It runs only while the current
  permission mode is `auto`.
- HTTP, SDK, headless, SWE-bench, background agents, and compatibility callers
  keep their current behavior by default, including when their permission mode
  is `auto`.
- Safe local reads, searches, an explicit safe-state allowlist, and
  workspace-local transactional edits use a deterministic fast path and make
  no classifier call.
- Bash, persistent process mutation, MCP, network access, Agent spawning,
  unknown tools, and other residual side effects go to the classifier.
- A classifier `block`, timeout, Provider failure, or malformed response opens
  an interactive `once / always / reject` decision. It never becomes an
  automatic hard refusal.
- Explicit deny rules, workspace escape, and absolute-danger rules remain hard
  denials and cannot be overridden by the classifier or an approval.
- Auto-generated `always` approval is session-scoped and bound to the exact
  canonical action. It is not persisted as a broad Bash, process, or MCP rule.
- No new dependency or Agent framework is introduced.

## Reference Alignment

The design adopts the useful common structure in InfCodeX and the recovered
Claude Code source:

1. deterministic hard safety before model judgment;
2. safe-operation fast paths before the classifier;
3. a transcript-aware classifier only for residual risk;
4. denial/error tracking with a circuit breaker;
5. separate classifier usage, latency, and cost accounting.

It intentionally differs from both references where their classifier can
directly deny or abort. NZ-Coder converts classifier risk and infrastructure
failure into a user decision because a model-generated hard refusal wastes a
tool round and conflicts with the terminal product's interaction model.

## Activation and Composition

`permission_mode` and classifier eligibility are separate controls.

- `ProductRunEnvironment` gains a boolean, default-false product capability for
  Auto classification and owns the session-scoped classifier state.
- `interface/cli.py` is the only default-on composition point. It passes the
  capability while constructing the local terminal product. The policy still
  remains dormant unless `PermissionManager.mode == "auto"`.
- Native SDK, HTTP, headless, SWE-bench, and background Agent composition do
  not pass the capability, so their existing Auto semantics and Provider-call
  counts remain unchanged.
- A global `PERMISSION_MODE` value never implies classifier eligibility.
- The rollout can be disabled by one configuration switch without rewriting
  stored permission modes or rules.

The exact composition contract is:

- `config.AUTO_MODE_CLASSIFIER_ENABLED` reads
  `NZ_AUTO_MODE_CLASSIFIER_ENABLED` and defaults to `True`;
- the local terminal constructs its environment with
  `auto_mode_classifier_enabled=config.AUTO_MODE_CLASSIFIER_ENABLED`;
- `ProductRunEnvironment` defaults `auto_mode_classifier_enabled` to `False`;
- all other product and evaluation surfaces omit the capability and therefore
  remain disabled;
- runtime activation is the conjunction of the environment capability, an
  installed async interaction adapter, and `PermissionManager.mode == "auto"`.

This explicit product capability is preferred to `isatty()` detection inside
the policy. TTY detection remains an interface concern and cannot silently
turn an SDK or piped process into an interactive authority.

## Architecture

### Deterministic router

A pure Auto admission router returns one of:

- `hard_deny`: the existing permission decision is deny, the action escapes
  the workspace, or the shell/process command matches an absolute-danger rule;
- `manual`: an explicit ask rule requires user confirmation;
- `fast_allow`: an explicit allow rule, an approved exact session action, a
  static local read/search tool, an explicitly listed safe-state tool, or a
  transactional workspace edit;
- `classify`: all residual actions.

The router reuses existing registry metadata and command-policy functions. It
does not call the Provider, render UI, persist state, or execute a tool.
`PermissionChecker` remains the final synchronous local enforcement layer and
runs again inside `ToolExecutor` immediately before dispatch.

The first release treats every Bash command, process `start`/`write`, MCP tool,
network read or mutation, Agent-spawning/cross-Agent/background/workflow
operation, and unknown side-effect class as `classify`. Unknown
`mutates-state` metadata is not a safe-state signal. Shell regexes are useful
hard-danger signals, not proof that a command is safe.

Before any fast allow or Provider call, the router canonicalizes every declared
path field for filesystem tools against the current workspace. It also parses
the target paths in multi-file patch calls. An absolute, parent-relative, or
symlink-resolved escape is `hard_deny`. Tool-level `_safe_path()` validation
still runs at dispatch as the final filesystem boundary; `ToolExecutor` itself
does not provide a generic path check for all file tools.

### Auto classifier

The runtime classifier creates a bounded, non-streaming `ModelCall` with the
new `ModelCallPurpose.AUTO_MODE`. It uses the active Provider through
`ProductionModelGateway.complete()` with `tools=()`, `streaming=False`, and
`max_retries=0`. The bounded call parameters are configuration constants:

- `AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS = 15.0`;
- `AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS = 256`;
- `AUTO_MODE_CLASSIFIER_BLOCK_STREAK = 3`;
- `AUTO_MODE_CLASSIFIER_INFRA_FAILURES = 5`;
- `AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS = 600.0`.

Classifier input contains only:

- a capped projection of the current user intent;
- the tool name;
- canonical, schema-bounded action fields;
- deterministic risk signals and the workspace-relative target when known;
- recent approval summaries required to interpret the action.

It excludes assistant reasoning, raw tool results, file contents being
written, unbounded MCP payloads, credentials, cookies, authorization headers,
and the complete trace. Embedded repository or user text is explicitly marked
as untrusted data.

The only accepted response is a strict JSON object:

```json
{
  "decision": "allow | block",
  "reason_code": "bounded_identifier",
  "reason": "short user-facing explanation"
}
```

Unknown fields may be ignored, but missing fields, an unknown decision,
multiple objects, tool calls, non-JSON output, and oversized values make the
classifier unavailable. No classifier output can produce `hard_deny`.

### Runtime integration

`ProductionGuardrailRuntime.before_tool` remains the authoritative async
boundary because it runs after model tool-call creation and before scheduler
dispatch and `ToolExecutor` permission checks.

Auto admission is a built-in product policy, not a declared `ToolGuardrail`.
The default coding terminal has no `agent_graph`, so declaration-only
guardrails do not cover it.

`ProductRunEnvironment` creates one session-owned Auto controller and exposes
it to `ProductionGuardrailRuntime`. `before_tool()` invokes that controller
independently of `_selected()` and therefore still invokes it when there is no
`agent_graph`. Declared tool guardrails are selected from
`host.current_agent_name`, while input/output selection may retain their entry
Agent semantics. The session Auto controller remains installed across declared
Agent handoffs.

The order is:

1. copy and validate the proposed tool call;
2. run the current Agent's declared tool guardrails, preserving the rule that
   they may rewrite arguments but not the tool name;
3. canonicalize the rewritten action;
4. if Auto is not eligible or mode is not `auto`, return unchanged;
5. route through deterministic `hard_deny / manual / fast_allow / classify`;
6. call the classifier only for `classify`;
7. resolve `block` or unavailable through the async terminal approval adapter;
8. return the authorized call, or a recoverable `ToolExecutionResult` with
   `executed=False` and `permission_denied=True` when the user rejects;
9. let the existing executor repeat final permission checks and each filesystem
   tool repeat its own `_safe_path()` validation.

The Auto policy must not return the generic guardrail `escalate` action.
`GuardrailEscalateError` currently terminates the run rather than pausing and
resuming a tool call.

Tool admission is sequential within a batch so that multiple classifier calls
or prompts cannot race. After admission, the existing scheduler may still run
eligible read operations concurrently and keeps writes serial.

### Async terminal approval

The current synchronous `PermissionManager.ask_user()` is designed for tool
worker threads. Calling it from the event-loop-owned `before_tool` would make
`TerminalInteractionBridge` fail closed instead of displaying the selector.

The terminal bridge therefore exposes a narrow async approval callback. Auto
admission awaits this callback directly and receives exactly `once`, `always`,
or `reject`.

- `once` authorizes only the current call.
- `always` inserts the canonical action digest into the session Auto state.
- `reject` returns a recoverable permission-denied tool result.
- Escape, cancellation, or a missing interaction adapter is `reject` and never
  dispatches the action.

The action digest covers the tool name, canonical arguments, workspace
identity, and relevant dynamic binding identity. MCP actions additionally bind
the server/tool generation when that metadata is available. Bash/process
hard-deny checks run before digest lookup and again through `PermissionChecker`
inside `ToolExecutor`; filesystem targets run before digest lookup and again
through each tool's `_safe_path()` validation during dispatch.

Existing persistent permission rules remain user-owned configuration. They are
evaluated with their existing precedence, but Auto-generated risk approvals do
not add new broad persistent rules.

## State and Degradation

Classifier health is session-scoped, never module-global. It contains:

- exact session approval digests;
- consecutive completed `block` decisions;
- monotonic timestamps of logical infrastructure failures;
- a degraded flag and transition reason.

State transitions:

- completed `allow` resets the consecutive block counter;
- completed `block` increments it;
- three consecutive `block` decisions enter degraded mode;
- timeout, Provider error, or parse failure adds one infrastructure failure;
- five infrastructure failures in a rolling ten-minute window enter degraded
  mode;
- hard denials, fast paths, and Gateway retry attempts are not infrastructure
  failures;
- a new session starts healthy.

In degraded mode, deterministic hard-deny, explicit rules, exact approvals,
and safe fast paths still apply. Every residual action goes directly to human
confirmation and creates no classifier call. Degradation is fail-interactive,
not fail-open and not fail-closed.

## Trace and Accounting

Every classifier request uses `ModelCallPurpose.AUTO_MODE`. The session Auto
context receives a narrow, observer-bound async completion callback created
from `host._gateway(max_retries=0).complete`. That Gateway retains
`host._model_gateway_observer`, which records the purpose in `RuntimeState` and
adds non-coding usage to the active `RunContext` exactly once. The Auto layer
must not manually add the same outcome again. Calls, attempts, usage, duration,
model, and cost therefore remain separately attributable while the real usage
also contributes to the total run ledger.

Each admission also emits a bounded `auto_mode_decision` trace with:

- tool name;
- decision and source (`hard_rule`, `explicit_rule`, `fast_path`,
  `classifier`, `human`, or `degraded`);
- classifier status and bounded reason code;
- latency;
- block streak and infrastructure-failure count;
- action/rule fingerprint.

The event contains no raw transcript, file content, secret, raw MCP response,
or classifier reasoning. Default-disabled product surfaces emit neither an
Auto classifier call nor Auto decision events.

## Error and Cancellation Semantics

- Invalid tool-call JSON remains a normal tool argument error and does not
  reach the classifier.
- A deterministic hard denial produces `permission_denied=True` and cannot be
  approved.
- Classifier timeout, malformed output, and client/5xx failure resolve through
  human confirmation while the run remains active. Cancellation of the owning
  run propagates after the Provider worker settles and does not open a prompt.
- User rejection is a settled tool result, not a thrown exception.
- Cancellation during classification or terminal approval dispatches no tool.
- The existing transaction rollback boundary remains authoritative after a
  write has begun; Auto admission itself performs no mutation.
- Synchronous compatibility dispatch may use the same pure router/parser, but
  production interactive classification is defined by the async pipeline. It
  must not issue a second Provider call from an already-running event loop.

## Test Strategy

Implementation follows TDD and adds focused tests before production changes.

### Router and permissions

- feature disabled and non-`auto` mode make zero classifier calls;
- explicit deny, workspace escape, and absolute-danger Bash/process precede
  explicit allow, session approval, and classifier allow;
- explicit ask routes directly to manual confirmation;
- local reads/search/state and transactional workspace edits fast-allow;
- Bash, process start/write, MCP, Agent spawning, network read/mutation, unknown
  side effects, and malformed static classification route conservatively;
- exact session approval matches only the same canonical action/workspace;
- hard-deny remains authoritative after an approval exists;
- Bash persistent prefixes cannot match a command extended with shell control
  operators.

### Classifier and state

- strict allow/block parsing and bounded reason fields;
- malformed JSON, tool-call output, timeout, client error, and owning-run
  cancellation;
- prompt projection excludes assistant reasoning, tool output, credentials,
  and raw write content and enforces size limits;
- three consecutive blocks and five failures per rolling ten minutes enter
  degraded mode at exact boundaries;
- allow reset, logical-call error counting, and new-session reset;
- degraded residual actions skip the Provider and ask the user.

### Runtime and terminal

- Auto runs after declared argument rewrite and classifies final arguments;
- default coding runtime without `agent_graph` still executes Auto admission;
- allow, block-to-once, block-to-always, block-to-reject, unavailable-to-once,
  and degraded flows;
- rejected results are recoverable and set `permission_denied=True`;
- terminal async selector pause/resume, Escape, and cancellation;
- multiple residual calls are admitted sequentially without duplicate prompts;
- `ModelCallPurpose.AUTO_MODE` usage/cost appears in runtime summaries.

### Surface compatibility

- interactive terminal capability plus mode `auto` enables classification;
- terminal default/plan/acceptEdits modes do not call it;
- HTTP, SDK, headless JSON/JSONL, SWE normal run, SWE retry, and background
  Agents remain disabled by default even with permission mode `auto`;
- existing permission interaction, settings persistence, transaction,
  cancellation, MCP, process, and full-suite tests remain green.

## Rollout and Rollback

The first release is one local-terminal slice behind a configuration switch.
HTTP/remote opt-in, a separately selected classifier model, persisted Auto
approval patterns, and cross-session degraded state are explicitly deferred.

Rollback disables terminal classifier eligibility. It does not alter the
stored permission mode, delete permission rules, rewrite sessions, or change
headless/SWE behavior. Existing traces remain readable because the new event
and model-call purpose are additive.

## Non-goals

- replacing `PermissionChecker` with an LLM;
- proving arbitrary shell commands safe through regexes;
- IDE, Web, cloud, ACP, or A2A permission products;
- remote Auto enablement in the first release;
- a general plugin or Agent framework;
- broad refactoring of the tool pipeline;
- fixing unrelated filesystem TOCTOU behavior as part of this feature.
