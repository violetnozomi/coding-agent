# Windows and Terminal UX Release Candidate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Windows an honest first-class execution target and make the existing terminal understandable to a first-time user without changing Agent Core ownership.

**Architecture:** Keep ProcessService and FullscreenComposer as the only owners, adding thin platform shell/process backends and presentation projections. Native Windows evidence comes from conditional pywinpty support plus windows-latest CI; Linux simulations validate dispatch but never create a Windows release claim.

**Tech Stack:** Python 3.9+, standard library, prompt_toolkit, Rich, conditional pywinpty on Windows, pytest, GitHub Actions.

## Global Constraints

- Freeze AgentRunner, SessionRuntime, ToolRuntime, ContextRuntime, Repo Intelligence, Verification, Memory Core, Skill Core, MCP Core, and SubAgent architecture.
- Preserve the public `bash` tool and ProcessService interfaces.
- Use `pathlib` and platform-aware path normalization; never string-prefix containment.
- Default Windows shell order is `pwsh`, `powershell.exe`, then `cmd.exe`.
- ConPTY failure must report Tier B pipe fallback.
- Status is never conveyed by color alone.
- Windows-only packages use environment markers and never install on Linux/macOS.

---

### Task 1: Windows risk map and platform contracts

**Files:**
- Create: `nz_coder/runtime/platform_runtime.py`
- Create: `tests/test_windows_platform_runtime.py`
- Create: `docs/windows-compatibility-risk-map-2026-08-13.md`
- Modify: `nz_coder/interface/platform_capabilities.py`
- Modify: `tests/test_platform_capabilities.py`

**Interfaces:**
- Produces: `ShellKind`, `ShellSpec`, `select_shell(os_name, which)`, `is_within_workspace(path, workspace, platform)` and `decode_process_output(data)`.

- [ ] Write failing tests for PowerShell/cmd/POSIX selection, Windows drive/case containment, UTF-8/UTF-16 replacement-safe decoding, and Tier A/B/C platform output.
- [ ] Run `pytest -q tests/test_windows_platform_runtime.py tests/test_platform_capabilities.py` and confirm failures identify missing contracts.
- [ ] Implement the immutable platform contracts and expand platform capability reporting with actual shell, ConPTY, process-tree, token-security, editor, clipboard, daemon, LSP, MCP, and parser tiers.
- [ ] Generate the risk map from a fresh POSIX-assumption source scan with remediation and verification status for every material module.
- [ ] Re-run the focused tests and require zero failures.

### Task 2: Explicit shell and Windows process-tree execution

**Files:**
- Modify: `nz_coder/tools/bash.py`
- Modify: `nz_coder/runtime/process_service.py`
- Create: `tests/test_windows_shell_runtime.py`
- Modify: `tests/test_process_service.py`

**Interfaces:**
- Consumes: `ShellSpec.argv(command)` and `decode_process_output()`.
- Produces: explicit shell metadata and recursive Windows termination through a backend boundary.

- [ ] Write failing tests asserting `shell=False`, exact PowerShell/cmd argv, UTF output, warning/nonzero semantics, `CREATE_NEW_PROCESS_GROUP`, and bounded `taskkill /T /F` fallback.
- [ ] Run the focused tests and confirm the old `shell=True` path fails them.
- [ ] Replace implicit shell execution with selected argv in one-shot and persistent pipe paths; retain compatibility tool name while reporting `shell_kind`.
- [ ] Keep Job Object support isolated and use taskkill only when Job binding is unavailable; never scan process names.
- [ ] Verify cancellation, timeout, exit code, and zero-orphan Linux regressions.

### Task 3: Windows ConPTY backend

**Files:**
- Create: `nz_coder/runtime/process_backends.py`
- Modify: `nz_coder/runtime/process_service.py`
- Modify: `pyproject.toml`
- Create: `tests/test_process_backends.py`

**Interfaces:**
- Produces: `ProcessBackendSession` protocol and `create_process_backend(..., tty=True)` selection.
- ConPTY session supports `read_bytes`, `write_bytes`, `resize`, `poll`, `wait`, `terminate_tree`, and `close`.

- [ ] Write failing tests with an injected winpty module covering start/read/write/resize at 80x24, 120x40, and 200x60, Ctrl+C input, status, cleanup, and pipe fallback.
- [ ] Add `pywinpty>=2.0.13; platform_system == "Windows"` without changing non-Windows installs.
- [ ] Implement a thin pywinpty adapter and refactor ProcessService records to delegate I/O/resize/cleanup while preserving its public API and event ownership.
- [ ] Run ProcessService, backend, daemon, and HTTP process tests.

### Task 4: TUI information architecture and first-use state

**Files:**
- Create: `nz_coder/interface/presentation_tokens.py`
- Modify: `nz_coder/interface/fullscreen.py`
- Modify: `nz_coder/interface/terminal_input.py`
- Modify: `nz_coder/interface/cli.py`
- Create: `tests/test_tui_product_frames.py`
- Modify: `tests/test_fullscreen.py`

**Interfaces:**
- Produces: logical frame helpers for header, empty state, activity, prompt attachments, and responsive layout.

- [ ] Write failing frame tests for empty/no-provider/running/waiting/error/remote/narrow/CJK states and keyboard-only Ctrl+K discovery.
- [ ] Implement compact header fields, explicit LOCAL/REMOTE and text status, concise empty-state guidance, focused prompt boundary, attachment chips, and grouped command discovery.
- [ ] Map runtime/tool events to Thinking/Searching/Reading/Editing/Running tests/Waiting/Verifying without exposing middleware internals.
- [ ] Validate widths below 80, 80–120, and above 120 plus Windows-safe symbols.
- [ ] Capture a real PTY first-launch transcript for the before/after UX report.

### Task 5: Tool, diff, permission, error, and Session UX

**Files:**
- Modify: `nz_coder/interface/run_renderer.py`
- Modify: `nz_coder/interface/interactions.py`
- Modify: `nz_coder/interface/timeline.py`
- Modify: `nz_coder/interface/commands/handlers/core.py`
- Modify: `tests/test_run_renderer.py`
- Modify: `tests/test_terminal_interactions.py`
- Create: `tests/test_tui_product_scenarios.py`

**Interfaces:**
- Consumes: semantic presentation tokens and canonical runtime events.
- Produces: U1–U14 logical scenario evidence.

- [ ] Write failing tests for compact/normal/detailed tool cards, bounded large diff, actionable permission/question/error/retry/verification/process cards, interrupted Session labels, fork feedback, and undo/redo previews.
- [ ] Implement semantic status tokens with textual labels, consistent cards, categorized Provider errors, bounded failure evidence, and grouped `/help`.
- [ ] Ensure no normal-mode internal policy JSON or traceback leaks.
- [ ] Run renderer, interaction, Session, CJK, width, and large-output tests.

### Task 6: Surface parity and Remote process controls

**Files:**
- Modify: `nz_coder/interface/session_controller.py`
- Modify: `nz_coder/interface/backend.py`
- Modify: `nz_coder/interface/remote.py`
- Modify: `nz_coder/http_service/client.py`
- Modify: `nz_coder/http_service/server.py`
- Modify: `nz_coder/http_service/manager.py`
- Modify: `tests/test_custom_commands.py`
- Modify: `tests/test_http_service.py`

**Interfaces:**
- Produces: per-run custom-command model override and daemon-owned process write/resize routes.

- [ ] Write failing cross-surface tests proving `command.model` enters RunRequest without global mutation and Remote can inspect model/mode/permission.
- [ ] Write failing authenticated HTTP tests for process write and resize ownership/validation.
- [ ] Implement immutable run override plumbing and Remote controls through existing owners.
- [ ] Verify Embedded, Headless, Remote, Session, process, permission, and reconnect suites.

### Task 7: Windows CI and W/U/R acceptance manifests

**Files:**
- Create: `.github/workflows/windows-product-rc.yml`
- Create: `nz_coder/evaluation/windows_product_scenarios.py`
- Create: `tests/test_windows_product_scenarios.py`
- Create: `tests/test_windows_native_smoke.py`
- Modify: `scripts/release_smoke.py`

**Interfaces:**
- Produces: W1–W15, U1–U14, and Windows/Linux R1–R12 machine-readable manifests.

- [ ] Write failing manifest tests requiring every requested scenario and executable evidence owner.
- [ ] Add windows-latest Python 3.12 install/build/doctor/headless/file/path/PowerShell/Session/process/ConPTY/daemon/attach/clipboard/LSP/MCP/TUI commands.
- [ ] Make release smoke platform-aware without importing POSIX-only modules on Windows.
- [ ] Validate workflow syntax and run the complete Linux side of the RC suite.

### Task 8: Final audit, documentation, and verification

**Files:**
- Create: `docs/windows-terminal-ux-rc-report-2026-08-13.md`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/release-checklist.md`
- Modify: `docs/terminal-product-parity-final-report-2026-08-13.md`

**Interfaces:**
- Produces: required report sections and an explicit implemented/verified/unverified status for every prompt requirement.

- [ ] Re-read the entire 2320-line prompt and map every numbered section to code, test, documentation, Windows CI, deliberate deferral, or true remaining gap.
- [ ] Record Windows Tier A/B/C without converting unexecuted CI into host evidence.
- [ ] Compare Windows, TUI, PTY, Remote, Session, rendering, permissions, models, extensions, commands, and install behavior with InfCodeX/OpenCode source.
- [ ] Run `python -m pytest -q`, Ruff, compileall, `git diff --check`, wheel/sdist release smoke, ProductScenarioSuite, Linux RC scenarios, and report-consistency checks.
- [ ] State Developer Preview, Release Candidate, or General Usable from evidence and list only real remaining product gaps.

## Execution Status (2026-08-13)

All eight implementation tasks and every checklist action above have been
executed. The unchecked boxes are retained as the original pre-execution plan;
the authoritative requirement-by-requirement result is
`docs/windows-terminal-ux-prompt-audit-2026-08-13.md`.

Local closure evidence is: full repository regression passing, source-external
wheel/sdist release smoke passing, T1–T20 product journeys passing 20/20, Ruff,
compileall, and diff checks passing. The `windows-latest` job is intentionally
the sole owner of native Windows host evidence, so Windows remains Developer
Preview until that workflow succeeds; this is an external verification gate,
not an unimplemented source task.
