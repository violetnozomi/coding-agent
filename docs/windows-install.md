# Install NZ-Coder on Windows

NZ-Coder's Windows installer is a self-contained, per-user package for Windows
10/11 x64. It includes the Python runtime and core NZ-Coder dependencies, so the
target computer does not need Python, pip, pipx, or Git.

## Download and verify

Download `NZ-Coder-0.1.0-windows-x64-setup.exe` and `SHA256SUMS.txt` from the
matching GitHub Release or from the successful Windows Installer workflow.
Until an Authenticode certificate is configured, Windows may show an unknown-
publisher warning; verify the checksum before continuing:

```powershell
Get-FileHash .\NZ-Coder-0.1.0-windows-x64-setup.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

The two SHA-256 values must match. Run the setup EXE, keep the recommended
current-user installation, and allow it to add NZ-Coder to your user PATH. Open
a new Windows Terminal after setup completes.

## Configure a workspace

NZ-Coder operates on the directory in which it starts. Open a trusted source
repository and initialize its private configuration:

```powershell
cd D:\code\your-project
nz-coder init
nz-coder doctor
nz-coder
```

Inside NZ-Coder, run `/connect`. The connection dialog masks the API key,
discovers available models when supported, writes the workspace `.env` through
the private Windows DACL path, and activates the model for the current Session.

To configure DeepSeek manually, edit the generated `.env`:

```dotenv
MODEL_PROVIDER=openai-compatible
MODEL_ID=deepseek-v4-flash
API_BASE_URL=https://api.deepseek.com
API_KEY=replace-with-your-key
PERMISSION_MODE=default
```

For another OpenAI-compatible service, replace `MODEL_ID`, `API_BASE_URL`, and
`API_KEY` with the values supplied by that service. Native Anthropic, Gemini,
and OpenAI Responses adapters use the provider-specific variables documented in
the generated file. Never put a real API key in a Git commit.

Validate configuration with:

```powershell
nz-coder doctor
nz-coder config show --sources
nz-coder platform
```

Language servers are optional and are not bundled in the setup executable.
Doctor reports which servers are present and the installation action for each
missing language. Structural repository intelligence remains available without
an optional language server.

## Upgrade

Download the newer verified setup executable and run it normally. The stable
installer identity replaces program files in place. It does not remove or
rewrite project `.env`, `.nz-coder`, Sessions, traces, memories, or source files.

## Uninstall

Use **Settings → Apps → Installed apps → NZ-Coder → Uninstall**, or use the
NZ-Coder uninstaller from the Start Menu. Uninstall removes the application,
shortcut, and installer-owned PATH entry. Repository `.env` and `.nz-coder`
directories deliberately remain because they contain user configuration and
work-product state; delete them manually only when you no longer need them.

## Optional language servers

Install only the servers needed by your projects. Typical examples are:

```powershell
py -m pip install basedpyright
npm install --global typescript typescript-language-server
go install golang.org/x/tools/gopls@latest
```

Restart Windows Terminal and run `nz-coder doctor` after changing PATH.
