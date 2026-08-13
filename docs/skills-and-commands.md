# NZ-Coder skills, commands, and extensions

_Choose the correct extension mechanism and manage its lifecycle safely._

---

## 📋 Concepts

| Mechanism | Purpose | Execution model |
| --- | --- | --- |
| Command | Lightweight prompt shortcut | Inert Markdown expansion |
| Skill | Instructions, resources, and tool policy | Loaded into the active Agent scope |
| Workflow | Multi-step, multi-Agent orchestration | Inert JSON Capsule, approval, then async runtime |
| MCP | External tool, prompt, or resource service | Governed external transport |
| Hook | Runtime lifecycle integration | Schema-limited configured owner |
| Tool pack | Optional local tools | Registered catalog owner |

This composition is deliberately different from a universal plugin class. It
covers extension behavior without creating a second orchestration runtime.

Embedded workflow commands provide list, generate, run, show, pause, resume,
and stop. Generation returns declarative JSON rather than executable model code;
the approval gate runs before asynchronous child work starts. The durable
journal restores identity and marks orphaned active work failed after restart.
Remote attach exposes list/show/run/pause/resume/stop. Its `run` flow resolves
the Capsule in the daemon workspace, shows the exact risk/limits, and binds the
approval to the resolved plan digest before the shared Workflow owner starts it.

## 🔧 Skill lifecycle

Project, user, and bundled skills preserve source provenance and resource base.
Declared `allowed_tools` narrows the runtime tool policy. Enable/disable state is
persisted by the skill owner in `.nz-coder/settings.json`, and reload performs a
real rescan.

```bash
nz-coder extensions list
nz-coder extensions status skill:code-review
nz-coder extensions disable skill:code-review
nz-coder extensions enable skill:code-review
nz-coder extensions reload
```

Unsupported hot lifecycle operations return an explicit error or
`restart_required`; the registry never pretends that a metadata refresh changed
a live owner.

There is intentionally no hosted marketplace or universal plugin installer in
this phase. Local Skills and commands are installed by placing bounded data in
their documented project/user directories; MCP configuration retains its own
owner. A future archive installer is justified only when signature, overwrite,
and removal semantics are specified—it is not silently approximated by copying
untrusted executable code.

## 📝 Command format

```markdown
---
description: Review selected code
allowed_tools:
  - read_file
  - grep_search
  - bash
model: provider/model
---
Review $ARGUMENTS. Start with $1 and report concrete evidence.
```

Project command files must be regular, non-symlink Markdown files no larger than
256 KiB. Built-in runtime commands always win name conflicts.
