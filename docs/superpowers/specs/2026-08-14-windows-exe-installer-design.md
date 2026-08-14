# NZ-Coder Windows EXE Installer Design

Date: 2026-08-14

## Goal

Ship a native-looking `NZ-Coder-0.1.0-windows-x64-setup.exe` for Windows 10
and Windows 11 x64. A user must be able to install, launch, configure, upgrade,
and uninstall NZ-Coder without separately installing Python, Git, pip, or pipx.

The installer is a distribution boundary only. It must package the already
verified Windows Product RC runtime without creating a second Agent runtime or
changing CLI, Session, permission, provider, memory, MCP, LSP, or tool behavior.

## Selected Approach

Use PyInstaller in one-directory mode to freeze the Python application and its
runtime dependencies. Wrap that directory with Inno Setup to produce one signed-
ready setup executable.

One-directory mode is selected over PyInstaller one-file mode because NZ-Coder
loads package data and native modules such as `pywinpty` and Tree-sitter. Keeping
those files installed beside the executable avoids per-launch extraction,
reduces antivirus-sensitive temporary execution, and makes missing-resource
failures diagnosable. Inno Setup supplies the installation wizard, per-user
installation, PATH registration, shortcuts, upgrades, and uninstallation.

## Release Artifact and Platform Boundary

- Artifact name: `NZ-Coder-0.1.0-windows-x64-setup.exe`.
- Supported hosts: Windows 10/11, x86-64.
- Installation scope: current user by default; no administrator privilege is
  required.
- Installation directory: the current user's local application-data programs
  directory under an `NZ-Coder` product folder.
- Entrypoints: `nz-coder.exe` in the installation directory, an optional Start
  Menu shortcut that launches Windows Terminal or the console host, and a PATH
  entry owned by the installer.
- Uninstall removes installed program files, shortcuts, and the installer-owned
  PATH entry. It never deletes repository-local `.env` or `.nz-coder` data.
- In-place upgrades use a stable application identifier and replace only
  installer-owned files.

macOS, Linux, ARM64 Windows, Microsoft Store/MSIX distribution, automatic
background updates, and code-signing certificate procurement are outside this
phase. The build remains ready for Authenticode signing when a certificate is
provided later.

## Packaged Components

The frozen application includes:

- the CPython runtime selected by the release workflow;
- the `nz_coder` package and declared package data;
- the CLI and headless console entry behavior;
- OpenAI client, Rich, prompt-toolkit, PyYAML, dotenv, watchfiles, pywinpty, and
  supported Tree-sitter runtimes;
- bundled commands and bundled skills;
- license and concise Windows installation documentation.

Language servers are not bundled. Python, TypeScript/JavaScript, Go, C/C++, and
shell language servers have separate release cycles and can add hundreds of
megabytes. `nz-coder doctor` continues to detect them and reports actionable,
language-specific installation guidance. Missing optional language servers do
not prevent NZ-Coder from starting or using structural repository intelligence.

## Build Structure

The repository will own three explicit release inputs:

1. A PyInstaller specification or small build driver that declares entrypoint,
   package data, hidden imports, native binaries, version metadata, and output
   directory.
2. An Inno Setup script that defines per-user installation, stable upgrade ID,
   PATH ownership, Start Menu shortcut, uninstaller, and artifact filename.
3. A PowerShell orchestration script that builds from a clean checkout, verifies
   expected files, invokes Inno Setup, and writes machine-readable evidence.

Release-only dependencies belong to the build workflow, not NZ-Coder's runtime
dependencies. Versions of PyInstaller and the installer compiler are pinned or
otherwise recorded in build evidence so the artifact is reproducible enough to
audit.

## Installation and First Run

The setup wizard displays product/version/architecture and installs the program.
After completion, a user opens Windows Terminal in a trusted source repository
and runs:

```powershell
nz-coder init
nz-coder doctor
nz-coder
```

The Start Menu entry may open the product, but it must not silently choose a
workspace with write authority. If launched outside a suitable repository, the
CLI keeps its existing workspace behavior and clearly displays the selected
directory.

## Provider and Secret Configuration

The installer never asks for, embeds, logs, migrates, or stores an API key.
Provider configuration remains workspace-owned and follows the existing
precedence contract: process environment, then workspace `.env`.

The recommended interactive flow is `/connect`, which masks the credential,
writes the workspace `.env` through the existing private-path implementation,
discovers models, and activates the selection in the current Session. The
non-interactive flow is `nz-coder init` followed by editing `.env`.

For DeepSeek-compatible use, the generated settings are equivalent to:

```dotenv
MODEL_PROVIDER=openai-compatible
MODEL_ID=deepseek-v4-flash
API_BASE_URL=https://api.deepseek.com
API_KEY=replace-me
PERMISSION_MODE=default
```

For another OpenAI-compatible endpoint, the user changes `MODEL_ID`,
`API_BASE_URL`, and `API_KEY`. Native Anthropic, Gemini, and OpenAI Responses
adapters continue to use their existing provider-specific variables. Secrets
must not appear in installer logs, build artifacts, Doctor JSON, or crash output.

## Failure Handling

- The build fails if PyInstaller reports a missing required module, package data
  is absent, the console entrypoint cannot start, or the setup artifact is absent.
- Installation failure is reported by the setup program and must not leave an
  active PATH entry pointing at an incomplete directory.
- An unsupported architecture is rejected before files are installed.
- A missing optional LSP is a Doctor warning, not an installer failure.
- Missing provider credentials allow local commands and `/connect`; they do not
  crash the terminal product.
- Uninstall preserves all user workspaces, `.env` files, Sessions, traces,
  memories, and repository changes.

## Verification

GitHub Actions runs the installer build and product smoke on `windows-latest`.
The release job must verify:

1. clean x64 build from the repository;
2. expected frozen modules, package data, native DLLs, and version resources;
3. silent per-user installation into a path containing spaces;
4. `nz-coder --help`, `nz-coder platform --json`, and credential-free
   `nz-coder doctor --json` from outside the source checkout;
5. headless local startup without a provider credential;
6. an interactive ConPTY composer/start/cancel/exit smoke using the installed
   executable;
7. bundled skill and command discovery;
8. pywinpty and Tree-sitter import/runtime behavior through packaged features;
9. upgrade over an existing installation without deleting workspace state;
10. silent uninstall, removal of installer-owned PATH/shortcuts, and preservation
    of a separate test workspace;
11. artifact checksum plus structured build/install/smoke/uninstall evidence;
12. successful upload of the setup executable and evidence as workflow artifacts.

The existing Windows Product RC workflow remains a separate source-install
quality gate. The installer workflow adds distribution evidence and does not
replace W1-W15, R1-R12, Repo Intelligence, or Linux sanity checks.

## Release Publishing

Normal branch and pull-request runs build and test the installer, then upload it
as a GitHub Actions artifact. A version tag matching the project version may
attach the exact tested setup executable, checksum, and evidence to a GitHub
Release. Publishing must consume the artifact produced by the tested job rather
than rebuilding an unverified binary.

## Acceptance Criteria

This phase is complete when a non-developer can download one EXE, install it
without Python or Git, run `nz-coder` from a new terminal, configure a provider
with `/connect`, use the existing Windows RC terminal runtime, upgrade safely,
and uninstall without losing workspace data. Both the installer-specific job
and the existing Windows Product RC checks must be green for the same commit.
