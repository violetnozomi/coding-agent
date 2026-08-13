# NZ-Coder

NZ-Coder is a terminal repository-level coding agent for bug fixing, feature
work, and Greenfield project creation. It is built from scratch in Python and
provides embedded, headless, SDK, authenticated loopback HTTP, daemon, and
same-machine Remote attach surfaces over one native Agent runtime.

It is intended for personal and internal use on trusted repositories. It is not
an OS sandbox, an enterprise unattended agent, or a hosted multi-tenant service.
Linux/POSIX is the release-proven path; other platform evidence and remaining
product gaps are recorded in the final parity report below.

## Architecture

```text
User Task
   ↓
Interactive TUI / Headless CLI / Python SDK / authenticated HTTP API
   ↓
RunRequest → AgentClient → NativeSDKRunner → ProductRunEnvironment
   ↓
ProductionRuntimeHost → AgentRunner + immutable Provider/Model capability snapshot
   ↓
Tools: persistent code index / Repo Map / optional LSP / MCP / web search + fetch / safe edits / bash / persistent processes
   ↓
RuntimeState + Scratchpad + Memory + background Agent Manager
   ↓
Transactions + Verification + Trace + Session Events
```

Repository repair and Greenfield creation both run through the same loop, but they differ in strategy. Repair mode starts from repo search and narrow verification. Greenfield mode starts from requirements, blueprint, scaffold, batch file writes, acceptance planning, and project verification.

The living Chinese learning log for changes made while aligning NZ-Coder with
InfCode is maintained in
[`docs/infcode-alignment-learning-log.md`](docs/infcode-alignment-learning-log.md).
The currently supported release boundary is summarized in
[`docs/release-baseline.md`](docs/release-baseline.md).
The executable packaging and compatibility gates are in
[`docs/release-checklist.md`](docs/release-checklist.md).
The four-surface runtime convergence audit and remaining product gaps are in
[`docs/product-runtime-convergence-phase7.md`](docs/product-runtime-convergence-phase7.md).
The current reader documentation is organized as:

- [`docs/quick-start.md`](docs/quick-start.md) — five-minute installation and first run
- [`docs/cli-reference.md`](docs/cli-reference.md) — terminal and automation commands
- [`docs/architecture.md`](docs/architecture.md) — architecture overview
- [`docs/remote-daemon.md`](docs/remote-daemon.md) — daemon, attach, replay, and recovery
- [`docs/process.md`](docs/process.md) — persistent process and PTY behavior
- [`docs/mcp.md`](docs/mcp.md) — MCP transports, trust, OAuth, and discovery
- [`docs/skills-and-commands.md`](docs/skills-and-commands.md) — extension concepts and lifecycle
- [`docs/memory.md`](docs/memory.md) — governed memory review
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — product diagnostics and common failures
- [`docs/terminal-product-parity-final-report-2026-08-13.md`](docs/terminal-product-parity-final-report-2026-08-13.md) — final three-way audit and release verdict


## Quickstart

Install from a checkout (editable mode is only needed for development):

```bash
python -m pip install .
# or: pipx install .

cd /path/to/your/repository
nz-coder init
# edit the generated .env, then validate without network access:
nz-coder doctor
nz-coder doctor --repo-intelligence-only
```

`nz-coder init` creates a private mode-0600 `.env` and refuses to overwrite an
existing file. Shell variables take precedence over `.env`. Provider-specific
credentials (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or `OPENAI_API_KEY`) work
without a duplicate generic `API_KEY`. After `doctor` has no FAIL rows, start:

```bash
nz-coder
```

Python AST and the TypeScript, JavaScript, and Go tree-sitter analyzers are part
of the standard installation. `doctor --repo-intelligence-only` reports the
parser tier and watcher actually active without requiring model configuration.
The embedding retrieval experiment remains optional and can be installed with
`python -m pip install '.[semantic-experiment]'`.

For scripts and CI, use the undecorated Native headless surface:

```bash
nz-coder run "inspect this repository"
cat prompt.txt | nz-coder run --output json
nz-coder run --continue "continue fixing the failing tests"
nz-coder run --resume SESSION_ID --output jsonl "try another approach"
nz-coder run --file notes.txt --attach screenshot.png "review these"
```

Install offline completion for the active shell with one of:

```bash
eval "$(nz-coder completion bash)"
eval "$(nz-coder completion zsh)"
nz-coder completion fish | source
```

For source development, use `python -m pip install -e .`. Run
`python scripts/release_smoke.py` before a release; it builds a wheel, verifies
the console entry point and bundled runtime assets, installs outside the checkout, and
runs help/doctor from a separate workspace.

Inside the REPL:

```text
/keys        # show input shortcuts
/model       # open the model picker (discovers from the active provider if needed)
/models      # alias for /model
/model list  # list offline-known models
/model PROVIDER/MODEL [VARIANT]  # switch this Session without restarting
/model-picker # choose an offline-known model with the keyboard
/model-favorite # favorite/unfavorite the active model
/model-cycle next # cycle recent models (F2)
/connect      # masked provider credential flow and model discovery
/mode        # choose default / acceptEdits / plan / auto with risk descriptions
/theme       # choose a workspace terminal theme
/tool-details full # hidden / compact / full tool cards
/mouse on    # enable mouse selection in pickers
/keybind list # inspect workspace message-navigation keys
/keybind messages_next c-n # hot-update one binding; use none/default/reset
/attach PATH # attach a workspace file to the next request
/attachments # show the one-shot attachment queue
/detach all  # clear queued attachments
/sessions    # metadata table of saved and active Sessions
/session     # choose and resume a Session with the keyboard
/delete-session ID # permanently delete one Session and its owned artifacts
/timeline    # user-turn timeline with Agent/tool summaries
/message 3   # inspect turn 3; clicking a message opens the same detail overlay
/message-next # jump to the next visible message (default Ctrl+X J)
/fork 3      # branch conversation through turn 3; workspace files stay shared
/fork-picker # choose a completed turn, then fork it
/profile     # show detected project profile
/status      # workspace and runtime status
/stats [days] # persisted Session/model/tool tokens and known cost
/diff        # latest agent-authored diff
/undo        # undo latest agent turn and file changes
/redo        # redo the most recently undone turn
```

Interactive terminals use a persistent `prompt_toolkit` editor inside a compact
inline composer. Its title shows the active provider/model, permission mode, and
estimated context usage without reserving the bottom of the terminal. Enter
submits non-empty input, Alt+Enter inserts a newline, and history is searchable
across restarts. Typing `/` at the start opens slash completion with aliases and
descriptions; Enter executes the highlighted command. `Ctrl+P` opens the
searchable, categorized command palette. `Ctrl+X M/T/E` opens the model picker,
theme picker, or external editor; F2 cycles recent models and Ctrl+V reads the
application/system text clipboard. `@` completes bounded workspace file paths
without submitting. `/session`, `/model`, `/mode`, and the fork picker reuse
async fuzzy selectors: type to filter, use Up/Down or the mouse to move, Enter
to select, and Esc to cancel. In the Session picker, pressing Ctrl+D twice on
the same row permanently deletes that Session; the explicit `/delete-session`
form requires typing the exact ID as confirmation. Model selection exposes
recent/favorite groups and provider connection. Pipes and non-TTY environments
retain the plain input fallback and picker commands report that a TTY is required.
Forks are independent top-level Sessions: NZ-Coder rekeys their complete
Message/Part reference graph and numbers titles as `(fork #1)`, `(fork #2)`,
and so on. Workspace files remain shared until an Agent explicitly edits them.
Task-child Sessions referenced by forked history are cloned as separate child
identities; write-capable children receive separate managed worktrees carrying
their recorded changed and deleted file state.
While an Agent is running, a new terminal submission is queued. The current
Provider stream and tool step settle normally, then the old turn is marked
interrupted before another model request so the queued follow-up can take over.
New Sessions use the first real user prompt as a bounded fallback title; a
manual `/rename` is never overwritten by later checkpoints.

Terminal preferences are workspace-owned in a private atomic state file.
`/connect` masks the key, writes the workspace `.env` with mode 0600, and applies
the connection to the current execution context without mutating global runtime
configuration. `/attach` accepts only regular, non-symlink workspace files and
clears the queue after one request. Large text pastes and attachments receive
compact metadata cards while their content/reference remains available to the
Agent. Vision-capable models receive supported images directly. For a text-only
active model, configure `NZ_IMAGE_DESCRIBE_MODEL` (and optionally
`NZ_IMAGE_DESCRIBE_PROVIDER`) to run a separate vision preflight and inject its
durable description into the original user turn; without it, the Agent receives
an explicit per-image failure note instead of pretending it inspected pixels.
The same fallback describes images returned by `read_file` before the next
text-only model request while retaining the original attachment in Session state.
Ctrl+V checks text first and then a native clipboard image on Linux, macOS,
Windows, and WSL. Valid images are signature/size checked, stored privately
below `.nz-coder/attachments`, and passed through the same FilePart pipeline as
`/attach` and `@file`. Pasting a line made entirely of existing workspace paths
queues those files. Prefix input with `!` to run a direct shell command through
the normal permissioned bash tool without adding its output to Agent context.

`load_skill` returns the selected instructions together with their base file URI
and a bounded sample of up to ten sibling scripts/references. This keeps relative
paths executable without dumping a whole skill directory; loading and sampling
also observe the current tool cancellation signal.
PDF and DOCX attachments are converted before the main request instead of being
decoded as binary text. DOCX uses the standard library; PDF conversion uses the
optional system `pdftotext` command. Both paths enforce a 10 MB limit and store
only bounded extracted text plus a workspace-relative, fingerprinted FilePart.
The `read_file` tool reuses that converter: use `pages="1-20"` for PDF page
ranges (maximum 20 pages per call), then `offset`/`limit` to page through the
converted text. PDFs known to exceed 20 pages require an explicit page range.
For ordinary text, `read_file` returns at most 2,000 lines and 50 KiB per call,
truncates individual lines beyond 2,000 characters, and gives an exact next
`offset`. The same tool reads one directory level with sorted, paged entries;
binary files are rejected instead of being decoded with replacement characters.

Root `AGENTS.md`, `CLAUDE.md`, and first-level workspace/global rules are loaded
on every model request under a 20 KiB per-source and 32 KiB cumulative budget.
Project rules are labelled as checked-in or private with a best-effort Git probe;
Git is not required. Rules lead the first user turn in their own
`<system-reminder>`, and requests without a user message fall back to system
context. Read-scoped nested instruction discovery is intentionally disabled to
match the current InfCode source.

Ctrl+C cancels the current input, slash command, or Agent turn and returns to the
same REPL. When a synchronous Provider or tool worker is already running,
NZ-Coder waits for that worker to settle before reporting cancellation so it
cannot leave a late file write outside the transaction rollback boundary.
Each asynchronous tool call also receives an isolated cooperative stop signal;
Bash terminates its process group and PDF Read terminates `pdfinfo`/`pdftotext`
before the Session records the tool as interrupted. Content and glob searches
observe the same signal in their subprocess, file, and directory scan loops.
Foreground and background child agents also inherit it: nested tools stop,
the child Provider client is closed best-effort, pending child edits roll back,
and the child state settles before cancellation returns to the parent.

During a run, the CLI projects the existing Session events into compact cards.
Pending and running tools occupy a transient status area with title, elapsed
time, and the latest bounded Bash output preview; completion replaces that live
state with one permanent card containing category/name, argument summary,
status, duration, bounded sanitized output, and the final changed-file count.
Provider backoff uses the same transient area to show the retry attempt,
countdown, and bounded error summary until new model/tool progress arrives.
Foreground task children use that same parent ToolPart to show their current
child tool and bounded title as a nested `↳` row. Durable Assistant errors render
once with a typed recovery hint, and the final row identifies the Agent, model,
duration, and end state.
Use `/subagents` (or `/subagents ID`) to open a read-only child Message/Part
transcript without leaving or mutating the parent Session.
Use `/subagent ID PROMPT` to continue a settled owned child in its original
tool scope and worktree without replacing the parent Agent.
`/tool-details` switches those cards between hidden, compact, and full bounded
views. Permission requests use the same fuzzy selector
for once/always/reject; structured questions support listed choices, multi-select,
custom answers, and dismissal. Blocking tool handlers run outside the terminal
event-loop thread and bridge interaction back without nesting an event loop.
Full outputs remain available through Session history/trace and `/diff`; terminal
previews never execute escape sequences from tool output.

When idle in an interactive terminal, NZ-Coder uses an alternate-screen Session
view: the bounded transcript scrolls above a fixed multiline composer, and wide
terminals automatically show a 42-column Session/workspace/change sidebar.
The transcript is rendered as Markdown after terminal control sequences are
removed; the sidebar also shows current Todo, already-running MCP, and active
workspace LSP status without starting those services merely for display.
Use `/sidebar auto|show|hide` to control it. Messages retain stable rendered
anchors: Home/End and Ctrl+X J/K/H navigate boundaries, and mouse clicks open an
independently scrollable detail overlay. Compact ToolParts can be expanded in
place without changing Session state. `/keybind` persists workspace overrides
and hot-updates the current Application. Input, Agent streaming, tool/retry
progress, command output and dialogs all remain inside one prompt_toolkit
Application; it intentionally implements OpenTUI behavior without importing its
TypeScript renderer.

If startup fails, run `nz-coder doctor` first. It performs bounded offline checks
for Python/dependencies, workspace state, active model and credential presence,
endpoint safety, permission mode, MCP configuration, installed project LSPs, and
TTY capability. `nz-coder doctor --json` is suitable for CI; the independent
`--repo-intelligence-only` probe does not initialize a model provider. `--strict` also
treats optional warnings as failures. It never prints credential values or starts
a Provider, language server, or MCP process.

Inspect or select models without starting an Agent:

```bash
nz-coder models current
nz-coder models list --details
nz-coder models refresh --provider PROVIDER_NAME  # explicit provider /models request
nz-coder models sync                        # explicit models.dev-compatible sync
nz-coder models select PROVIDER_NAME/MODEL_ID
```

Inspect extension ownership and effects without loading optional code or
starting MCP processes:

```bash
nz-coder extensions list
nz-coder extensions status tool_pack:lsp
```

MCP is disabled by default. When enabled, it supports user/project/environment
configuration, project-local command trust, stdio, Streamable HTTP with legacy
SSE fallback, OAuth, tools/prompts/resources, and live reconcile. Management is
explicit through `nz-coder mcp list|trust|untrust|auth|status|logout|smoke`; see
`.env.example` for bounded credential and configuration examples.

Live interoperability checks are opt-in and never run from normal startup:

```bash
nz-coder provider-smoke --confirm-live --json
nz-coder mcp smoke SERVER --confirm-live
```

The terminal exposes generated and saved declarative workflows through one
interactive approval gate. Generation returns inert JSON Capsules, never
model-authored executable source:

```text
/workflow list
/workflow generate inspect the routing and independently verify edge cases
/workflow run parallel-investigation inspect the routing and cite file:line evidence
/workflow show WORKFLOW_RUN_ID
/workflow pause WORKFLOW_RUN_ID
/workflow resume WORKFLOW_RUN_ID
/workflow stop WORKFLOW_RUN_ID
```

Workflow starts are asynchronous, so the composer remains usable. The durable
journal restores run identity after a process restart and explicitly fails an
orphaned active run instead of presenting stale `running` state.
These lifecycle commands are currently an Embedded-TUI surface; Remote attach
does not yet expose an equivalent workflow picker/control panel.

SWE-bench commands are also available from the main executable. The main
benchmark is Verified 500 with strict pass@1. `run-agent` excludes hints,
official test feedback, web/MCP/child-agent extension paths, commits one
attempt per instance to a resumable journal, and exports public trajectories.
Lite 300 is only a development smoke profile:

```bash
nz-coder swebench check
nz-coder swebench run-agent --profile verified --output predictions.jsonl
nz-coder swebench run-eval --predictions-path predictions.jsonl

# Development smoke only; do not report this as the main leaderboard result.
nz-coder swebench run-agent --profile lite --max-instances 10 \
  --output lite-smoke.jsonl
```

After a successful full Verified official evaluation, `run-eval` validates and
creates `submission-<run-id>/` automatically. It contains predictions,
metadata, README, manifest, inference-time trajectories, and normalized
official logs. `retry-agent` remains diagnostic and its manifest is always
submission-ineligible.

## Optional Local Session HTTP Service

NZ-Coder remains a command-line tool by default. The optional HTTP mode exposes
the same Product Runtime through `AgentClient` to local scripts and future IDE/App hosts; it does not ship
a GUI and is not a remote multi-user deployment.

Start it with a generated bearer token:

```bash
nz-coder serve
```

Or provide a stable token of at least 16 characters through the environment:

```bash
NZ_HTTP_TOKEN='replace-with-a-long-random-secret' \
  nz-coder serve --port 4096 --interaction-timeout 300 \
  --workspace /path/to/another/project
```

The service boundary is deliberately local:

- it only binds `127.0.0.1` or `localhost`;
- every route except `/health` requires `Authorization: Bearer <token>`;
- browser `Origin` requests to authenticated routes are rejected, while the
  unauthenticated `/health` probe contains no session data; the bundled client bypasses
  environment HTTP proxies so the local token is not forwarded to a proxy;
- the current directory is registered automatically; each repeated
  `--workspace PATH` lets the operator register another non-overlapping root,
  while HTTP clients select only its `workspace_id` and cannot submit a cwd;
- each session is permanently bound to one workspace; runs are serialized
  within that workspace (a second run gets HTTP 409), while the manager may run
  sessions from different, non-overlapping roots concurrently;
- saved Session histories under authorized roots are discovered after restart
  as lightweight `dormant` records and instantiate an Agent only when accessed
  or continued;
- permission and structured-question requests become authenticated pending
  records and SSE events; clients may reply/reject them, while timeout and abort
  conservatively reject instead of reading `/dev/tty`;
- events are available at `/event?session_id=<id>` with bounded in-memory replay.

The workspace registry is a control-plane boundary, not an OS filesystem
sandbox. It prevents HTTP requests from selecting an unregistered working
directory, but Bash or child processes may still reach paths allowed by the
service account. Use a least-privilege account or an external sandbox when that
distinction matters. Workspace IDs are stable selectors, not secrets; the
authenticated `/workspace` response includes the resolved path.

Use the small standard-library client:

```python
from nz_coder.http_service import NZCoderClient

client = NZCoderClient("http://127.0.0.1:4096", "replace-with-a-long-random-secret")
workspace = client.list_workspaces()[0]
session = client.create_session(
    permission_mode="acceptEdits",
    workspace_id=workspace["id"],
)
client.run(session["id"], "Inspect this repository and explain its entry points")

for event in client.events(session["id"]):
    print(event["type"], event.get("properties", {}))
    if event["type"] == "session.run.settled":
        break
```

HTTP Agents use provider streaming and publish text-part lifecycle events:

- `message.part.updated` creates an empty text part and later replaces it with a
  full snapshot;
- `message.part.delta` appends one provider text delta to that part;
- `message.part.removed` invalidates a partial part before a stream retry.

A client reducer should deduplicate ordinary events by `meta.event_id`, replace
the whole part on `updated`, append `delta` only to a live matching
`(message_id, part_id)`, and tombstone that part on `removed`. The final
`updated` remains provisional until the matching `session.message.completed`;
that completed event also contains the full text and is the fallback when part
events were filtered or discarded, but it must not revive an ID already
tombstoned by `removed`. A stream failure before its first text chunk creates no
part and therefore emits no `removed` event.

HTTP abort retires the active attempt, emits one `removed` for a started part,
and suppresses later deltas from the provider worker. Because Python cannot
forcibly stop an already-running worker thread, the Session remains busy until
that worker returns; this prevents a new run from overlapping the retired one.
Repeated abort requests are idempotent and cannot bypass that cleanup barrier.

Every ordinary Session event is sent as an SSE `id:` frame. To resume manually,
pass the last fully processed event ID; the server replays strictly after it:

```python
events = client.events(
    session["id"],
    last_event_id="the-last-complete-event-id",
    reconnect_attempts=3,
)
```

The bundled client automatically sends its newest complete frame ID as
`Last-Event-ID` after a connection failure. If that ID has fallen outside the
bounded replay tail, the server returns HTTP 410 `event_cursor_expired` instead
of silently skipping events. `server.connected` and heartbeat frames have no
ID and never advance the cursor.

On 410, discard the expired cursor and provisional parts, then fetch the idle
Session snapshot and reconnect after its cursor:

```python
snapshot = client.snapshot(session["id"])
events = client.events(
    session["id"],
    last_event_id=snapshot["cursor"]["event_id"],
)
```

`/snapshot` keeps the legacy `/messages` response unchanged while returning
structured `{info, parts}` records with persistent message/part IDs. State copy
and `session.snapshot.created` are one EventBus checkpoint, so later events are
strict-after replayable. Snapshot creation requires an idle Session; a running
Session returns 409, so the caller must wait for `session.run.settled`. It may
request abort first, but abort does not replace that wait. If the returned cursor
expires before subscription, fetch a new snapshot.

For HTTP-created Agents, the most recent event tail is also written to the
Session runtime directory and can survive a service restart. The JSONL journal
is periodically compacted from 1024 records to the current 256-event replay
tail; between compactions it may contain 256--1023 records, and a single record
has no byte-size cap. Restart loading reads at most the last 16 MiB and only
exposes the final contiguous, valid suffix for replay; a cursor before a corrupt
or missing record expires with 410. It is
best-effort local state, not a database or an authoritative interaction store:
after restart, `/permission` and `/question` pending endpoints remain the source
of truth, and active runs are not resumed.

Agent-level `session.run.completed`, `failed`, or `cancelled` events describe
execution. `session.run.settled` is the HTTP manager commit barrier: after it,
status/history are committed, persistence has been attempted, and another run
may be accepted.

When an SSE client receives `permission.asked` or `question.asked`, it can use
the request ID from the event or query the pending records:

```python
permission = client.pending_permissions(session["id"])[0]
client.reply_permission(session["id"], permission["id"], "once")

question = client.pending_questions(session["id"])[0]
client.reply_question(session["id"], question["id"], [["Selected label"]])
```

Permission replies are `once`, `always`, or `reject`. `always` adds an in-memory
rule for this Session only; it does not edit project settings. Questions also
support `reject_question()`. A disconnected event stream does not resolve a
request: reconnect and list pending records, explicitly abort, or let the
configured timeout reject it.

The local API provides `/workspace`, `/session`, `/session/{id}`, `/messages`, `/run`,
`/abort`, Session-scoped `/permission` and `/question` reply routes, `/event`,
and `/health`. User snapshot records retain the Agent, logical provider/model,
reasoning variant, and real creation time selected for that turn. Assistant
records expose normalized `finish` plus typed `error` objects, per-message
model/provider/tokens/cost, explicit parent user ID, execution mode/path,
created/completed time, and an immutable final-turn `end_state`; the same
sanitized info is emitted as `message.updated` over the Session event stream.
Retry parts retain typed Provider errors and creation time while preserving the
terminal countdown fields. Exhausted Provider failures retain normalized auth/API
identity, status, retryability, response diagnostics, and exception class/code;
credential-shaped mapping fields are redacted. Known request cost is also
projected on assistant info and StepFinish: Provider-reported OpenRouter/gateway
billing wins, otherwise an explicitly synced models.dev price is applied after
cache/reasoning normalization and the over-200K tier. Unknown pricing is omitted,
not reported as free. Foreground `task` children persist their own usage and
propagate only each invocation's cost delta, so the parent Assistant cost includes
its child while StepFinish remains scoped to the parent model step. Background
agents intentionally do not claim an old parent Assistant cost owner yet. Deleting an idle or dormant Session physically removes its saved
history, plan, owned artifacts, and recorded child-agent worktrees; an active
run must first be aborted and settled. Deletion publishes `session.deleted` to
live consumers. The service has no web UI, remote listener, database registry,
persistent pending interaction store, or remote event broker. Cursor replay is
bounded and local; there is no cross-process writer coordination or exactly-once
delivery claim.

## Optional LSP Support

NZ-Coder can use installed language servers for compiler-grade definitions,
references, hover/type information, symbols, implementations, call hierarchy,
and diagnostics. The LSP tool pack is unloaded until the agent calls
`load_optional_tools` with `{"packs": ["lsp"]}`.

Successful `write_file`, `edit_file`, `replace_lines`, `apply_patch`,
`write_files_batch`, and Python structural-edit transactions automatically
sync their changed paths to an installed language server. Diagnostics are
appended to the final write tool result. Failed transactions roll back without
publishing their temporary file state to LSP.

No language server is downloaded automatically. Common supported commands:

- Python: `ty`, `basedpyright-langserver`, `pyright-langserver`, or `pylsp`
- TypeScript/JavaScript: project-local or global `typescript-language-server`
- Go: `gopls`
- Rust: `rust-analyzer`
- C/C++: `clangd`
- Java: `jdtls`

Other discovery entries cover Kotlin, Ruby, PHP, Lua, Bash, YAML, and Dart.
Override discovery per language when necessary:

```bash
NZ_LSP_PYTHON_COMMAND="pyright-langserver --stdio"
NZ_LSP_TYPESCRIPT_COMMAND="typescript-language-server --stdio"
```

If no matching server is installed, NZ-Coder returns a clear message and keeps
the existing AST, grep, and repository-intelligence tools available.

## Multi-language Repo Map

The built-in `repo_map` fast snapshot recursively scans supported source files.
Python and Python stubs use the standard-library `ast` module. Its fallback for
TypeScript/JavaScript, Go, Rust, Java, Kotlin, C/C++, Ruby, PHP, Lua, and shell
uses conservative declaration extraction without requiring an LSP. The
persistent Repo Intelligence index separately uses the installed tree-sitter
analyzers for TypeScript, JavaScript, and Go. `repo_map` returns
classes, interfaces, types, functions, methods, variables, and constants with
source positions and compact signatures.

Unchanged files reuse a workspace-isolated SQLite symbol/reference index across
process restarts; successful file transactions incrementally replace or delete
changed entries. Excluded build/cache directories, oversized files, parse
errors, and output limits prevent context growth.
Queries use stable exact, prefix, contains, filename, path, and fuzzy ranking.

Set `semantic` to `true` to append a bounded workspace-symbol index from an
installed language server. This supplement is best-effort, so the structural
map still succeeds when no server is installed or the server is unavailable.

```bash
python - <<'PYCODE'
import nz_coder.tools.repo_map
from nz_coder.tools import dispatch
print(dispatch("repo_map", {
    "path": "infcode-dev/infcode-dev/packages/opencode/src/lsp",
    "query": "workspaceSymbol"
}))
PYCODE
```

## Durable Working Memory And Compaction

Scratchpad plans/failures and todo items are isolated by both workspace and
session, then atomically persisted under that session's runtime directory. A
process restart or `/resume` can therefore restore active working state, while
`/clear` explicitly clears both stores.

Conversation compaction produces a fixed-structure, anchored continuity
summary. Later compactions update the previous summary instead of starting
over, and the two most recent complete user turns are preserved within a
bounded tail budget.

Long-term memory remains a separate layer: durable learnings extracted from
session history are deduplicated, ranked against the current query, and
retrieved into the prompt only when relevant.

## Safe Parallel Tool Scheduling

Tools opt into one of three internal execution effects: `read`, `serial`, or
`write`. Consecutive explicit reads can run concurrently up to
`MAX_PARALLEL_TASKS` (default 4). Serial and write calls are ordered barriers,
so a read after an edit cannot start before that edit completes. Tool results
are always returned to the model in the original call order.

Unknown tools default to serial. State-changing tools such as todo/scratchpad
updates and dynamic tool loading are also serial. Bash is parallel only for the
small conservative read-only command allowlist. Write tools remain serial and
inside the existing transaction boundary. Dynamically registered tools can opt
into write classification with `execution="write"`; permission modes and read-only
subagents then treat it as a write. Rollback coverage still requires the handler
to use NZ-Coder's transaction-aware file APIs.

Run the no-model offline scheduler benchmark with:

```bash
python -m nz_coder.evaluation.parallel_benchmark \
  --tasks 6 --delay 0.05 --parallel-limit 3 --json
```

## Conservative Recovery And Doom-Loop Protection

NZ-Coder combines the exact tool name with a canonicalized argument object
before dispatch. If the same call reaches the configured consecutive threshold
without an intervening different call, that threshold-reaching call is blocked
and returned to the model as a `<doom-loop-diagnostic>`. The diagnostic
requires a different or narrower
approach, preservation of public APIs and already-passing behavior, and the
smallest evidence-backed change.

The default matches InfCode's threshold of 3, so the third call is blocked by
default. Set `NZ_DOOM_LOOP_THRESHOLD=0` to disable the guard, or use a value of
2 or greater to make the second, fourth, or another threshold-reaching call the
blocked one. Each new Agent run starts a fresh streak. Blocked
calls are traced as `doom_loop_blocked` and never reach tool dispatch.

This is a repeated-action guard, not a frozen-symbol system. NZ-Coder's broader
conservative repair behavior still comes from exact-edit diagnostics,
transactions, verification gates, and the separate SWE-bench regression guard.

## Staged Verification Pipeline

After a material write, NZ-Coder builds a machine-readable verification plan
with three ordered stages: `static`, `targeted`, and `regression`. The legacy
`recommended`, `fallback`, and `notes` planner fields remain available.

- Static commands such as `py_compile`, typecheck, `cargo check`, and Go
  compile-only checks are required when they can be inferred. Every required
  changed-file command must pass; `verify_changed_files` can satisfy the whole
  static stage as an aggregate check. A path-scoped `python_symbol_check`
  satisfies only that file, and deleted Python files are skipped rather than
  turned into impossible `py_compile` requirements.
- Exact tests observed failing before an edit become required targeted checks
  after the edit. Filename-inferred related tests remain optional so a weak
  heuristic cannot deadlock completion.
- Broad regression commands remain optional by default and are not injected by
  the completion gate. If the agent chooses to run one and it fails, the failure
  remains blocking until that check passes or a new edit starts a fresh plan.

The pipeline recommends and tracks commands; it does not execute tests in the
background. `AgentLoop.last_status` exposes an additive
`verification_pipeline` snapshot. Gate prompts list required checks that have
not passed and optional checks that were already run and failed; unrun optional
checks are not suggested. Ordinary bash verification is also recorded in
`RunEvidence` with its stage; equivalent commands are normalized across common
runner wrappers and presentation flags, so a successful rerun replaces its
previous result. This classifier recognizes verification evidence only; it is
not a general shell-mutation or formatter-blocking engine.

## Structured User Questions

The main agent can call `question` when a material user-owned decision cannot
be resolved from the request, repository, or a sensible default. One call can
contain 1-4 questions; each question provides 2-5 options and can allow either
single or multiple selection. Terminal users may select option numbers or type
a custom answer.

The question service is bound to the current `AgentLoop` run and executes as a
serial barrier. The CLI pauses its streaming renderer while reading the answer
and always resumes it afterward. Headless runs never read stdin: without an
explicit `question_asker` adapter, the tool returns an immediate error so local
evaluation and API clients cannot hang. Child agents do not receive this tool;
they relay clarification needs to the parent agent.

## Demo Commands

Inspect project profile without calling a model:

```bash
python - <<'PYCODE'
import nz_coder.project_profile
from nz_coder.tools import dispatch
print(dispatch('project_profile', {'save': False}))
PYCODE
```

Plan verification for current changes:

```bash
python - <<'PYCODE'
import nz_coder.verification_planner
from nz_coder.tools import dispatch
print(dispatch('plan_verification', {}))
PYCODE
```

Analyze current patch risk:

```bash
python - <<'PYCODE'
import nz_coder.impact_analyzer
from nz_coder.tools import dispatch
print(dispatch('analyze_impact', {}))
PYCODE
```

## Greenfield Project Mode

NZ-Coder supports both:
1. Repository repair mode
2. Greenfield project creation mode for small local prototypes

Greenfield flow:

```text
User requirement
  ↓
analyze_project_requirements
  ↓
create_project_blueprint
  ↓
scaffold_project
  ↓
inspect_generated_project
  ↓
check_project_completeness
  ↓
write_files_batch (only if scaffold gaps remain)
  ↓
plan_project_acceptance
  ↓
verify_project_build
  ↓
Runnable project + README
```

FastAPI Todo API demo prompt:

```text
创建一个名为 todo_api 的 FastAPI Todo API 项目，支持 CRUD、pytest 测试和 README
```

What the default FastAPI scaffold provides:
- in-memory Todo CRUD API
- pytest coverage for create/list/get/update/delete
- README quickstart
- low-noise verification commands
- Swagger UI at `http://localhost:8000/docs` after `uvicorn` starts
- generated-project inspection and completeness checks for the demo scaffold

FastAPI scaffold note:
- the generated FastAPI demo currently targets Python 3.10+

What it intentionally does not do by default:
- cloud deployment
- automatic dependency installation
- overwrite existing files
- SQLite persistence in the default template

After scaffolding, the recommended quality loop is:

```text
inspect_generated_project
  ↓
check_project_completeness
  ↓
plan_project_acceptance
  ↓
verify_project_build
```

## RunEvidence

NZ-Coder records structured evidence during each run, including generated files, expected files, staged verification results, impact/completeness reviews, limitations, and tool failures. Bash tests, changed-file checks, symbol checks, and project-build checks share `static` / `targeted` / `regression` stage labels. The latest result for the same stage and command replaces stale failure evidence. In the current MVP RunEvidence itself is observational and does not change AgentLoop control flow.
review_run_evidence is available as a read-only tool to summarize whether current evidence is sufficient, missing, or limited. It does not currently block finalization.

## Evaluation

Dry-run eval for repository repair tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/eval_tasks --limit 3 --mode dry-run
```

Live eval for repository repair tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/eval_tasks --limit 3 --mode live
```

Dry-run eval for Greenfield project creation tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/project_creation_tasks --limit 3 --mode dry-run
```

Dry-run only validates task loading and result writing. Live mode is required to actually create project files and satisfy `expected_files` checks.

Live eval for Greenfield project creation tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/project_creation_tasks --limit 3 --mode live
```

If `--mode auto` is used, the runner chooses live mode only when `API_KEY` is configured; otherwise it falls back to dry-run.

Results are written to:

```text
eval/results/<timestamp>.json
eval/results/<timestamp>.md
```

See [EVAL.md](EVAL.md) for setup, metrics, and example result format.

## Safety Notes

- Permission-based command safety is enforced before tool execution.
- Dangerous shell commands are blocked by policy.
- Package installs are blocked by default for benchmark repair and project generation.
- File edits through NZ-Coder tools are tracked and can be reverted.
- Greenfield scaffolding does not overwrite existing files unless `overwrite=True` is explicitly used.
- There is no OS-level sandbox, Docker isolation, or VM boundary yet.
- NZ-Coder is intended for local trusted repositories; verification depends on each repo's local tests and typecheck commands.

## Limitations

- Python static references/callers are strongest. Other supported languages use
  conservative declaration extraction plus installed LSP definition/reference/
  call-hierarchy support. TypeScript, JavaScript, and Go tree-sitter analyzers
  are standard dependencies; semantic embeddings/vector retrieval remain optional.
- Greenfield mode is meant for small local projects, not large product-grade systems.
- The default FastAPI template uses in-memory storage, even when the prompt mentions SQLite.
- The agent does not do deployment, cloud provisioning, or long-horizon autonomous project management.
- Evaluation harness is local and heuristic, not a secure multi-tenant benchmark service.
- The loopback HTTP service is not a remote multi-user control plane and has no
  official GUI/VS Code/JetBrains consumer.
- Public Provider, models.dev, and third-party MCP interoperability have not
  been claimed without explicit live tests and user-owned credentials.
- Historical Lite batches are not a strict Verified 500 result. Only a complete
  Verified run whose submission validator passes may be reported as the main
  pass@1 score.
