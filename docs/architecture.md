# NZ-Coder Architecture

NZ-Coder is a terminal coding-agent runtime built around a small set of explicit subsystems.

## Runtime Flow

```text
user input
  -> AgentLoop
  -> OpenAI-compatible chat completion
  -> tool calls
  -> permission check
  -> tool dispatch
  -> transaction/change tracking/trace logging
  -> tool results back to model
  -> final answer
```

## Core Components

- `loop.py`: orchestrates model calls, tool execution, retry/backoff, transaction boundaries, trace events, and context compaction.
- `tools/`: exposes file, shell, search, and todo tools through a function-calling registry.
- `permissions.py` and `command_policy.py`: classify shell commands and enforce deny/ask/allow behavior.
- `transaction.py`: snapshots edited files during multi-tool write rounds and rolls back if any write fails.
- `changes.py`: records agent-authored before/after file snapshots for `/diff` and `/revert-last`.
- `trace.py`: records JSONL events for each run so failures can be inspected after the fact.
- `sessions.py`: saves and restores conversation history for resume workflows.
- `benchmark.py`: evaluates the agent across coding-task categories and produces JSON/Markdown reports.

## Safety Model

NZ-Coder uses layered safety rather than a single prompt instruction:

- Workspace path checks prevent file tools from escaping `WORKDIR`.
- Shell commands are classified as read-only, mutating, or dangerous.
- Plan mode blocks writes and unknown/mutating shell commands.
- File writes return unified diffs.
- Multi-file write rounds run inside a transaction.
- Agent-authored changes are tracked and can be reverted if the current file still matches the tracked after-state.

## Observability

Trace events are stored as JSONL. They include model request/response summaries, tool calls, errors, compaction events, and run termination status. The CLI exposes `/trace` for quick inspection and `/status` for current workspace/runtime state.

