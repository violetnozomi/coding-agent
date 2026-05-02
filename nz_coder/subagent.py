"""Subagent: spawn a child agent with fresh context for isolated exploration."""

import json

from openai import OpenAI

from nz_coder import config
from nz_coder.tools import register

# Subagent gets read-only tools by default
_SUB_TOOLS = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read file contents.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_directory", "description": "List directory.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "grep_search", "description": "Grep for pattern.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]},
    }},
]

_SUB_WRITE_TOOLS = [
    {"type": "function", "function": {
        "name": "write_file", "description": "Write file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Edit file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
    }},
]


def run_subagent(prompt: str, agent_type: str = "explore") -> str:
    """Spawn a subagent with fresh context. Returns only the final summary."""
    from nz_coder.tools.bash import run_bash
    from nz_coder.tools.files import read_file, write_file, edit_file, list_directory
    from nz_coder.tools.search import grep_search

    client = OpenAI(api_key=config.API_KEY, base_url=config.API_BASE_URL)
    tools = list(_SUB_TOOLS)
    if agent_type != "explore":
        tools.extend(_SUB_WRITE_TOOLS)

    handlers = {
        "bash": lambda **kw: run_bash(kw["command"], read_only=(agent_type == "explore")),
        "read_file": lambda **kw: read_file(kw["path"], limit=kw.get("limit")),
        "write_file": lambda **kw: write_file(kw["path"], kw["content"]),
        "edit_file": lambda **kw: edit_file(kw["path"], kw["old_text"], kw["new_text"]),
        "list_directory": lambda **kw: list_directory(kw.get("path", ".")),
        "grep_search": lambda **kw: grep_search(kw["pattern"], kw.get("path", ".")),
    }

    system = f"You are a coding subagent at {config.WORKDIR}. Complete the given task, then summarize your findings."
    messages = [{"role": "user", "content": prompt}]

    for _ in range(30):
        try:
            resp = client.chat.completions.create(
                model=config.MODEL_ID,
                messages=[{"role": "system", "content": system}] + messages,
                tools=tools,
                max_tokens=8000,
            )
        except Exception as e:
            return f"Subagent error: {e}"

        choice = resp.choices[0]
        msg = choice.message
        messages.append(msg.model_dump())

        if not msg.tool_calls:
            return msg.content or "(no summary)"

        for tc in msg.tool_calls:
            fn = tc.function
            handler = handlers.get(fn.name)
            try:
                args = json.loads(fn.arguments) if isinstance(fn.arguments, str) else fn.arguments
                output = handler(**args) if handler else f"Unknown tool: {fn.name}"
            except Exception as e:
                output = f"Error: {e}"
            output = str(output)[:50000]
            print(f"  [sub] {fn.name}: {output[:120]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})

    return "(subagent max turns reached)"


register(
    name="task",
    description="Spawn a subagent with fresh context for isolated exploration or work. Returns only a summary.",
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Task description for the subagent."},
            "agent_type": {
                "type": "string",
                "enum": ["explore", "general"],
                "description": "explore = read-only tools, general = all tools. Default: explore.",
            },
        },
        "required": ["prompt"],
    },
    handler=run_subagent,
)
