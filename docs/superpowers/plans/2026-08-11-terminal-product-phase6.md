# Terminal Coding Agent Product Phase 6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a scriptable Native headless CLI and unify terminal text, files, clipboard images, path drops, and direct shell interactions around existing runtime contracts.

**Architecture:** `interface/headless.py` is a product adapter over `AgentClient`; `RunOptions.on_event` projects canonical RuntimeEvents. `interface/clipboard.py` gains bounded image readers, while `TerminalInput` converts them to existing workspace attachments. Direct shell calls `ToolExecutor` and never mutates the conversation.

**Tech Stack:** Python 3.9+, asyncio, argparse, standard library, existing prompt_toolkit/Rich/OpenAI dependencies.

## Global Constraints

- Do not add AgentLoop methods or another Agent state machine.
- Machine stdout must contain only JSON or JSONL.
- All file paths remain workspace-bounded and reject symlinks.
- All production behavior follows test-first red/green cycles.
- No new dependency or Agent framework.

---

### Task 1: Native event and ephemeral-session contracts

**Files:** Modify `nz_coder/runtime/core/request.py`, `nz_coder/sdk.py`, `nz_coder/runtime/native_sdk.py`, `nz_coder/runtime/session/store.py`; test `tests/runtime/test_sdk.py`, `tests/runtime/session/test_store.py`.

**Interfaces:** Produce `RunOptions.on_event: Callable[[RuntimeEvent], object] | None` and `EphemeralSessionStore`.

- [x] Write tests for event delivery and zero durable files with no-session.
- [x] Run focused tests and observe the expected missing-contract failures.
- [x] Implement the minimal event sink and in-memory store selection.
- [x] Run focused tests to green.

### Task 2: Headless run and output contracts

**Files:** Create `nz_coder/interface/headless.py`, `nz_coder/interface/submission.py`; modify `nz_coder/interface/cli.py`; test `tests/test_headless_cli.py`.

**Interfaces:** Produce `run_main(argv, stdin, stdout, stderr, client_factory) -> int`, immutable argument validation, text/json/jsonl writers, and shared user-message construction.

- [x] Write parsing, stdin, JSON cleanliness, JSONL event, exit-code, attachment, continue/resume, and offline Model→Tool→Model tests.
- [x] Run the tests and observe failures because `run` is not registered.
- [x] Implement request construction and output projection over `AgentClient`.
- [x] Run the headless contracts to green.

### Task 3: Shell completion

**Files:** Create `nz_coder/interface/completion.py`; modify `nz_coder/interface/cli.py`; test `tests/test_shell_completion.py`.

**Interfaces:** Produce `completion_main(argv, stdout, stderr) -> int` for bash, zsh, and fish.

- [x] Write shell-specific content and invalid-shell exit tests.
- [x] Run and observe missing-command failures.
- [x] Implement deterministic offline scripts.
- [x] Run completion tests to green.

### Task 4: Clipboard images and dropped paths

**Files:** Modify `nz_coder/interface/clipboard.py`, `nz_coder/interface/terminal_input.py`, `nz_coder/interface/fullscreen.py`; test `tests/test_terminal_input.py`, `tests/test_clipboard_image.py`.

**Interfaces:** Produce `read_image() -> ClipboardImage | None`, `persist_clipboard_image(workspace, image) -> str`, and `detect_dropped_paths(text, workspace) -> tuple[str, ...]`.

- [x] Write platform helper, missing-helper, MIME, size, persistence, inline/fullscreen Ctrl+V, quoted path, outside path, and symlink tests.
- [x] Run and observe missing API failures.
- [x] Implement bounded helper execution and reuse `queue_attachment`.
- [x] Run terminal input tests to green.

### Task 5: Direct shell interaction

**Files:** Create `nz_coder/interface/direct_shell.py`; modify `nz_coder/interface/cli.py`; test `tests/test_direct_shell.py`.

**Interfaces:** Produce `execute_direct_shell(command, permission_mode, asker) -> ToolExecutionResult`.

- [x] Write success, denial, empty command, and transcript-isolation tests.
- [x] Run and observe missing API failures.
- [x] Implement via `ToolExecutor` and the registered `bash` tool.
- [x] Run direct-shell and interactive CLI contracts to green.

### Task 6: Product audit, real terminal verification, and regression

**Files:** Create `docs/terminal-product-parity-phase6.md`; modify `README.md`, `docs/infcode-alignment-learning-log.md`, this plan.

**Interfaces:** Produce a fresh 70+ item three-way product matrix, quantitative before/after report, and documented exit/output contracts.

- [x] Re-read each Cluster acceptance requirement and record evidence.
- [x] Run help/completion, piped stdin, JSON/JSONL, and PTY smoke commands with an offline Provider harness where credentials would otherwise be required.
- [x] Run focused terminal tests, complete pytest, Ruff, compileall, import-boundary, and diff checks.
- [x] Update documentation with exact evidence and mark plan boxes only after verification.
