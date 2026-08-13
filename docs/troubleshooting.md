# NZ-Coder troubleshooting

_Diagnose common installation, provider, terminal, daemon, parser, and process failures._

---

## 🔍 Start with product diagnostics

```bash
nz-coder doctor
nz-coder config show --sources
nz-coder platform
```

Use `--json` for issue reports and CI. These commands redact credentials and do
not start a provider, language server, or MCP process merely to inspect status.

## 🔐 Provider authentication fails

Confirm that the selected provider's credential environment variable exists,
the base URL includes the expected API prefix, and `MODEL_ID` names a model the
endpoint exposes. Run `nz-coder models` or the provider smoke command only when
you intentionally authorize a network request. Do not paste keys into issue
reports.

## 🖥️ Terminal or clipboard behaves differently

Run `nz-coder platform --json`. Headless sessions do not claim OSC 52 or native
clipboard support. Linux image paste requires `wl-paste` on Wayland or `xclip`
on X11; macOS uses `pngpaste`; Windows and WSL image paste use
`powershell.exe`. Plain file attachments remain available without clipboard
integration.

## 🌐 Daemon attach fails

```bash
nz-coder daemon status
nz-coder daemon stop
nz-coder daemon start --workspace /path/to/repository
```

Do not reuse a token from another daemon profile. An unavailable endpoint with a
live, identity-matched owner must be stopped before restart; NZ-Coder refuses to
kill an unrelated reused PID.

## 🔧 Parser or LSP is unavailable

Python AST and installed tree-sitter parser tiers appear in `doctor`. LSP is an
optional navigation enhancement: install only the language servers used by the
repository. Missing LSP does not disable structural search or Repo Map.

## ⚙️ Process output has a cursor gap

Persistent process logs are bounded. An expired cursor means older bytes were
discarded; re-read from the returned retained cursor or use a tail read. It is
not evidence that the process restarted.

## ❌ A traceback appears

Normal product failures should render typed messages. Save the exact command,
`nz-coder --version`, redacted `doctor --json`, `platform --json`, and the
smallest reproducible steps. Never attach `.env`, daemon tokens, raw prompts, or
unredacted traces to a public report.

| Failure | Normal product behavior | Action |
| --- | --- | --- |
| Provider authentication | Typed provider error, stable headless exit code | Check `config show --sources` and credential name |
| Model unavailable | Provider/model diagnostic | Run intentional model discovery or choose a configured model |
| MCP failure/auth expiry | Server-scoped status/error | Inspect `nz-coder mcp`; reconnect only with authorization |
| Tree-sitter/LSP missing | Optional capability warning | Install only the parser/server needed by the repository |
| Daemon unavailable/wrong token | Authenticated attach error | Restart the owned daemon; never copy a stale token |
| Session corrupt/oversized | Record skipped or marked interrupted | Preserve the file for private diagnosis and resume a valid Session |
| Process unavailable/cursor gap | Typed ProcessService error | Inspect process identity/status and restart explicitly if needed |
| External editor failure | TUI recovery then safe inline mode | Fix `VISUAL`/`EDITOR` and retry `/editor` |

Startup exceptions are traceback-free by default and point to `doctor`.
`NZ_CODER_DEBUG=1` is the explicit developer-only escape hatch for a traceback.
