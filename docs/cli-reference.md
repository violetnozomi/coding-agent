# NZ-Coder CLI reference

_Stable user-facing commands for terminal, automation, configuration, and services._

---

## 📋 Top-level commands

| Command | Purpose |
| --- | --- |
| `nz-coder` | Start the embedded terminal product |
| `nz-coder run ...` | Run headless text, JSON, or JSONL automation |
| `nz-coder init` | Create a safe workspace `.env` template |
| `nz-coder doctor [--json]` | Diagnose required, optional, and experimental capabilities |
| `nz-coder config show [--sources] [--json]` | Show effective secret-free configuration |
| `nz-coder platform [--json]` | Show platform capability truth |
| `nz-coder models ...` | Inspect, discover, and select models |
| `nz-coder memory ...` | Inspect and govern memory proposals |
| `nz-coder extensions ...` | List, inspect, reload, enable, or disable extensions |
| `nz-coder mcp ...` | Inspect, trust, connect, and manage MCP servers |
| `nz-coder daemon ...` | Start, stop, and inspect the loopback daemon |
| `nz-coder attach SESSION_ID` | Attach a terminal to a daemon Session |
| `nz-coder serve ...` | Run the authenticated HTTP service in the foreground |
| `nz-coder completion SHELL` | Generate Bash, Zsh, or Fish completion |
| `nz-coder --version` | Print the installed version |

## ⚙️ Headless automation

```bash
nz-coder run "prompt"
nz-coder run --output json "prompt"
nz-coder run --output jsonl "prompt"
nz-coder run --continue "follow-up"
nz-coder run --resume SESSION_ID "follow-up"
nz-coder run --file prompt.txt --attach screenshot.png "review"
```

The headless surface builds the same `RunRequest` as the embedded and SDK
surfaces. JSONL emits runtime events followed by one terminal result record.
Exit codes distinguish success, task failure, invalid input, missing provider,
and cancellation; scripts should not parse decorated terminal output.

## 🔧 Embedded slash commands

`/help` intentionally shows only Essentials; `/help all` shows every command.
Ctrl+K opens the searchable palette, which orders suggested commands, recent
commands, common commands, then the full catalog. Its user-facing groups are:

| Group | Commands |
| --- | --- |
| Essentials | `/help`, `/keys`, `/status`, `/profile`, `/exit` |
| Model | `/model`, `/connect`, `/variants` |
| Session | `/sessions`, `/session`, `/timeline`, `/message`, `/fork`, `/rename`, `/export` |
| Files | `/diff`, `/undo`, `/redo`, `/attach`, `/attachments`, `/detach`, `/editor` |
| Agent | `/agents`, `/subagents`, `/subagent`, `/workflow`, `/todo`, `/trace` |
| Processes | `/processes` and process inspect/log/follow/write/resize/kill operations |
| Memory | `/memory`, `/memory-review` |
| Extensions | `/skills`, `/mcps`, `/extensions` |
| Settings | `/mode`, `/permission`, `/theme`, `/sidebar`, `/tool-details`, `/mouse`, `/keybind` |

Prefix a prompt with `!` to execute a direct shell command through the normal
permissioned Bash tool. Remote attach rejects this local-only shorthand; use a
normal Agent request or `/processes` for daemon-side work. The client never
silently runs a remote command in its own working directory.

## 📝 Custom commands

Markdown prompt shortcuts are discovered with this precedence:

1. `.nz-coder/commands/*.md`
2. `~/.nz-coder/commands/*.md`
3. package-owned bundled commands

They support `$ARGUMENTS` and `$1` through `$9`. An `allowed_tools` frontmatter
entry can only narrow the normal tool policy. Command files are inert: they do
not execute Python or shell code during discovery or expansion.
