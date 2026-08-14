# Windows EXE Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce and continuously verify a self-contained, per-user `NZ-Coder-0.1.0-windows-x64-setup.exe` that requires neither Python nor Git on the target Windows host.

**Architecture:** A small standard-library release-contract module derives versioned artifact names and validates build outputs. PyInstaller freezes the existing `python -m nz_coder` entrypoint in one-directory mode; Inno Setup wraps that directory with stable per-user install, PATH, shortcut, upgrade, and uninstall behavior. A Windows-only PowerShell smoke installs the EXE into a path containing spaces and verifies the installed product outside the checkout, while Linux tests statically guard release inputs.

**Tech Stack:** Python 3.12, PyInstaller 6.x, Inno Setup 6, PowerShell 7, pytest, GitHub Actions

## Global Constraints

- Support Windows 10/11 x86-64 and install for the current user without administrator privileges.
- Do not add PyInstaller or Inno Setup to NZ-Coder runtime dependencies.
- Do not create a second Agent runtime or change CLI, Session, provider, permission, memory, MCP, LSP, or tool semantics.
- Include pywinpty, Tree-sitter runtimes, package data, bundled commands, and bundled skills.
- Do not bundle language servers; missing optional servers remain Doctor warnings.
- Never collect or embed API keys, and never delete workspace `.env` or `.nz-coder` state on upgrade/uninstall.
- Build/test artifacts on ordinary branch and pull-request runs; publish the already-tested artifact only for matching version tags.

---

### Task 1: Versioned Installer Contract

**Files:**
- Create: `scripts/windows_installer_contract.py`
- Create: `tests/test_windows_installer_release.py`

**Interfaces:**
- Consumes: `[project].version` from repository `pyproject.toml`.
- Produces: `InstallerContract(version: str, architecture: str)`, `load_contract(root: Path) -> InstallerContract`, `artifact_name` and `app_version` properties, and `validate_frozen_tree(path: Path) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing contract tests**

```python
def test_installer_contract_uses_project_version():
    contract = load_contract(ROOT)
    assert contract.version == "0.1.0"
    assert contract.artifact_name == "NZ-Coder-0.1.0-windows-x64-setup.exe"

def test_frozen_tree_requires_entrypoint_and_bundled_assets(tmp_path):
    contract = InstallerContract("0.1.0", "x64")
    assert "nz-coder.exe" in contract.validate_frozen_tree(tmp_path)
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `pytest -q tests/test_windows_installer_release.py`

Expected: collection fails because `scripts.windows_installer_contract` does not exist.

- [ ] **Step 3: Implement the immutable release contract**

Use `tomllib` on Python 3.11+ and `tomli` only when already available is not acceptable; because project support begins at Python 3.9, parse the single project version assignment with a bounded regular expression. Reject malformed versions, unsupported architectures, missing entrypoint, absent `_internal/nz_coder/bundled_commands`, absent `_internal/nz_coder/bundled_skills`, and absent required native parser/winpty files with explicit messages.

- [ ] **Step 4: Run contract tests and lint**

Run: `pytest -q tests/test_windows_installer_release.py`

Run: `ruff check scripts/windows_installer_contract.py tests/test_windows_installer_release.py`

Expected: all pass.

- [ ] **Step 5: Commit the contract**

```bash
git add scripts/windows_installer_contract.py tests/test_windows_installer_release.py
git commit -m "build: define Windows installer contract"
```

### Task 2: PyInstaller Frozen Application

**Files:**
- Create: `packaging/windows/nz_coder_entry.py`
- Create: `packaging/windows/nz-coder.spec`
- Modify: `tests/test_windows_installer_release.py`

**Interfaces:**
- Consumes: `nz_coder.cli.main()` and package resources from `nz_coder`.
- Produces: `dist/NZ-Coder/nz-coder.exe` plus `_internal` runtime and package assets.

- [ ] **Step 1: Add failing static packaging tests**

Assert that the entrypoint calls `nz_coder.cli.main`, the spec uses one-directory `COLLECT`, names the executable `nz-coder`, collects `nz_coder` data/submodules/dynamic libraries, and requests Windows x64.

- [ ] **Step 2: Run the targeted tests and verify failure**

Run: `pytest -q tests/test_windows_installer_release.py`

Expected: failures report missing `packaging/windows/nz_coder_entry.py` and `packaging/windows/nz-coder.spec`.

- [ ] **Step 3: Add the frozen entrypoint and spec**

The entrypoint must contain only:

```python
from nz_coder.cli import main

raise SystemExit(main())
```

The spec uses `collect_all("nz_coder")`, explicitly includes metadata for
`nz-coder`, and combines collected datas, binaries, and hidden imports into
`Analysis`. Build a console executable with `EXE(..., name="nz-coder", console=True)`
and a one-directory tree with `COLLECT`.

- [ ] **Step 4: Verify static packaging contracts**

Run: `pytest -q tests/test_windows_installer_release.py`

Run: `ruff check packaging/windows/nz_coder_entry.py tests/test_windows_installer_release.py`

Expected: all pass.

- [ ] **Step 5: Commit the frozen application definition**

```bash
git add packaging/windows/nz_coder_entry.py packaging/windows/nz-coder.spec tests/test_windows_installer_release.py
git commit -m "build: freeze Windows NZ-Coder application"
```

### Task 3: Per-User Inno Setup Package

**Files:**
- Create: `packaging/windows/nz-coder.iss`
- Create: `scripts/build_windows_installer.ps1`
- Modify: `tests/test_windows_installer_release.py`

**Interfaces:**
- Consumes: `dist/NZ-Coder` and `InstallerContract.artifact_name`.
- Produces: `dist/installer/NZ-Coder-<version>-windows-x64-setup.exe` and `artifacts/windows-installer-build.json`.

- [ ] **Step 1: Add failing installer-policy tests**

Check the Inno script for `PrivilegesRequired=lowest`, `ArchitecturesAllowed=x64compatible`, a stable `AppId`, recursive frozen-tree installation, PATH task, Start Menu shortcut, and `UninstallDisplayIcon`. Check the PowerShell script for strict error handling, clean output directories, PyInstaller invocation, frozen-tree validation, ISCC invocation, SHA-256 generation, and JSON evidence.

- [ ] **Step 2: Run tests and verify the packaging inputs are missing**

Run: `pytest -q tests/test_windows_installer_release.py`

Expected: failures identify the missing `.iss` and build script.

- [ ] **Step 3: Implement the Inno setup definition**

Use preprocessor variables `AppVersion`, `SourceDir`, and `OutputDir`. Install to
`{localappdata}\Programs\NZ-Coder`, copy the complete frozen tree, create a
Start Menu terminal shortcut, and register an opt-out PATH task through the
user environment. Use `ChangesEnvironment=yes`, close applications safely,
and never declare workspace paths in uninstall sections.

- [ ] **Step 4: Implement the strict build orchestrator**

The PowerShell script accepts `-Python`, `-Iscc`, `-OutputDirectory`, and
`-EvidencePath`; checks that it runs on 64-bit Windows; installs nothing; invokes
the caller-provided Python's PyInstaller module; calls the contract validator;
invokes ISCC with explicit version/source/output defines; validates the exact
artifact; hashes it; and writes bounded JSON evidence without environment dumps.

- [ ] **Step 5: Run static release tests and PowerShell parser validation**

Run: `pytest -q tests/test_windows_installer_release.py`

Run on PowerShell: parse both scripts with `[System.Management.Automation.Language.Parser]::ParseFile(...)` and fail on parser errors.

Expected: all pass.

- [ ] **Step 6: Commit the setup package**

```bash
git add packaging/windows/nz-coder.iss scripts/build_windows_installer.ps1 tests/test_windows_installer_release.py
git commit -m "build: add per-user Windows setup package"
```

### Task 4: Installed Product Smoke and Uninstall Safety

**Files:**
- Create: `scripts/test_windows_installer.ps1`
- Modify: `tests/test_windows_installer_release.py`

**Interfaces:**
- Consumes: the exact setup artifact and a temporary root.
- Produces: `artifacts/windows-installer-smoke.json` with install, CLI, Doctor, platform, headless, asset, upgrade, uninstall, and workspace-preservation results.

- [ ] **Step 1: Add failing smoke-policy tests**

Require the smoke script to install silently into a path containing spaces,
invoke the installed executable directly from outside the checkout, parse Doctor
and platform JSON, run credential-free local commands, preserve a sentinel `.env`
and `.nz-coder` file across reinstall/uninstall, verify uninstaller success, and
write structured evidence in `finally` without secret-bearing environment output.

- [ ] **Step 2: Run tests and verify the smoke script is missing**

Run: `pytest -q tests/test_windows_installer_release.py`

Expected: failure identifies missing `scripts/test_windows_installer.ps1`.

- [ ] **Step 3: Implement install/upgrade/uninstall smoke**

Use `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR=<path>` and wait
for the setup process. Run `--help`, `--version`, `platform --json`, `doctor
--json`, `config show --json`, and a local credential-free command. Re-run the
installer to exercise upgrade. Invoke `unins000.exe` silently, then assert the
executable is gone while the separate workspace sentinel files remain.

- [ ] **Step 4: Run static tests and PowerShell parser validation**

Run: `pytest -q tests/test_windows_installer_release.py`

Expected: all pass.

- [ ] **Step 5: Commit installed-product verification**

```bash
git add scripts/test_windows_installer.ps1 tests/test_windows_installer_release.py
git commit -m "test: verify Windows setup lifecycle"
```

### Task 5: CI Distribution Gate and User Documentation

**Files:**
- Create: `.github/workflows/windows-installer.yml`
- Create: `docs/windows-install.md`
- Modify: `README.md`
- Modify: `docs/release-checklist.md`
- Modify: `tests/test_windows_installer_release.py`
- Modify: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Consumes: build and smoke PowerShell scripts.
- Produces: tested setup EXE, SHA-256 file, build/smoke JSON evidence, Actions artifact, and tag-gated GitHub Release attachment.

- [ ] **Step 1: Add failing workflow/release-document tests**

Require `windows-latest`, Python 3.12, pinned PyInstaller, Inno Setup compiler
discovery, build script, smoke script, artifact upload, and tag-gated release
publication using the same tested files. Require user docs to include install,
upgrade, uninstall, `/connect`, DeepSeek, generic OpenAI-compatible configuration,
Doctor, optional LSP boundaries, and checksum verification.

- [ ] **Step 2: Run the tests and verify workflow/docs failures**

Run: `pytest -q tests/test_windows_installer_release.py`

Expected: failures identify missing workflow and Windows install guide.

- [ ] **Step 3: Add the Windows installer workflow**

Install build-only dependencies with `python -m pip install . pyinstaller==6.16.0`.
Resolve preinstalled Inno Setup 6 or install it explicitly in the runner, build
the setup, run lifecycle smoke, create a checksum file, upload all tested files,
and publish only on `refs/tags/v*` after confirming the tag version equals
`pyproject.toml` project version.

- [ ] **Step 4: Document end-user installation and API configuration**

Document downloading the EXE, optional checksum verification, per-user install,
new-terminal PATH behavior, workspace initialization, `/connect`, `.env`
examples, Doctor interpretation, upgrade, uninstall, and retained workspace
state. Correct any old Windows-unverified wording in release docs using actual CI
evidence without claiming code signing or bundled LSPs.

- [ ] **Step 5: Run all local verification**

Run: `pytest -q tests/test_windows_installer_release.py`

Run: `ruff check scripts/windows_installer_contract.py packaging/windows/nz_coder_entry.py tests/test_windows_installer_release.py`

Run: `python -m compileall -q nz_coder scripts`

Run: `pytest -q`

Expected: all pass with only documented optional skips/warnings.

- [ ] **Step 6: Commit CI and documentation**

```bash
git add .github/workflows/windows-installer.yml docs/windows-install.md README.md docs/release-checklist.md docs/infcode-alignment-learning-log.md tests/test_windows_installer_release.py
git commit -m "ci: publish verified Windows installer artifact"
```

### Task 6: Native Windows Evidence and Closure

**Files:**
- Modify if evidence requires correction: installer files from Tasks 1-5
- Modify: `docs/windows-install.md`
- Modify: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Consumes: GitHub Actions Windows installer job logs and uploaded evidence.
- Produces: a green installer workflow for the same commit as Windows Product RC and an exact user-download handoff.

- [ ] **Step 1: Push the implementation branch and inspect the installer run**

Run: `git push origin agent/windows-product-rc`

Run: `gh run list --branch agent/windows-product-rc --workflow windows-installer.yml`

Expected: a new Windows Installer run starts for the pushed commit.

- [ ] **Step 2: Diagnose any native-only failure from complete logs and evidence**

Run: `gh run view <run-id> --log-failed`

Download the evidence artifact and fix the smallest root cause. Never weaken a
security, install, upgrade, uninstall, or workspace-preservation assertion merely
to make CI green.

- [ ] **Step 3: Repeat targeted and full verification after each correction**

Run targeted contract tests locally, push, and monitor the real Windows runner
through build, install, product smoke, upgrade, uninstall, and artifact upload.

- [ ] **Step 4: Record final evidence and verify all PR checks**

Update the learning log with the tested run URL, artifact identity, checksum
location, and honest support boundary. Run `gh pr checks 1` and require Windows
Installer, Windows Product RC, Linux sanity, and Repo Intelligence to pass.

- [ ] **Step 5: Commit and push closure documentation**

```bash
git add docs/windows-install.md docs/infcode-alignment-learning-log.md
git commit -m "docs: record verified Windows installer evidence"
git push origin agent/windows-product-rc
```
