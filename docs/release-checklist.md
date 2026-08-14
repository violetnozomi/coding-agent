# NZ-Coder v0.1 Release Checklist

The Windows distribution gate is `.github/workflows/windows-installer.yml`. It
must build the PyInstaller one-directory runtime, compile the Inno Setup EXE,
install into a path containing spaces, exercise the installed product, reinstall
as an upgrade, uninstall, preserve workspace sentinels, and upload the tested EXE
with SHA-256 and JSON evidence. See `docs/windows-install.md`.

This checklist separates locally proven release behavior from evidence that
requires another operating system or user-owned third-party credentials.

## Required local gate

Run from the source checkout:

```bash
python -m pip install -e ".[dev]"
python -m ruff check nz_coder tests scripts/release_smoke.py
pytest -q
python scripts/release_smoke.py
python scripts/benchmark_terminal_product_final.py \
  --output docs/evidence/terminal-product-final-2026-08-13.json
# When both interpreters are installed:
python3.12 scripts/release_smoke.py
python3.13 scripts/release_smoke.py
```

The wheel smoke must prove all of the following:

- a PEP 517 wheel builds without network-dependent build isolation;
- the `nz-coder` console entry point is present;
- `nz_coder/bundled_skills/code-review/SKILL.md` is packaged;
- bundled Markdown commands are packaged;
- the installed import resolves outside the source checkout;
- installed `python -m nz_coder --help` works;
- installed `doctor --json` returns valid, secret-free JSON.
- installed `config show --json` and `platform --json` remain secret-free.
- doctor and the terminal do not inherit developer Provider credentials during smoke.
- on POSIX, an installed real PTY shows the composer and slash menu, runs a command
  before and after resize without leaving the screen, clears a draft, exits on two
  subsequent empty Ctrl+C presses, emits no traceback, and has exactly one
  alternate-screen owner.

## First-run acceptance

In a new trusted repository:

```bash
nz-coder init
# fill one Provider credential in .env
nz-coder doctor
nz-coder
```

Acceptance rules:

- `init` creates `.env` with mode 0600 and never overwrites it;
- shell environment variables override workspace `.env` values;
- the selected native Provider accepts its specific credential variable;
- `doctor` has no FAIL rows before an Agent starts;
- WARN rows are optional capabilities, such as a missing LSP for one language;
- credentials are never printed by doctor, traces, or release logs.

## Evidence matrix

| Evidence | Current status |
|---|---|
| ProductScenarioSuite T1–T20 | 20/20 passed on 2026-08-13; T1/T2/T3/T9 are real journeys covering isolated install, Provider setup, PTY coding, and daemon attach |
| Current source full regression | 2019 passed, 10 native-Windows skips on Linux/Python 3.13 on 2026-08-13; 7 `fork()` deprecation warnings, 0 failures |
| Linux, Python 3.12, non-editable wheel | Reverified locally at A130, including isolated dependencies and resized real PTY |
| Linux, Python 3.13, non-editable wheel | Reverified locally at A130, including isolated dependencies and resized real PTY |
| Full automated regression | 1144 passed on each of Python 3.12/3.13 at A130, 1 existing multiprocessing warning each |
| Source-external help, init and doctor | Verified credential-free locally at A124 |
| macOS terminal | Not yet verified |
| Windows Terminal | Not yet verified |
| Python 3.9–3.11 wheel matrix | Not yet verified |
| Public OpenAI/Anthropic/Gemini request | Requires user-owned credentials and explicit live smoke |
| Configured DeepSeek terminal flow | Verified at A130: real read tool, queued follow-up takeover, one old-run Provider request, clean Ctrl+C exit |
| Public third-party MCP interoperability | Requires an explicitly selected server and credentials |
| Fixed 300-instance SWE-bench Lite result | Explicitly deferred |

Do not convert an unverified row into a release claim based on local protocol
fixtures. Product code may be complete while external compatibility evidence is
still pending.

## Windows + Terminal UX RC gate

The native Windows evidence owner is `.github/workflows/windows-product-rc.yml`.
It must pass W1–W15 and Windows R1–R12 before changing Windows from Developer
Preview to Release Candidate. The workflow installs the conditional pywinpty
dependency and exercises PowerShell, space/CJK paths, ConPTY read/write/resize/
Ctrl+C, daemon/attach, Session restore, clipboard, TUI, wheel, and sdist. It also
installs/discovers basedpyright, TypeScript language server, and gopls, imports
the default Tree-sitter wheels, and runs a real Python MCP stdio round-trip.

Local preflight commands are:

```bash
pytest -q tests/test_windows_product_scenarios.py \
  tests/test_windows_platform_runtime.py \
  tests/test_windows_shell_runtime.py \
  tests/test_process_backends.py \
  tests/test_tui_product_frames.py \
  tests/test_tui_product_scenarios.py
python scripts/release_smoke.py
```

Linux simulations may validate dispatch and validation rules. Only the
`windows-latest` native job can close the Windows-host evidence rows.

## Safety statement

NZ-Coder is for trusted local repositories. Permission checks, workspace path
validation, transactions, and undo reduce accidental changes, but they are not
an OS sandbox. Public release notes must not imply that Bash or child processes
are isolated from the current operating-system account.
