# Windows Compatibility Report

> Continuation note: the later Windows private-state/runtime-diagnostics phase
> supersedes this report's original Token ACL Tier B gap. Current source uses a
> protected current-user-and-SYSTEM DACL with native round-trip/Doctor owners;
> see `windows-tui-rc-closure-report-2026-08-13.md`. Historical counts below
> remain evidence for this earlier phase.

This phase kept Agent Core frozen and changed platform/product adapters only.
The source-level support matrix is:

| Capability | Tier | Implementation | Evidence state |
|---|---|---|---|
| Core Agent | A | Canonical `RunRequest -> ProductRunEnvironment -> AgentRunner` unchanged | Existing Linux full regression; Windows CI owner added |
| File tools | A | Workspace `_safe_path` plus drive/case/UNC contract | Linux tests and injected Windows path tests; native W4/W5 pending CI |
| PowerShell/Bash execution | A | Explicit PowerShell 7 -> Windows PowerShell -> cmd and Bash -> sh selection; `shell=False` | Unit tests and Linux Bash smoke; native W3 pending CI |
| Persistent process | A | One ProcessService with POSIX PTY, ConPTY, and pipe backends | POSIX real process tests; injected ConPTY; native W6 pending CI |
| ConPTY | A/B | pywinpty when installed; honest pipe fallback | Adapter tests; native W7–W9 pending CI |
| Daemon / Remote attach | A | Existing authenticated loopback owner and PID identity fence | Linux real daemon/attach; native W10/W11 pending CI |
| Clipboard text/image | A | `clip`/PowerShell text and PowerShell image adapters | Injected Unicode test; native W12 pending CI |
| External editor | A | prompt_toolkit editor handoff with configured/fallback probe | Component tests; native host pending CI |
| Tree-sitter | A | Default binary wheels with runtime capability probe | Linux installed; Windows wheel smoke pending CI |
| LSP | A/B | PATHEXT resolution and `.cmd/.bat/.ps1` wrappers | Unit/stdio tests; Windows CI installs and discovers Python/TypeScript/Go servers; native W14 pending CI |
| MCP | A | argv stdio, Windows wrappers, process group/tree cleanup | Protocol tests; W15 owns a real Python stdio child/tool round-trip pending native CI |
| TUI | A | One FullscreenComposer, responsive semantic frames, keyboard ownership | Real Linux PTY + frame tests; Windows host pending CI |
| Token ACL | B | Authentication and private user state remain mandatory; chmod is not presented as a Windows ACL | Explicit capability warning; ACL hardening remains |

The detailed source scan and disposition are in
`docs/windows-compatibility-risk-map-2026-08-13.md`. A successful
`windows-product-rc.yml` run is required before changing “pending CI” to native
host verified.

# TUI UX Audit Before

The pre-phase product had a persistent full-screen owner, but the actual first
screen still created these frictions:

- The transcript area was visually blank before the first message, so the user
  could not infer what to type or where files/commands lived.
- The header was a flattened provider/model string and did not state LOCAL vs
  REMOTE or IDLE/RUNNING/WAITING/ERROR.
- The footer advertised Ctrl+P while the requested primary discovery gesture was
  Ctrl+K; command discovery existed but was not the obvious entry point.
- Queued attachments were only reflected as a count, not recognizable filenames.
- Running tool state exposed tool names rather than a stable user vocabulary.
- Windows capability reporting said “pipe fallback” but had no backend that could
  become ConPTY-capable when the dependency was present.
- A real installed PTY smoke existed, but its assertion was coupled to the old
  “New request” copy instead of semantic product state.

# TUI UX Changes

- Empty sessions now show workspace, model, one suggested coding task, `/help`,
  `@` file discovery, and Ctrl+K without a long tutorial.
- Missing provider state shows one direct recovery action: `/connect`.
- The compact header states NZ-Coder, LOCAL/REMOTE, model/mode/Session when width
  permits, and a textual IDLE/RUNNING/WAITING/ERROR state.
- The prompt remains a visible `❯` boundary; attachment chips render validated
  filenames immediately above it.
- Ctrl+K and legacy Ctrl+P both open the categorized command palette. Slash and
  file completions continue to show descriptions and bounded workspace results.
- Agent activity maps to Thinking, Searching, Reading, Editing, Running tests,
  Waiting for process, and Verifying. Internal middleware names are not the
  primary explanation.
- Tool density is expressed as Compact, Normal (new default), and Detailed;
  legacy `hidden`/`full` persisted values still load.
- Provider errors are categorized and actionable; normal mode removes stack
  frames. Permission prompts include a plain-language risk reason without policy
  JSON. Large output/diffs remain bounded and summary first.
- Width bands are `<80`, `80–120`, and `>120`; status is never color-only and
  CJK filenames are covered by logical-frame tests.

# Windows Process / ConPTY

ProcessService remains the only lifecycle/event/state owner. Its records now
delegate byte I/O to three thin implementations:

1. POSIX PTY: explicit Bash/sh argv, resize via `TIOCSWINSZ`, process-group cleanup.
2. Windows ConPTY: pywinpty read/write/resize/wait/terminate adapter.
3. Pipe fallback: explicit shell argv and the same ProcessService ownership.

Windows pipe children are bound to a Job Object when the host permits it. Cleanup
falls back to bounded `taskkill /PID <pid> /T /F`; no process-name scan is used.
ConPTY receives 80x24, 120x40, and 200x60 resize contracts and passes literal
Ctrl+C bytes independently from Agent-run cancellation and process-read
cancellation. `pywinpty` is installed only under the Windows environment marker.

# Surface Parity

- Embedded custom-command `model` frontmatter now enters one immutable
  RunRequest; it does not mutate the Session environment or global model.
- Headless already used per-run model selection and remains unchanged.
- Remote sends the same per-run model through authenticated HTTP. The daemon
  resolves provider/model for that run while keeping the configured Session model.
- Remote Session info exposes provider, model, permission/mode, runtime status,
  and workspace.
- Authenticated Remote process routes now support write and resize in addition to
  list/get/read/kill. Session ownership checks remain enforced by ProcessService.

# Acceptance Suite

- W1–W15, U1–U14, and per-platform R1–R12 have machine-readable manifests in
  `nz_coder.evaluation.windows_product_scenarios`.
- The final real `ProductScenarioSuite` completed T1–T20 at 20/20. Measured
  startup was 459.769 ms, attach/reconnect were 404.045/396.572 ms, event
  duplication was 0, and orphan process count was 0. This includes isolated
  install, provider setup, interactive coding, Session recovery, process,
  daemon/Remote, custom command, Skill, MCP, Memory, extension, stress, and
  Headless JSONL journeys.
- Linux/injected Windows focused closure: 117 passed, 10 native-Windows skips.
- Final repository regression after the implementation changes: 2019 passed,
  10 native-Windows skips. The Windows-only LSP/parser/MCP/startup assertions are
  collected locally as skips and owned by CI.
- Real source-external release smoke: wheel + sdist + isolated venv + installed
  help/doctor/config/platform + daemon + one real PTY owner passed; measured cold
  startup was 394.078 ms on the final run.
- A separate warmed local measurement reported 336.190 ms median CLI startup.
  Rendering a 12,016-character Markdown result 100 times measured 99.898 ms
  median / 103.771 ms p95 per render with a 2.783 MiB tracemalloc peak. This is
  a deterministic logical-render benchmark, not a fabricated terminal FPS claim.
- Focused TUI renderer/interactions: 40 passed. LSP/MCP wrapper and protocol tests:
  57 passed. HTTP/custom command/Remote surface closure: 76 passed.
- `.github/workflows/windows-product-rc.yml` is the native evidence owner. Its
  Windows job installs basedpyright, TypeScript language server, and gopls,
  imports the default Tree-sitter wheels, and executes actual TUI startup and
  MCP stdio round-trip tests. Its result has not been fabricated from Linux
  dependency injection.

# Three-way Product Comparison

| Dimension | NZ-Coder after this phase | InfCodeX | infcode-dev/OpenCode | Assessment |
|---|---|---|---|---|
| Windows shell | Explicit PowerShell/cmd abstraction | Host/process abstraction | Runtime/platform abstraction | Comparable contract; native evidence pending |
| PTY | POSIX PTY + conditional ConPTY + pipe fallback | ConPTY-aware host paths | node/Bun PTY | Architecture aligned; emulator depth differs |
| TUI ownership | Single prompt_toolkit FullscreenComposer | Product REPL/TUI | OpenTUI | Single-owner invariant aligned |
| First use | Empty/no-provider states, Ctrl+K, visible prompt | Guided REPL | Rich onboarding/palette | Core discoverability aligned, visual polish smaller |
| Sessions | Durable timeline/fork/undo/redo/remote | SDK/session runtime | Server/TUI Sessions | Strong parity |
| Rendering | Shared Embedded/Remote semantic renderer | Runtime presentation | Tool-part components | Semantics aligned; component library smaller |
| Permissions | Scoped decisions and plain-language reason | AAMP permissions | Rules and permission UI | Core parity |
| Models | picker/history/favorites/per-run command override | provider selection | provider/model dialogs | Core parity; breadth still differs |
| Extensions | Skills/MCP/hooks/tool packs with lifecycle truth | broad capability packages | plugin system | Functional but narrower ecosystem |
| Install | Python wheel/sdist/venv | package-specific | npm/binary | Different distribution model |

This comparison was rechecked against implementation, not repository names.
InfCodeX's `packages/repl/src/tui/runtime.ts` explicitly detects Windows/SSH
ConPTY hosts and downgrades unsafe full-screen behavior; its
`tui/substrate/ink/cell-renderer.ts` records a measured ConPTY/CJK scrolling
failure and gates the DECSTBM fast path. OpenCode's
`packages/opencode/src/pty/index.ts` owns bounded PTY history, subscribers,
write, resize, and lifecycle events, while `permission/index.ts` resolves
base/saved/session rule precedence and `config/keybinds.ts` exposes Session and
model actions. NZ-Coder adopted the relevant ownership and user semantics, but
did not copy their React/OpenTUI terminal engines: its thinner
prompt_toolkit renderer is why raw nested-terminal emulation and the larger
component ecosystem remain explicit gaps.

Commercial account, billing, organization, hosted sharing, and marketplace cloud
features remain out of scope.

# Remaining Product Gaps

1. The new native Windows workflow has not yet produced a successful hosted run;
   Windows Terminal, cmd launch, VS Code integrated terminal, actual pywinpty,
   Tree-sitter wheels, daemon, and third-party LSP/MCP remain evidence gates.
2. Windows token storage is authenticated and user-private but does not yet apply
   an explicit owner-only DACL; it remains Tier B security rather than a false A.
3. Raw full-screen process attach is not implemented. Read/write/resize controls
   are complete, but NZ-Coder intentionally does not implement a nested xterm
   emulator in prompt_toolkit.
4. macOS native installation/terminal evidence remains absent.
5. Cross-host file upload remains outside local-daemon attachment semantics.

# Release Assessment

Current status: **Release Candidate on Linux; Developer Preview on native
Windows until the Windows Product RC workflow passes**.

The source implementation for this phase is complete enough to request Windows
RC evidence, and the installed Linux terminal is usable without reading a long
manual. It would be inaccurate to call Windows “General Usable” before a native
host run. The release claim should become cross-platform RC only after W1–W15 and
Windows R1–R12 pass with zero orphan processes.
