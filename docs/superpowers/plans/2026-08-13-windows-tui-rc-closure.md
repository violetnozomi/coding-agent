# Windows + TUI RC Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Windows correctness and terminal usability risks without changing Agent Core ownership or adding product breadth.

**Architecture:** Keep platform_runtime, ProcessService, FullscreenComposer, and the command registry as the existing owners. Add byte-decoding, Job Object, terminal-column, Remote-boundary, and evidence contracts at those owners, then collect real Linux artifacts and delegate native Windows proof to windows-latest.

**Tech Stack:** Python 3.9+, standard library, prompt_toolkit, Rich, conditional pywinpty, pytest, GitHub Actions.

## Global Constraints

- Freeze AgentRunner, SessionRuntime, ToolRuntime, Repo Intelligence, Verification, Memory, Skills, MCP public behavior, ProcessService public contract, and SubAgent architecture.
- Do not add an Agent framework, terminal emulator, plugin marketplace, cloud feature, or semantic production index.
- Keep public tool names and payloads compatible.
- Use TDD for every production behavior change.
- Never represent Linux simulation as native Windows evidence.
- Preserve unrelated dirty-worktree changes; do not commit or create a worktree in this shared workspace.

---

### Task 1: Unified process decoding and workspace boundary

**Files:**
- Modify: `nz_coder/runtime/platform_runtime.py`
- Modify: `nz_coder/tools/bash.py`
- Modify: `nz_coder/runtime/process_service.py`
- Modify: `nz_coder/config.py`
- Modify: `.env.example`
- Test: `tests/test_windows_platform_runtime.py`
- Test: `tests/test_windows_shell_runtime.py`
- Test: `tests/test_process_service.py`
- Test: `tests/test_files.py`

**Interfaces:**
- Produces: `decode_process_output(data, preferred_encoding=None) -> str` and resolved `is_within_workspace(...) -> bool`.
- Consumes: `NZ_PROCESS_OUTPUT_ENCODING` as an optional legacy-codepage hint.

- [ ] Add literal UTF-8 Chinese/Japanese/emoji, UTF-16 BOM, GBK, malformed-byte, one-shot shell, split process-read, symlink, and new-child-parent boundary tests.
- [ ] Run the focused tests and confirm fixed UTF-8 shell decoding and boundary behavior fail for the intended reason.
- [ ] Switch one-shot shell stdout to bytes and decode only through `decode_process_output`; pass the same configured preference to ProcessService reads.
- [ ] Add host-native resolved containment after the platform lexical check, including the nearest existing parent for new paths.
- [ ] Re-run focused shell/process/file tests to green.

### Task 2: Windows Job Object and crash cleanup

**Files:**
- Modify: `nz_coder/runtime/process_backends.py`
- Create: `tests/test_windows_crash_cleanup.py`
- Modify: `tests/test_process_backends.py`
- Modify: `tests/test_windows_native_smoke.py`

**Interfaces:**
- Produces: `_WindowsJob.bind(pid)` configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` before PID assignment.
- Consumes: exact owned child PIDs from Pipe and ConPTY creation.

- [ ] Add an injected kernel32 test that fails until the Job limit flag is configured before assignment and handles close on every failure branch.
- [ ] Add ConPTY tests that fail until its owned PID is Job-bound and taskkill remains the exact-PID fallback.
- [ ] Implement the ctypes Job information structure and attach one Job to each Windows Pipe/ConPTY backend.
- [ ] Add native WC1 normal kill, WC2 Session cleanup, WC3 daemon shutdown, WC4 abrupt owner exit, and WC5 Node child/grandchild checks using condition-based waits.
- [ ] Run injected backend tests locally; leave native tests collected as explicit Linux skips.

### Task 3: Terminal-column correctness and identity UX

**Files:**
- Modify: `nz_coder/interface/presentation_tokens.py`
- Modify: `nz_coder/interface/terminal_input.py`
- Modify: `nz_coder/interface/cli.py`
- Modify: `nz_coder/interface/run_renderer.py`
- Test: `tests/test_tui_product_frames.py`
- Test: `tests/test_terminal_input.py`
- Test: `tests/test_run_renderer.py`

**Interfaces:**
- Produces: `terminal_text_width(text) -> int`, `clip_terminal_text(text, max_columns) -> str`, display-safe header/status/card labels.
- Consumes: `session_title`, `session`, `workspace`, `location`, and attachment path metadata from the existing terminal state.

- [ ] Add failing ASCII/CJK/emoji/combining/ZWJ clipping tests with hand-derived column limits.
- [ ] Add failing header tests for title plus short ID, workspace basename, mandatory location/status, and narrow priority.
- [ ] Add failing attachment tests for user basename, workspace-relative source, and hidden clipboard cache identity.
- [ ] Implement grapheme-safe terminal-column clipping and route header, compact status, attachment chips, activity subjects, composer title, tool subjects, Session title, and process title through it.
- [ ] Keep full paths in detail/status output and run width/CJK/renderer tests to green.

### Task 4: Command, help, error, and Remote UX closure

**Files:**
- Modify: `nz_coder/interface/commands/registry.py`
- Modify: `nz_coder/interface/commands/handlers/core.py`
- Modify: `nz_coder/interface/terminal_input.py`
- Modify: `nz_coder/interface/remote.py`
- Modify: `nz_coder/runtime/process_service.py`
- Test: `tests/test_cli_commands.py`
- Test: `tests/test_terminal_input.py`
- Test: `tests/test_tui_product_scenarios.py`
- Test: `tests/test_terminal_backend.py`

**Interfaces:**
- Produces: ordered palette commands, essentials-only `/help`, `/help all`, explicit LOCAL DAEMON/REMOTE identity, rejected Remote direct shell/client attachments, and actionable ConPTY fallback output.

- [ ] Add failing palette-order/help-scope tests and verify the current registration-order/command-wall behavior.
- [ ] Add failing Remote tests proving `!command` cannot execute locally and explicit remote URLs reject client path attachments.
- [ ] Add a failing ProcessService fallback test requiring the actionable pywinpty guidance.
- [ ] Implement the smallest registry ordering, help filtering, Remote boundary, and fallback copy changes.
- [ ] Re-run terminal/Remote/process tests and retain logs/write/resize/kill instead of unstable raw attach.

### Task 5: Executable acceptance artifacts and native CI

**Files:**
- Create: `nz_coder/evaluation/release_acceptance.py`
- Create: `scripts/run_release_acceptance.py`
- Create: `tests/test_release_acceptance.py`
- Modify: `nz_coder/evaluation/windows_product_scenarios.py`
- Modify: `.github/workflows/windows-product-rc.yml`

**Interfaces:**
- Produces JSON schema containing suite, platform, environment, Python version, package version, scenario, result, duration, failure, command, and bounded output.

- [ ] Add a failing real-runner test using two executable commands, one passing and one failing, and assert complete per-scenario metadata.
- [ ] Implement the runner without shell invocation and with bounded timeout/output.
- [ ] Add WC1–WC5 to the machine-readable Windows manifest.
- [ ] Make Windows CI build artifacts, run W1–W15/WC1–WC5 through the runner, test both PowerShell families when present, and upload JSON/log artifacts even on failure.
- [ ] Make Linux CI run T1–T20 and U1–U14 artifacts; keep fresh wheel/sdist smoke as a separate gate.

### Task 6: Local evidence, performance, and gap classification

**Files:**
- Create: `docs/evidence/windows-tui-rc-closure/`
- Create: `docs/windows-tui-source-gap-audit-2026-08-13.md`
- Modify: `tests/test_terminal_product_stress.py` only if a measured contract is missing.

**Interfaces:**
- Produces durable Linux T/U acceptance JSON, performance JSON, package smoke output, and categorized TODO/FIXME/unavailable findings.

- [ ] Run T1–T20 into a repository evidence artifact.
- [ ] Run U1–U14 into a repository evidence artifact.
- [ ] Measure cold/warm startup, typing mutation latency, streaming projection, 100K/1M output, large transcript, 10K completion, and 1K Session listing into JSON.
- [ ] Scan TODO/FIXME/NotImplemented/pass/restart_required/unsupported/unavailable and classify each material product finding as bug, honest limitation, or future enhancement.
- [ ] Run source-external wheel/sdist/fresh-install release smoke and preserve bounded output.

### Task 7: Final matrices, usability walkthrough, demo, and re-audit

**Files:**
- Create: `docs/windows-tui-rc-closure-report-2026-08-13.md`
- Create: `docs/windows-tui-rc-closure-prompt-audit-2026-08-13.md`
- Create: `docs/interview-demo-journey.md`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/release-checklist.md`

**Interfaces:**
- Produces the ten required report headings, Windows Tier matrix, TUI Good/Acceptable/Needs-work matrix, scripted five-minute walkthrough metrics, P0/P1/P2 gaps, and exact Release Decision.

- [ ] Re-read all 62 prompt sections and map each to source, test, artifact, deliberate boundary, native gate, or remaining gap.
- [ ] Run a strict scripted new-user walkthrough and label it scripted rather than human evidence; record documentation lookups, failures, and lost-navigation count.
- [ ] Compare exact InfCodeX and OpenCode process/TUI/Session/permission sources, not filenames.
- [ ] Write the 3–5 minute interview demo using only production commands.
- [ ] Run full pytest, Ruff, compileall, diff check, release smoke, document consistency, and artifact schema checks.
- [ ] Select Developer Preview, Release Candidate, or General Usable from the actual native Windows/Linux evidence and list only real P0/P1/P2 gaps.

## Execution mode

Inline execution was selected by the user's instruction to proceed without
questions. The repository is on `main` with a heavily dirty shared worktree;
worktree creation and commits are deliberately omitted to preserve the user's
existing changes.
