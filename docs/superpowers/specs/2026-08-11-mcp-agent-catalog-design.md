# MCP Agent Catalog Design

## Goal

Expose the already-supported MCP tools, prompts, resources and connection
status to the Agent through one bounded, progressively discoverable tool.
Transport, OAuth, trust, notifications and client lifecycle remain unchanged.

## Design

The active `MCPRuntime` is bound through a `ContextVar` by
`ProductionRuntimeHost`, alongside the existing dynamic tool provider. A
registered local `mcp_catalog` tool reads only that run-owned runtime.

`mcp_catalog` supports three operations:

- `search`: lexical search over server status, dynamic tools, prompts and
  resources, returning bounded metadata only.
- `get_prompt`: fetch one named prompt from one connected server.
- `read_resource`: fetch one exact URI from one connected server.

Search never performs arbitrary remote calls and never exposes configuration,
commands, environment variables, OAuth tokens or trust-store internals.
Fetch/read operations require exact server/name or server/URI values obtained
from search. Output is JSON and then passes through the unified Tool Result
Budget already implemented in the production Tool Runtime.

## Isolation and Failure Semantics

The MCP runtime binding is Context-local and reset at run exit, so concurrent
workspaces cannot discover each other's servers. Outside an active run the
tool returns `Error: MCP runtime is not active`. MCP protocol, timeout and
connection failures are caught by the handler and returned with `Error:`.

## Verification

Tests cover prompt/resource/tool/status discovery, query and kind filtering,
bounded results, prompt/resource retrieval, error formatting, ContextVar
isolation and host binding cleanup. No live MCP server or network is required.
