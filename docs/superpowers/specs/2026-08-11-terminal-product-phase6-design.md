# Terminal Coding Agent Product — Phase 6 Design

## Scope

Phase 6 implements exactly three clusters: a headless product surface, a shared
Native product runtime, and terminal input productization. Daemon/attach, PTY,
memory review UI, extension lifecycle UI, custom commands, agent picker,
specialized rendering, and semantic-index workers remain deferred.

## Alternatives considered

The headless command could simulate the interactive REPL or call the HTTP
service, but both would create different execution semantics and noisy machine
output. The selected design is a thin `argparse` adapter that builds an immutable
`RunRequest` and calls the zero-argument `AgentClient`, whose default is the
Native `AgentRunner` composition.

Clipboard images could be represented as a second terminal-only attachment
type. That is rejected. Platform helpers return bounded image bytes, which are
signature-validated and persisted beneath `.nz-coder/attachments`; the normal
workspace attachment pipeline then produces the existing FilePart representation.

Direct shell could call `subprocess.run(shell=True)`. That is rejected. `!command`
uses the existing `bash` tool through `ToolExecutor`, so command classification,
permission, cancellation-aware execution, session events, and output semantics
remain authoritative. Its output is intentionally not injected into Agent context.

## Headless command

`nz-coder run [PROMPT]` accepts stdin plus positional text, `--cwd`, provider,
model, variant/effort, permission mode, exact session, continue, resume,
no-session, max-turns, repeated file/attach arguments, and text/json/jsonl output.
Conflicting session selectors fail before model initialization. Machine modes
write only serialized records to stdout and diagnostics to stderr.

Stable exit codes are: 0 completed, 1 Agent/task failure, 2 CLI/config/input
error, 3 provider/auth failure, and 4 cancellation/interruption.

JSON is one terminal envelope containing session, status, text, usage, changed
files, and nullable error. JSONL serializes the existing `RuntimeEvent` records
and finishes with one `result` record. `RunOptions.on_event` is the narrow SDK
bridge; no second event vocabulary is introduced.

`--no-session` selects an in-memory SessionStore. Continue means the latest
Session in the selected workspace; resume means one explicit Session ID.

## Shell completion

`nz-coder completion bash|zsh|fish` generates static command and flag completion
plus the known permission modes. Provider/model values are resolved at runtime
by invoking existing list commands where the shell supports it. Completion never
contacts a Provider.

## Terminal input

Clipboard probing is optional and fail-soft. Linux supports Wayland (`wl-paste`)
and X11 (`xclip`), macOS supports the optional `pngpaste` helper, and Windows/WSL
uses PowerShell without a shell string. Missing helpers, unsupported formats,
oversized images, or clipboard races fall back to text paste or a terminal bell.

Ctrl+V first checks prompt-toolkit text, then the system text clipboard, then an
image. A valid image is saved to a deterministic, private workspace path and
queued as an ordinary `AttachedFile`. The fullscreen and inline composers invoke
the same callback.

A pasted line made entirely of existing quoted/unquoted paths is treated as a
drag/drop attachment gesture only when every path is a regular non-symlink file
inside the workspace. External paths remain ordinary text and are never read.

## Acceptance and tests

Contract tests cover parsing, stdin, clean JSON/JSONL, exit codes, session
selection, no-session persistence, completion generation, Native import
boundaries, clipboard platforms/fallbacks, MIME/size validation, path-drop
boundaries, and direct shell permission routing. A real offline Provider performs
Model→Tool→Model through the headless entry without importing `AgentLoop`.
Terminal behavior is then manually exercised in a PTY, followed by the complete
pytest, Ruff, compile, diff, and import-boundary checks.
