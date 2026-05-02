"""Core agent loop: user → model → tool_use → tool_result → continue.

Supports two execution modes:
  - Streaming (default): tokens are yielded to on_token() as they arrive
  - Non-streaming: full response returned at once (used by benchmark)
"""

import json

from openai import OpenAI

from nz_coder import config
from nz_coder.changes import ChangeTracker
from nz_coder.context import estimate_tokens, micro_compact, auto_compact, persist_large_output
from nz_coder.permissions import PermissionManager
from nz_coder.recovery import RecoveryState
from nz_coder.trace import TraceRecorder
from nz_coder.transaction import TransactionManager
from nz_coder.tools import dispatch, get_specs
from nz_coder.tools.todo import has_open_items, get_reminder

# Import tool modules to trigger registration
import nz_coder.tools.bash       # noqa: F401
import nz_coder.tools.files      # noqa: F401
import nz_coder.tools.python_ast  # noqa: F401
import nz_coder.tools.search     # noqa: F401
import nz_coder.tools.todo       # noqa: F401
import nz_coder.subagent          # noqa: F401
import nz_coder.memory            # noqa: F401
import nz_coder.skills            # noqa: F401

# Register compact as a special tool
from nz_coder.tools import register
register(
    name="compact",
    description="Manually compress the conversation context to free up space.",
    parameters={"type": "object", "properties": {}},
    handler=lambda: "Compacting...",
)


class AgentLoop:
    def __init__(self, system_prompt: str, permission_mode: str = None,
                 client=None, tracer: TraceRecorder = None, trace_enabled: bool = None,
                 change_tracker: ChangeTracker = None):
        self.client = client or OpenAI(api_key=config.API_KEY, base_url=config.API_BASE_URL)
        self.system_prompt = system_prompt
        self.permissions = PermissionManager(permission_mode)
        self.recovery = RecoveryState()
        self.rounds_without_todo = 0
        self.txn = TransactionManager()
        enabled = config.TRACE_ENABLED if trace_enabled is None else trace_enabled
        self.tracer = tracer or TraceRecorder(enabled=enabled)
        self.change_tracker = change_tracker or ChangeTracker(
            run_id=self.tracer.run_id,
            change_dir=config.WORKDIR / ".nz-coder" / "changes",
        )
        # Inject transaction manager into file tools
        from nz_coder.tools.files import set_change_tracker, set_txn_manager
        set_txn_manager(self.txn)
        set_change_tracker(self.change_tracker)

    def run(self, messages: list, on_tool=None, on_text=None,
            on_token=None, stream: bool = True) -> None:
        """Run the agent loop until the model stops calling tools.

        Args:
            messages: Conversation history (mutated in place).
            on_tool: Callback(tool_name, output) for each tool execution.
            on_text: Callback(text) for final assembled text (non-streaming).
            on_token: Callback(token_str) for each streaming token chunk.
            stream: Whether to use streaming mode. Default True.
        """
        self.last_status = {"status": "running", "errors": 0}
        self.tracer.log(
            "run_start",
            message_count=len(messages),
            stream=stream,
            mode=self.permissions.mode,
            change_set=str(self.change_tracker.path),
        )
        for _ in range(config.MAX_AGENT_TURNS):
            # Context compression pipeline
            micro_compact(messages)
            if estimate_tokens(messages) > config.MAX_CONTEXT_TOKENS:
                if on_text:
                    on_text("[auto-compact triggered]")
                self.tracer.log("compact", kind="auto", token_estimate=estimate_tokens(messages))
                messages[:] = auto_compact(messages, self.client, config.MODEL_ID)

            # Build API messages
            api_messages = [{"role": "system", "content": self.system_prompt}]
            api_messages.extend(self._sanitize_messages(messages))
            self.tracer.log("llm_request", message_count=len(api_messages), token_estimate=estimate_tokens(api_messages))

            if stream:
                result = self._call_streaming(api_messages, on_token)
            else:
                result = self._call_non_streaming(api_messages)

            if result is None:
                # Error recovery exhausted
                if on_text:
                    on_text(f"Agent aborted after {self.recovery.consecutive_errors} consecutive errors")
                self.last_status = {
                    "status": "aborted",
                    "errors": self.recovery.consecutive_errors,
                    "last_error": self.recovery.last_error,
                }
                self.tracer.log("run_end", status="aborted", errors=self.recovery.consecutive_errors)
                return self.last_status

            content_text, tool_calls_raw = result
            self.tracer.log("llm_response", content_len=len(content_text or ""), tool_calls=len(tool_calls_raw or []))

            # Append assistant message
            assistant_msg = {"role": "assistant", "content": content_text or ""}
            if tool_calls_raw:
                assistant_msg["tool_calls"] = tool_calls_raw
            messages.append(assistant_msg)

            # If no tool calls, output text and return
            if not tool_calls_raw:
                if content_text and on_text and not stream:
                    on_text(content_text)
                if stream and on_token:
                    on_token(None)  # signal end-of-stream
                self.last_status = {"status": "completed", "errors": 0}
                self.tracer.log("run_end", status="completed", message_count=len(messages))
                return self.last_status

            # Execute tool calls inside a transaction
            manual_compact = False
            used_todo = False
            executable_calls = tool_calls_raw[:config.MAX_TOOL_CALLS_PER_RESPONSE]
            has_write = any(
                tc["function"]["name"] in ("write_file", "edit_file", "apply_patch", "python_structural_edit")
                for tc in executable_calls
            )
            if has_write:
                self.txn.begin()

            all_succeeded = True
            for i, tc in enumerate(tool_calls_raw):
                fn_name = tc["function"]["name"]
                fn_args_raw = tc["function"].get("arguments", "{}")
                tool_executed = False
                if i >= config.MAX_TOOL_CALLS_PER_RESPONSE:
                    output = f"Error: Too many tool calls in one response (limit {config.MAX_TOOL_CALLS_PER_RESPONSE})"
                    all_succeeded = False
                else:
                    try:
                        tool_input = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else (fn_args_raw or {})
                    except json.JSONDecodeError as e:
                        output = f"Error: Invalid JSON arguments for {fn_name}: {e}"
                        all_succeeded = False
                    else:
                        # Permission check
                        decision = self.permissions.check(fn_name, tool_input)
                        if decision["behavior"] == "deny":
                            output = f"Denied: {decision['reason']}"
                            all_succeeded = False
                        elif decision["behavior"] == "ask":
                            if self.permissions.ask_user(fn_name, tool_input):
                                output = dispatch(fn_name, tool_input)
                                tool_executed = True
                            else:
                                output = "Denied by user"
                                all_succeeded = False
                        else:
                            output = dispatch(fn_name, tool_input)
                            tool_executed = True

                    tool_failed = output.startswith("Error:")
                    if tool_failed:
                        all_succeeded = False

                    if tool_executed and not tool_failed and fn_name == "compact":
                        manual_compact = True
                    if tool_executed and not tool_failed and fn_name == "todo":
                        used_todo = True

                # Persist large outputs
                output = persist_large_output(tc["id"], output)

                if on_tool:
                    on_tool(fn_name, output)

                self.tracer.log(
                    "tool_call",
                    name=fn_name,
                    status="error" if output.startswith("Error:") or output.startswith("Denied") else "ok",
                    output_len=len(output),
                    output=output,
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": output,
                })

            # Transaction: commit or rollback
            if has_write:
                if all_succeeded:
                    self.txn.commit()
                else:
                    rollback_report = self.txn.rollback()
                    if rollback_report:
                        self.tracer.log("transaction_rollback", report=rollback_report)
                        messages.append({
                            "role": "user",
                            "content": f"<transaction-rollback>\n{rollback_report}\n</transaction-rollback>",
                        })

            # Todo reminder
            self.rounds_without_todo = 0 if used_todo else self.rounds_without_todo + 1
            reminder = get_reminder(self.rounds_without_todo)
            if reminder:
                messages.append({"role": "user", "content": reminder})

            # Manual compact
            if manual_compact:
                if on_text:
                    on_text("[manual compact]")
                self.tracer.log("compact", kind="manual")
                messages[:] = auto_compact(messages, self.client, config.MODEL_ID)

        if on_text:
            on_text(f"Agent stopped after reaching MAX_AGENT_TURNS={config.MAX_AGENT_TURNS}")
        self.last_status = {"status": "max_turns", "errors": self.recovery.consecutive_errors}
        self.tracer.log("run_end", status="max_turns", message_count=len(messages))
        return self.last_status

    def _call_streaming(self, api_messages: list, on_token=None):
        """Streaming LLM call. Returns (content_text, tool_calls_list) or None on abort."""
        while True:
            content_parts = []
            tool_calls_map = {}  # index -> {id, function: {name, arguments}}
            try:
                stream = self.client.chat.completions.create(
                    model=config.MODEL_ID,
                    messages=api_messages,
                    tools=get_specs(),
                    max_tokens=8000,
                    stream=True,
                )

                # Accumulate streaming chunks
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    # Text content
                    if delta.content:
                        content_parts.append(delta.content)
                        if on_token:
                            on_token(delta.content)

                    # Tool call deltas
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": tc_delta.id or "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            entry = tool_calls_map[idx]
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    entry["function"]["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    entry["function"]["arguments"] += tc_delta.function.arguments

                self.recovery.record_success()
                content_text = "".join(content_parts)
                tool_calls_list = [tool_calls_map[i] for i in sorted(tool_calls_map)] if tool_calls_map else []
                return content_text, tool_calls_list
            except Exception as e:
                if content_parts and on_token:
                    on_token(f"\n[stream interrupted: {e}]\n")
                if not self._handle_api_error(e):
                    return None

    def _call_non_streaming(self, api_messages: list):
        """Non-streaming LLM call. Returns (content_text, tool_calls_list) or None on abort."""
        while True:
            try:
                response = self.client.chat.completions.create(
                    model=config.MODEL_ID,
                    messages=api_messages,
                    tools=get_specs(),
                    max_tokens=8000,
                )
                self.recovery.record_success()
                msg = response.choices[0].message
                tool_calls_list = []
                if msg.tool_calls:
                    tool_calls_list = [tc.model_dump() for tc in msg.tool_calls]
                return msg.content or "", tool_calls_list
            except Exception as e:
                if not self._handle_api_error(e):
                    return None

    def _handle_api_error(self, error) -> bool:
        """Handle API errors with retry/backoff. Returns False if abort."""
        error_info = self.recovery.record_error(error)
        self.tracer.log("api_error", count=error_info["count"], error=error_info["error"])
        if error_info["should_abort"]:
            return False
        self.recovery.backoff_wait()
        return True

    def _sanitize_messages(self, messages: list) -> list:
        """Clean messages for API compatibility."""
        cleaned = []
        for msg in messages:
            clean = dict(msg)
            # Remove None content
            if clean.get("content") is None and clean.get("role") == "assistant":
                clean["content"] = ""
            cleaned.append(clean)
        return cleaned
