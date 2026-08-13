# NZ-Coder Release Baseline

> Baseline date: 2026-08-08
> Alignment checkpoint: A130
> Verification snapshot: 1141 tests passed on Python 3.12 and 3.13; 1 existing warning each

This document defines what the current NZ-Coder project supports and what it
does not claim. It is the short current-state companion to the chronological
[`infcode-alignment-learning-log.md`](infcode-alignment-learning-log.md).
Release packaging and environment evidence is tracked in
[`release-checklist.md`](release-checklist.md).

## Product Boundary

NZ-Coder is a local terminal coding Agent with an optional authenticated
loopback Session service. It is not currently a remote multi-user platform,
desktop application, IDE extension, plugin marketplace, or secure OS sandbox.

The supported entry points are:

```text
nz-coder                         terminal REPL
nz-coder init                    safe workspace .env initialization
nz-coder doctor                  offline readiness diagnostics
nz-coder serve                   loopback HTTP Session service
nz-coder mcp ...                 MCP auth/trust/status management
nz-coder models ...              model discovery, registry, and selection
nz-coder extensions ...          read-only extension metadata inspection
python -m nz_coder.swebench ...  benchmark helpers (not a result claim)
```

## Supported Core Baseline

| Area | Baseline guarantee | Main evidence |
|---|---|---|
| Terminal input | One long-lived alternate-screen Application owns a message-addressable cached/virtualized Markdown transcript, sticky-bottom multiline composer, queued requests with step-boundary follow-up takeover, original-screen permission/question/model/session overlays, and wide-screen Todo/MCP/LSP sidebar; includes configurable message-boundary navigation, selection-safe mouse detail with independent scrolling, per-ToolPart hover/expansion, private history, slash completion, categorized Ctrl+P palette, full Ctrl+X leader map including latest-answer copy, external-editor/text-paste shortcuts, one-shot safe attachments, model recent/favorites/cycle, masked Provider connect, safe non-TTY fallback, owner-aware Ctrl+C, and one-reset/error fallback | A037/A040–A048/A115–A130 |
| Terminal run view | The same terminal Application consumes Session-event-backed run settlement, bounded inert streaming text, transient pending/running ToolPart and Provider-retry progress with Bash preview/countdown, typed Assistant error/recovery cards and final identity, nested task-child current-tool projection, hidden/compact/full control-sequence-safe views, paste/attachment cards, and changed-file summary without reparsing durable history per frame | A038/A042/A048/A092/A093/A111–A113/A121–A123 |
| Session navigation | Metadata-only active/saved Session table with first-task fallback titles, parent-graph-aware user-turn timeline, stable rendered Message/Part anchors, first/last/next/previous/last-user navigation, mouse/per-turn full-detail inspection, ToolPart inline expansion, keyboard Session/turn selection, task-child picker/transcript and owned interactive follow-up, resume completion, same-workspace conversation fork through a complete turn with independent Message/Part identities, numbered titles, recursively cloned task-child state/worktrees, and confirmed physical Session/worktree deletion | A039/A040/A091/A105/A106/A110/A114/A116/A118/A126/A127 |
| Runtime isolation | Workspace, execution limits, transactions, dynamic tools, sessions, and child context use instance/ContextVar ownership rather than temporary global mutation | A014, execution-context tests |
| Safe editing | Workspace-bounded paths, transactional local writes, ChangeTracker snapshots, rollback, permissions, and effect-aware scheduling | A007, file/transaction/tool-executor tests |
| Context | Model-aware soft/hard budgets, oversized input/tool persistence, anchored compaction with durable markers, recent complete turns, persistent instructions, and durable working/session memory | A006/A015/A053/A054 |
| Code understanding | Multi-language declaration map, persistent SQLite symbol/reference index, Python exact references, and optional installed LSP definition/reference/call hierarchy/diagnostics | A001–A005/A029; partial versus InfCode outside Python/LSP-covered languages |
| Providers | OpenAI-compatible Chat Completions, OpenAI Responses/Codex, native Anthropic, native Gemini, immutable capability snapshots, variants, explicit discovery/cache/selection, models.dev-compatible overlays/pricing, Provider-reported billing priority, normalized usage cost, and typed Provider exception preservation across retries | A015/A025/A028/A032/A033/A095/A096 |
| MCP | stdio, Streamable HTTP, legacy SSE fallback, OAuth, layered config, project-command trust, tools/prompts/resources, list-changed refresh, and live reconcile | A016/A024/A026/A027/A031 |
| Session protocol | Stable Session/message/part identity including fork graph rebinding; typed User time/Agent/model/variant; explicit Assistant lineage/time/execution path/final endState; durable Agent step/tool/typed-retry state; typed finish/error/model/provider/tokens/cost with live updates; permission/question lifecycle, SSE recovery, physical deletion, local stats, and loopback HTTP client | A017–A023/A055–A065/A091/A094/A098–A105 |
| Subagents | Context/workspace isolation, foreground delegation with the same durable Message/Part step/tool/error/retry lifecycle and persistent usage/cost delta propagation, background write-task groups, path claims, cooperative cancellation, atomic terminal/result observation, snapshot conflict detection, parent review, and transactional apply | A030/A097/A107–A109; local process implementation, not a distributed task service |
| Extensions | Secret-free immutable metadata for Skills, Hooks, lazy tool packs, and MCP servers, including source/scope/trust/status/effects/lifecycle | A035; metadata unification only, not a third-party plugin runtime |
| Architecture | One core HTTP/Session/Event/Agent implementation; the unrelated Dodo/PySide parallel product was removed | A034 |
| Distribution | Non-editable wheel includes console entry points and bundled Skill; isolated source-external install resolves declared dependencies and runs credential-free help/doctor plus a real resized PTY composer/slash/multiple-command/Ctrl+C lifecycle with exactly one alternate-screen owner; missing ensurepip can use virtualenv; workspace `.env` and Provider-specific credentials support first run | A043/A113/A121/A124 |

“Supported” means the local implementation has a completed code path and
offline or loopback tests. It does not imply every third-party service has been
tested live.

## Frozen By Default

The following areas are stable foundations for the current terminal-Agent
product boundary. They should receive demonstrated bug fixes and
consumer-driven additions, not speculative breadth. “Stable” does not mean
InfCode feature parity:

- HTTP Session correctness and reconnect lifecycle;
- terminal input, selection, permission/question, and Agent-owner interaction;
- MCP core transports, auth, trust, and reconcile;
- runtime/workspace isolation;
- context budgeting and compaction;
- persistent code-index foundation;
- background write-Agent ownership and apply protocol;
- Provider capability/discovery foundation;
- unified extension metadata.

A change may unfreeze one of these areas when it fixes a demonstrated bug,
supports a real consumer, or closes verified third-party interoperability. A
larger InfCode directory by itself is not an entry condition.

## Deferred Evidence

SWE-bench remains the highest-priority missing interview/release evidence, but
evaluation execution is explicitly deferred. Historical partial batches mix
models, configurations, retries, and reporting scopes; they must not be
presented as one fixed-model, fixed-configuration, 300-instance official Lite
result.

When evaluation resumes, the minimum evidence package is:

1. fixed source snapshot and NZ-Coder configuration;
2. fixed model/provider and request settings;
3. all 300 SWE-bench Lite instance predictions;
4. official harness report and raw logs;
5. failures categorized without silently replacing first attempts;
6. a reproducible command and artifact manifest.

## Consumer-Driven Only

These are InfCode product-ecosystem differences, not unfinished terminal-Agent
core work:

- plugin package installation, compatibility negotiation, arbitrary code hooks,
  marketplace, and automatic updates;
- generated OpenAPI/JavaScript SDK and official GUI/VS Code/JetBrains hosts;
- remote workspace control plane, account sync, sharing, quotas, and cloud event
  broker;
- broad native SDK adapters for every OpenAI-compatible vendor;
- Tree-sitter/vector-store indexing and multi-backend embedding infrastructure.

Each requires a named consumer and an acceptance test before implementation.

## Known Limitations

- Interactive input, Agent streaming, command output and dialogs share one
  long-lived full-screen Application, and requests may be queued during a run.
  A queued follow-up now takes over after the current Provider/tool step settles,
  before another Provider request is sent. The waiting item remains transient
  until the CLI consumes it; it is not a crash-durable HTTP-style prompt queue.
  Message detail and ToolPart expansion are mouse-addressable, with independent
  detail scrolling, drag-activation protection and ToolPart hover. The renderer
  remains prompt_toolkit plus Rich rather than OpenTUI: it lacks OpenTUI's
  cross-line selection/copy object, and sidebar components remain fixed built-ins
  rather than third-party TUI plugin slots.
- Workspace keybinding overrides currently cover the five message-navigation
  actions. The rest of InfCode's complete leader/input/model/agent keybind schema
  remains product defaults rather than user configuration.
- Attachments support text, images, PDF, and DOCX through the current Provider
  and conversion paths. Audio/video FilePart production and Provider wire
  consumption are not implemented.
- Static exact references are strongest for Python; other languages rely on
  conservative declarations and an installed LSP.
- The CLI and loopback HTTP service share Agent/session primitives, but the CLI
  still owns `AgentLoop` directly; it is not a thin client of the HTTP API as
  InfCode's TUI is of its server runtime.
- No language server is installed automatically.
- Provider/model registry and MCP public interoperability were tested with
  isolated local protocol fixtures, not arbitrary public services.
  A130 additionally verified one configured DeepSeek terminal read-tool and
  queued-follow-up flow; this is narrow runtime evidence, not a broad Provider matrix.
- The isolated wheel and full regression suite are verified on Linux/Python
  3.12 and 3.13. Python 3.9–3.11, macOS, and Windows Terminal still require CI
  evidence before a broad public compatibility claim.
- Generic `openai-compatible` cannot infer the vendor behind a private base URL;
  named providers or a local exact catalog are required for precise metadata.
- Project Skill/Hook trust is observable metadata, not a separate approval gate;
  project-local MCP command trust is enforced.
- HTTP is loopback-only and does not provide an OS sandbox.
- Running Session capability snapshots do not hot-switch when model registry or
  selection files change.
- Cost is projected only when a bounded Provider-reported charge or explicit
  registry price is available. Public billing reconciliation, child-Agent cost
  propagation beyond foreground `task`, and Session/CLI cost statistics remain
  partially limited; `/stats` is workspace-local and background cost remains
  explicitly unattributed.
- Built-in native wire protocols are OpenAI-compatible Chat Completions,
  OpenAI Responses, Anthropic Messages, and Gemini generateContent. Other named
  vendors mostly reuse the compatible protocol rather than matching InfCode's
  provider-adapter breadth.
- MCP external writes are permissioned serial side effects but cannot join the
  local filesystem transaction.
- Source checkouts currently contain several unreferenced `*.orig` backup
  artifacts. They are not packaged in the wheel or imported at runtime, but are
  repository hygiene debt.
- Explicit Session deletion is complete, but automatic artifact retention,
  dry-run pruning, storage quotas, and orphan-worktree discovery are not.

## Verification Commands

These checks do not contact a model or public service:

```bash
python -m nz_coder --help
nz-coder doctor --json
python -m nz_coder models current
python -m nz_coder extensions list
pytest -q
python scripts/release_smoke.py
```

Commands that perform network or process actions remain explicit:

```text
nz-coder models refresh / sync   network discovery or registry request
nz-coder mcp auth                browser/loopback OAuth flow
nz-coder                         model requests after a user task
nz-coder serve                   starts a local listener
```

## Release Claim Checklist

Before updating this baseline:

- link the implementation to an A-number learning-log entry;
- record focused and complete regression results;
- distinguish local fixture validation from public live interoperability;
- state whether SWE-bench was run;
- update README and architecture claims that the change invalidates;
- keep deferred and consumer-driven work out of “supported” language.
