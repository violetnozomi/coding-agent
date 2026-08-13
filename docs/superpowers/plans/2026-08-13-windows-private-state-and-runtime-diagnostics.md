# Windows Private State and Runtime Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining source-level Windows security, decoding, and process-lifecycle diagnostic gaps without changing core runtime contracts.

**Architecture:** Add one lazy standard-library private-path security adapter, consume it only at existing persistence boundaries, extend the shared process decoder with native code-page discovery, and expose backend lifecycle mode as diagnostics. Native Windows CI remains the authority for kernel-backed evidence.

**Tech Stack:** Python 3.9+, standard library `ctypes`, pytest, GitHub Actions `windows-latest`.

## Global Constraints

- Do not redesign AgentRunner, SessionRuntime, ToolRuntime, Repo Intelligence, Verification, Memory, Skills, MCP, or ProcessService public contracts.
- Do not add external dependencies or an Agent framework.
- Preserve atomic writes, workspace containment, and secret-free diagnostics.
- Tests must fail for the missing production behavior before implementation.
- Do not represent Linux simulation as native Windows evidence.

---

### Task 1: Private path security contract

**Files:**
- Create: `nz_coder/private_paths.py`
- Test: `tests/test_private_paths.py`

**Interfaces:**
- Produces: `harden_private_path(path, os_name=None, windows_api=None) -> PrivatePathSecurity`
- Produces: `inspect_private_path(path, os_name=None, windows_api=None) -> PrivatePathSecurity`
- Produces: `windows_private_acl_available(windows_api=None) -> bool`

- [x] Write tests proving POSIX modes, injected Windows ACL success, failure
  diagnostics, and inspection behavior.
- [x] Run `pytest -q tests/test_private_paths.py` and confirm the imports fail
  because the module does not exist.
- [x] Implement the lazy Windows current-user-and-SYSTEM protected DACL adapter.
- [x] Re-run the test and retain a green result.

### Task 2: Apply private ACLs at credential and product-state boundaries

**Files:**
- Modify: `nz_coder/http_service/daemon.py`
- Modify: `nz_coder/initializer.py`
- Modify: `nz_coder/providers/connect.py`
- Modify: `nz_coder/interface/clipboard.py`
- Modify: `nz_coder/interface/terminal_input.py`
- Modify: `nz_coder/interface/preferences.py`
- Test: `tests/test_daemon.py`
- Test: `tests/test_doctor.py`
- Test: `tests/test_provider_connect.py`
- Test: `tests/test_clipboard_input.py`
- Test: `tests/test_terminal_preferences.py`

**Interfaces:**
- Consumes: `harden_private_path(...)` from Task 1.
- Produces: unchanged public file formats and command results.

- [x] Add behavior tests that inject the Windows hardener at final-path write
  boundaries and fail while callers still use only `chmod`.
- [x] Implement the minimum calls after directory creation and atomic replace.
- [x] Run the focused persistence tests.

### Task 3: Honest platform and doctor security reporting

**Files:**
- Modify: `nz_coder/interface/platform_capabilities.py`
- Modify: `nz_coder/doctor.py`
- Test: `tests/test_platform_capabilities.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes: ACL availability/inspection from Task 1.
- Produces: `token_security` Tier A only when the Windows ACL adapter is
  available; otherwise existing Tier B wording.

- [x] Add failing tests for Tier A/Tier B selection and the doctor security
  check.
- [x] Implement probe injection and actionable, secret-free output.
- [x] Run both diagnostic test modules.

### Task 4: Native Windows process decoding

**Files:**
- Modify: `nz_coder/runtime/platform_runtime.py`
- Test: `tests/test_windows_platform_runtime.py`
- Test: `tests/test_windows_native_smoke.py`

**Interfaces:**
- Preserves: `decode_process_output(data, preferred_encoding=None) -> str`.
- Produces internally: ANSI/OEM candidates from `GetACP`/`GetOEMCP`.

- [x] Add failing fixtures for UTF-16LE without BOM and injected CP936/CP932
  native candidates.
- [x] Implement conservative UTF-16 detection and Windows code-page discovery.
- [x] Run decoder tests.

### Task 5: Job Object lifecycle diagnostics and acceptance

**Files:**
- Modify: `nz_coder/runtime/process_backends.py`
- Modify: `tests/test_process_backends.py`
- Modify: `tests/test_windows_native_smoke.py`
- Modify: `.github/workflows/windows-product-rc.yml`

**Interfaces:**
- Produces: read-only backend `lifecycle_mode: str`.
- Preserves: ProcessService/ProcessHandle public serialization.

- [x] Add failing assertions for Job, taskkill fallback, and POSIX lifecycle
  modes.
- [x] Implement the property without broadening lifecycle ownership.
- [x] Require real ACL/Job/native decoder tests in the Windows workflow.

### Task 6: Audit, documentation, and release verification

**Files:**
- Modify: `docs/windows-tui-rc-closure-report-2026-08-13.md`
- Modify: `docs/windows-tui-rc-closure-prompt-audit-2026-08-13.md`
- Modify: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Produces: an honest source/evidence matrix; native Windows remains pending
  until an uploaded artifact exists.

- [x] Run focused Windows/runtime/product tests.
- [x] Run full pytest, Ruff, compileall, YAML parsing, and diff checks.
- [x] Update reports with exact fresh counts and retain Developer Preview if
  native Windows evidence is still absent.
