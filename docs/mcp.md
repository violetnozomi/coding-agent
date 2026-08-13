# NZ-Coder MCP guide

_Connect external tools, prompts, and resources through governed MCP transports._

---

## 📋 Supported behavior

NZ-Coder supports stdio, Streamable HTTP, and legacy SSE transport behavior,
including session headers, notifications, reconnect, OAuth discovery/PKCE,
credential refresh, trust for workspace commands, and failure isolation between
servers. MCP tools enter the normal catalog, permission, tracing, and output
projection boundaries.

Use the CLI to inspect exact commands for the installed version:

```bash
nz-coder mcp --help
nz-coder mcp list
nz-coder doctor
```

Project stdio commands are not started until trusted. Remote URLs must satisfy
the configured transport safety rules. Credentials are kept outside projected
extension metadata and are never printed by `doctor` or `config show`.

## 🔍 Agent discovery

MCP tools are dynamically scoped to the active runtime. Cached prompts and
resources are exposed through a bounded `mcp_catalog` search/get/read tool so a
large external catalog does not consume the whole model context. Child Sessions
do not inherit a parent's dynamic tool overlay unless their own runtime grants it.

## ⚠️ Lifecycle boundary

`extensions reload` delegates to a live MCP owner when available. Operations
that cannot be safely hot-reloaded report `restart_required`; metadata refresh
alone is not described as a connection restart. A hosted marketplace is out of
scope.
