"""Core agent loop: user → model → tool_use → tool_result → continue.

支持两种执行模式:
  - Streaming（默认）: token 在到达时通过 on_token() 回调推送
  - Non-streaming: 完整响应一次性返回（用于 benchmark）
"""

import time

from nz_coder import config
from nz_coder.changes import ChangeTracker
from nz_coder.context import estimate_tokens, micro_compact, auto_compact, persist_large_output
from nz_coder.permissions import PermissionManager
from nz_coder.recovery import RecoveryState
from nz_coder.runtime_state import RuntimeState
from nz_coder.trace import TraceRecorder
from nz_coder.transaction import TransactionManager
from nz_coder.tools import get_specs
from nz_coder.tools.todo import get_reminder
from nz_coder.verification import VerificationManager
from nz_coder.tool_executor import ToolExecutor, WRITE_TOOLS

# 工具模块导入触发注册（副作用 import）
import nz_coder.tools.bash       # noqa: F401
import nz_coder.tools.files      # noqa: F401
import nz_coder.tools.python_ast  # noqa: F401
import nz_coder.tools.search     # noqa: F401
import nz_coder.tools.todo       # noqa: F401
import nz_coder.tools.repo_intel  # noqa: F401
import nz_coder.project_profile   # noqa: F401
import nz_coder.verification_planner  # noqa: F401
import nz_coder.impact_analyzer   # noqa: F401
import nz_coder.subagent          # noqa: F401
import nz_coder.memory            # noqa: F401
import nz_coder.skills            # noqa: F401
import nz_coder.tools.scratchpad  # noqa: F401

# compact 是特殊工具，在 loop 层注册（幂等）
from nz_coder.tools import register
register(
    name="compact",
    description="Manually compress the conversation context to free up space.",
    parameters={"type": "object", "properties": {}},
    handler=lambda: "Compacting...",
)

from dataclasses import dataclass, field
from typing import Optional

# 优先使用 OpenAI SDK 具体异常类型；版本不兼容时 fallback 到字符串匹配
try:
    from openai import BadRequestError as _BadRequestError
    from openai import UnprocessableEntityError as _UnprocessableEntityError
    _OPENAI_CLIENT_ERRORS = (_BadRequestError, _UnprocessableEntityError)
except ImportError:
    _OPENAI_CLIENT_ERRORS = ()


def _is_client_error(e: Exception) -> bool:
    """True 表示 400/422 类客户端错误，不应重试，而应注入诊断。"""
    if _OPENAI_CLIENT_ERRORS and isinstance(e, _OPENAI_CLIENT_ERRORS):
        return True
    error_str = str(e)
    return any(code in error_str for code in ("400", "422", "invalid_request_error"))


@dataclass
class LLMResult:
    """_call_streaming / _call_non_streaming 的返回值。

    用 dataclass 替代 2-tuple / 3-tuple 的长度判断，语义更清晰：
      - 正常完成：content/tool_calls 有值，diagnostic=None, aborted=False
      - 400/422：diagnostic 非 None，其余为 None
      - 不可恢复：aborted=True，其余为 None
    """
    content: Optional[str] = None
    tool_calls: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    diagnostic: Optional[str] = None  # 非 None 表示 400/422，应注入对话而非重试
    aborted: bool = False              # True 表示重试耗尽，应终止整个 run()


def _extract_model_field(obj, field_name: str):
    """从 OpenAI SDK 对象或 provider 扩展字段中读取模型返回字段。"""
    value = getattr(obj, field_name, None)
    if value is not None:
        return value
    model_extra = getattr(obj, "model_extra", None)
    if isinstance(model_extra, dict) and model_extra.get(field_name) is not None:
        return model_extra[field_name]
    if hasattr(obj, "model_dump"):
        dumped = obj.model_dump()
        if isinstance(dumped, dict):
            return dumped.get(field_name)
    return None


class AgentLoop:
    def __init__(self, system_prompt: str, permission_mode: str = None,
                 client=None, tracer: TraceRecorder = None, trace_enabled: bool = None,
                 change_tracker: ChangeTracker = None):
        from openai import OpenAI
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
        # 注入事务管理器到文件工具（依赖注入，不用全局单例）
        from nz_coder.tools.files import set_change_tracker, set_txn_manager
        set_txn_manager(self.txn)
        set_change_tracker(self.change_tracker)

        self.vm = VerificationManager(self.recovery, self.tracer)
        self.executor = ToolExecutor(self.permissions)
        self.tool_calls_this_run = 0
        self.used_save_memory = False
        self.runtime_state = RuntimeState()
        self._runtime_state_path = config.WORKDIR / ".nz-coder" / "runtime_state.json"
        self._restored_state = False
        self._replan_count = 0
        from nz_coder.tools.scratchpad import scratchpad as _sp
        self._sp = _sp
        # 持有 memory_mgr 引用，用于每轮按查询做相关性过滤
        from nz_coder.memory import memory_mgr as _mm
        self._mm = _mm
        # 上一轮 memory 查询缓存，避免连续相同查询重复计算
        self._last_memory_query: str = ""
        self._last_memory_block: str = ""
        self._project_profile_block_cache: str = ""

    def _project_profile_block(self) -> str:
        """返回适合注入 prompt 的简短项目画像。"""
        if self._project_profile_block_cache:
            return self._project_profile_block_cache
        try:
            from nz_coder.project_profile import build_project_profile, compact_profile_summary
            profile = build_project_profile(save=False)
            self._project_profile_block_cache = compact_profile_summary(profile)
        except Exception as exc:
            self.tracer.log("project_profile_failed", error=str(exc))
            self._project_profile_block_cache = ""
        return self._project_profile_block_cache

    def _memory_block(self, query: str) -> str:
        """返回与当前查询相关的 memory 注入块。

        对标 Claude Code findRelevantMemories()：
        - 有 query 时按相关性过滤（最多 5 条）
        - 没有 memory 时返回空字符串，不占 system prompt 空间
        - 相同查询结果缓存，避免 loop 内每轮重复召回
        """
        if not self._mm.memories:
            return ""
        if query and query == self._last_memory_query:
            return self._last_memory_block
        block = self._mm.build_prompt_block(
            query=query or None,
            max_items=5,
            max_chars=2000,
            rerank_client=self.client if config.MEMORY_LLM_RERANK else None,
            model=config.MODEL_ID if config.MEMORY_LLM_RERANK else None,
        )
        if query:
            self._last_memory_query = query
            self._last_memory_block = block
        return block

    def run(self, messages: list, on_tool=None, on_text=None,
            on_token=None, stream: bool = True) -> dict:
        """运行 agent loop 直到模型停止调用工具。"""
        max_turns, start_turn = self._init_run(messages, stream)
        self._maybe_generate_plan(messages)
        for turn_index in range(start_turn, max_turns):
            self.runtime_state.turn_count = turn_index + 1
            self._compact_if_needed(messages, on_text)
            api_messages = self._build_api_messages(messages)
            result = self._call_llm(api_messages, stream, on_token)

            if result.aborted:
                return self._finalize(messages, "aborted", on_text, on_token, stream)
            if result.diagnostic is not None:
                self._inject_api_diagnostic(messages, result.diagnostic)
                continue

            messages.append(self._make_assistant_message(result))
            self.tracer.log(
                "llm_response",
                content_len=len(result.content or ""),
                tool_calls=len(result.tool_calls or []),
            )

            if not result.tool_calls:
                gate_status = self._check_verification_gate(messages)
                if gate_status == "continue":
                    continue
                return self._finalize(
                    messages,
                    gate_status,
                    on_text,
                    on_token,
                    stream,
                    content_text=result.content,
                )

            self._execute_tools(result.tool_calls, messages, on_tool, on_text)
            self._persist_runtime_state(active=True)
            self._maybe_replan(messages)

        return self._finalize(messages, "max_turns", on_text, on_token, stream, max_turns=max_turns)

    def _init_run(self, messages: list, stream: bool) -> tuple[int, int]:
        """初始化一次 run，并返回 (max_turns, start_turn)。"""
        self.vm.reset()
        self.tool_calls_this_run = 0
        self.used_save_memory = False
        self._sp.clear()
        self.last_status = {"status": "running", "errors": 0}
        self._restored_state = False
        self._replan_count = 0

        _inject_missing_tool_results(messages)
        max_turns = _parse_turn_budget(messages) or config.MAX_AGENT_TURNS
        task_text = _extract_last_user_text(messages)

        agent_timeout = getattr(config, "AGENT_TIMEOUT_SECONDS", 0)
        self._runtime_state_path = config.WORKDIR / ".nz-coder" / "runtime_state.json"
        self.runtime_state.reset(max_turns=max_turns, timeout_seconds=agent_timeout)
        self.runtime_state.set_acceptance_criteria_from_text(task_text)
        self.runtime_state.initial_task_text = task_text

        if config.RUNTIME_STATE_PERSIST:
            self._restored_state = self.runtime_state.load(self._runtime_state_path)
            if self._restored_state:
                self.runtime_state.max_turns = max_turns
                self.runtime_state.timeout_seconds = agent_timeout
                if not self.runtime_state.initial_task_text:
                    self.runtime_state.initial_task_text = task_text
                if self.runtime_state.plan_text:
                    self._sp.replace_category("plan", self.runtime_state.plan_text)
                self._replan_count = self.runtime_state.replan_count
        self._persist_runtime_state(active=True)

        config.BLOCK_BROAD_TESTS = False
        self.tracer.log(
            "run_start",
            message_count=len(messages),
            stream=stream,
            mode=self.permissions.mode,
            change_set=str(self.change_tracker.path),
            max_turns=max_turns,
            restored_runtime_state=self._restored_state,
        )
        start_turn = self.runtime_state.turn_count if self._restored_state else 0
        return max_turns, start_turn

    def _compact_if_needed(self, messages: list, on_text=None) -> None:
        """执行轻量压缩，并在上下文超预算时自动压缩。"""
        micro_compact(messages)
        token_estimate = estimate_tokens(messages)
        if token_estimate <= config.MAX_CONTEXT_TOKENS:
            return
        if on_text:
            on_text("[auto-compact triggered]")
        self.tracer.log("compact", kind="auto", token_estimate=token_estimate)
        messages[:] = auto_compact(messages, self.client, config.MODEL_ID)

    def _build_api_messages(self, messages: list) -> list:
        """按固定/半固定/动态层构建 API messages。"""
        scratch_block = self._sp.build_prompt_block()
        memory_query = _extract_last_user_text(messages)
        memory_block = self._memory_block(memory_query)
        state_block = self.runtime_state.build_prompt_block()
        profile_block = self._project_profile_block()
        dynamic_state_block = "\n".join(part for part in (profile_block, state_block) if part)
        stable_system, dynamic_context, layer_stats = _build_context_layers(
            self.system_prompt,
            memory_block,
            dynamic_state_block,
            scratch_block,
            max_tokens=config.SYSTEM_CONTEXT_BUDGET_TOKENS,
        )
        sanitized_messages = self._sanitize_messages(messages)
        sanitized_messages = _inject_dynamic_context(sanitized_messages, dynamic_context)
        api_messages = [{"role": "system", "content": stable_system}]
        api_messages.extend(sanitized_messages)
        self.tracer.log("context_layers", **layer_stats)
        self.tracer.log(
            "llm_request",
            message_count=len(api_messages),
            token_estimate=estimate_tokens(api_messages),
        )
        return api_messages

    def _call_llm(self, api_messages: list, stream: bool, on_token=None) -> LLMResult:
        """按运行模式调用模型。"""
        if stream:
            return self._call_streaming(api_messages, on_token)
        return self._call_non_streaming(api_messages)

    def _maybe_generate_plan(self, messages: list) -> None:
        """复杂任务执行前生成结构化 plan；失败不阻断主流程。"""
        if not config.PLANNING_ENABLED:
            return
        if self._restored_state or self.runtime_state.plan_generated:
            return

        from nz_coder.task_policy import estimate_text_complexity

        task_mode = self.runtime_state.task_mode
        task_text = self.runtime_state.initial_task_text or _extract_last_user_text(messages)
        text_complexity = estimate_text_complexity(task_text)
        should_plan = (
            task_mode in config.PLANNING_TASK_MODES
            or text_complexity in {"moderate", "complex"}
        )
        if not should_plan:
            return

        self.tracer.log("planning_start", task_mode=task_mode, text_complexity=text_complexity)
        try:
            plan_text = self._call_planning_llm(task_text)
        except Exception as exc:
            self.tracer.log(
                "planning_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                client_error=_is_client_error(exc),
            )
            return
        if not plan_text or not plan_text.strip():
            self.tracer.log("planning_empty")
            return

        self._sp.replace_category("plan", plan_text)
        self.runtime_state.plan_generated = True
        self.runtime_state.plan_text = plan_text
        self.runtime_state.initial_plan_complexity = text_complexity
        self._persist_runtime_state(active=True)
        self.tracer.log("planning_done", plan_len=len(plan_text))

    def _call_planning_llm(self, task_text: str) -> str:
        """调用 LLM 生成 plan；不传 tools，保持纯推理。"""
        criteria = "; ".join(self.runtime_state.acceptance_criteria[:5]) or "(none extracted)"
        task_mode = self.runtime_state.task_mode
        prompt = (
            "You are a coding agent planner. Given the user's task, produce a structured execution plan.\n\n"
            f"Task: {task_text}\n"
            f"Task mode: {task_mode}\n"
            f"Acceptance criteria: {criteria}\n\n"
            "Output a concise plan in this format:\n\n"
            "## Plan\n"
            "1. [Step title] - [target file/module or 'need to search'] - [verification method]\n"
            "2. ...\n\n"
            "Rules:\n"
            "- Maximum 5 steps. Prefer fewer.\n"
            "- Each step should be independently verifiable.\n"
            "- For bugfix: locate -> understand -> fix -> verify. Usually 3 steps.\n"
            "- For feature: design -> implement -> test -> verify. Usually 4 steps.\n"
            "- For refactor: identify scope -> rename/restructure -> verify no breakage. Usually 3 steps.\n"
            "- Do NOT include 'read the task' or 'understand requirements' as a step.\n"
            "- Be specific about file paths when possible; say 'need to search' when not.\n"
            "- Last step should always be verification.\n"
            "- Keep total output under 1200 characters.\n"
        )
        resp = self.client.chat.completions.create(
            model=config.MODEL_ID,
            messages=[
                {"role": "system", "content": "You are a concise coding task planner."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=config.PLANNING_MAX_TOKENS,
        )
        return (resp.choices[0].message.content or "").strip()

    def _should_replan(self) -> bool:
        """检查是否需要动态重规划。"""
        if not config.PLANNING_ENABLED:
            return False
        if not self.runtime_state.plan_generated:
            return False
        if self._replan_count >= config.REPLAN_MAX_ATTEMPTS:
            return False
        if self.runtime_state.task_mode == "discuss":
            return False

        rs = self.runtime_state
        no_edit_turns = (rs.turn_count - rs.last_edit_turn) if rs.last_edit_turn else rs.turn_count
        if no_edit_turns >= config.REPLAN_IDLE_TURNS and rs.turn_count >= config.REPLAN_IDLE_TURNS:
            return True
        if rs.has_diff and not rs.changed_files_verified and rs.verification_attempts >= 2:
            return True
        if rs.initial_plan_complexity:
            current = rs.task_complexity()
            initial = rs.initial_plan_complexity
            escalated = (
                (initial == "simple" and current in {"L2", "L3"})
                or (initial == "moderate" and current == "L3")
            )
            if escalated:
                return True
        return False

    def _maybe_replan(self, messages: list) -> None:  # noqa: ARG002
        """检查并执行动态重规划；失败不阻断主流程。"""
        if not self._should_replan():
            return
        self.tracer.log("replan_start", attempt=self._replan_count + 1)
        try:
            new_plan = self._call_replan_llm()
        except Exception as exc:
            self.tracer.log(
                "replan_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                client_error=_is_client_error(exc),
            )
            return
        if not new_plan or not new_plan.strip():
            self.tracer.log("replan_empty")
            return

        self._sp.replace_category("plan", new_plan)
        self.runtime_state.plan_text = new_plan
        self._replan_count += 1
        self.runtime_state.replan_count = self._replan_count
        self._persist_runtime_state(active=True)
        self.tracer.log("replan_done", attempt=self._replan_count, plan_len=len(new_plan))

    def _call_replan_llm(self) -> str:
        """调用 LLM 重新规划；不传 tools。"""
        rs = self.runtime_state
        failure_notes = "\n".join(
            e["content"] for e in self._sp.entries if e.get("category") == "failure"
        ) or "(none)"
        turns_remaining = max(0, rs.max_turns - rs.turn_count)
        prompt = (
            "You are a coding agent re-planner. The original plan hit obstacles. Revise it.\n\n"
            f"Original plan:\n{rs.plan_text}\n\n"
            "Execution progress:\n"
            f"- Turn {rs.turn_count}/{rs.max_turns}, {turns_remaining} remaining\n"
            f"- Files changed: {rs.changed_files or '(none)'}\n"
            f"- Edits made: {rs.edits_this_run}\n"
            f"- Verification: verified={rs.changed_files_verified}, attempts={rs.verification_attempts}\n"
            f"- Last transition: {rs.transition}\n"
            f"- Current complexity: {rs.task_complexity()}\n\n"
            f"Failures encountered:\n{failure_notes}\n\n"
            f"Task: {rs.initial_task_text}\n"
            f"Acceptance criteria: {'; '.join(rs.acceptance_criteria[:5]) or '(none)'}\n\n"
            "Output a revised plan in the same format (## Plan, numbered steps). Rules:\n"
            "- Mark completed steps with [DONE].\n"
            "- Revise or replace steps that failed.\n"
            "- If the approach is fundamentally wrong, propose a different approach.\n"
            "- Maximum 5 steps. Be realistic about remaining turn budget.\n"
            "- Do NOT repeat failed approaches listed above.\n"
            "- Keep total output under 1200 characters.\n"
        )
        resp = self.client.chat.completions.create(
            model=config.MODEL_ID,
            messages=[
                {"role": "system", "content": "You are a concise coding task re-planner."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=config.PLANNING_MAX_TOKENS,
        )
        return (resp.choices[0].message.content or "").strip()

    def _inject_api_diagnostic(self, messages: list, diagnostic: str) -> None:
        """把 400/422 诊断注入对话，让模型下一轮自我修正。"""
        messages.append({"role": "user", "content": diagnostic})
        self.tracer.log("api_error_injected_diagnostic")

    def _make_assistant_message(self, result: LLMResult) -> dict:
        """把 LLMResult 转成可追加到历史里的 assistant 消息。"""
        assistant_msg = {"role": "assistant", "content": result.content or ""}
        if result.extra:
            assistant_msg.update(result.extra)
        if result.tool_calls:
            assistant_msg["tool_calls"] = result.tool_calls
        assistant_msg["_timestamp"] = time.time()
        return assistant_msg

    def _check_verification_gate(self, messages: list) -> str:
        """检查无工具响应时是否需要继续验证。"""
        if self.vm.should_gate() and _is_keep_going(messages):
            self.vm.reset()
            self.tracer.log("verification_gate_bypassed", reason="keep_going")

        if not self.vm.should_gate():
            return "completed"

        if self.vm.increment_gate_prompt() <= config.MAX_VERIFICATION_GATE_PROMPTS:
            messages.append({"role": "user", "content": self.vm.make_gate_message()})
            self.tracer.log(
                "verification_gate",
                prompts=self.vm.gate_prompts,
                **self.vm.status(),
            )
            return "continue"
        return "completed_unverified"

    def _execute_tools(self, tool_calls_raw: list, messages: list,
                       on_tool=None, on_text=None) -> None:
        """执行一批工具调用，并分发执行后的状态更新。"""
        manual_compact = False
        used_todo = False
        all_succeeded = True
        has_write = self._tool_batch_has_write(tool_calls_raw)
        if has_write:
            self.txn.begin()

        for _i, tc, result_r in self._dispatch_tool_calls(tool_calls_raw, has_write):
            if self._record_tool_result(result_r):
                all_succeeded = False
            if result_r.executed and not result_r.dispatch_failed and result_r.name == "compact":
                manual_compact = True
            if result_r.executed and not result_r.dispatch_failed and result_r.name == "todo":
                used_todo = True

            output = persist_large_output(tc["id"], result_r.output)
            if on_tool:
                on_tool(result_r.name, output)
            self._trace_tool_result(result_r, output)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})
            self._append_tool_recovery_diagnostic(messages, result_r.name, output)

        self._finish_tool_transaction(has_write, all_succeeded, messages)
        self._maybe_add_todo_reminder(messages, used_todo)
        self._manual_compact_if_requested(messages, manual_compact, on_text)

    def _tool_batch_has_write(self, tool_calls_raw: list) -> bool:
        """只按实际会执行的调用判断是否需要开启事务。"""
        will_execute = tool_calls_raw[:config.MAX_TOOL_CALLS_PER_RESPONSE]
        return any(tc["function"]["name"] in WRITE_TOOLS for tc in will_execute)

    def _dispatch_tool_calls(self, tool_calls_raw: list, has_write: bool) -> list:
        """读工具批量并发，写工具或 task 串行。"""
        will_execute = tool_calls_raw[:config.MAX_TOOL_CALLS_PER_RESPONSE]
        non_concurrent_tools = {"task"}
        all_read_only = (
            not has_write
            and len(will_execute) > 1
            and not any(tc["function"]["name"] in non_concurrent_tools for tc in will_execute)
        )
        if all_read_only:
            return _execute_concurrent(self.executor, tool_calls_raw)
        return [(i, tc, self.executor.execute_one(tc, i)) for i, tc in enumerate(tool_calls_raw)]

    def _record_tool_result(self, result_r) -> bool:
        """观察工具结果并更新 verification/scratchpad/runtime 状态。"""
        if result_r.executed and not result_r.dispatch_failed:
            self.tool_calls_this_run += 1
            if result_r.name == "save_memory":
                self.used_save_memory = True

        self._observe_write_tool(result_r)
        self._observe_verification_tool(result_r)
        self._observe_runtime_tool(result_r)
        return result_r.dispatch_failed

    def _observe_write_tool(self, result_r) -> None:
        """写工具成功后更新验证状态并激活路径相关 skill。"""
        if not (result_r.is_write and result_r.executed and not result_r.dispatch_failed):
            return
        self.vm.mark_write(result_r.name, result_r.tool_input)
        edited_path = result_r.tool_input.get("path", "")
        if not edited_path:
            return
        from nz_coder.skills import skill_loader
        activated = skill_loader.activate_for_paths([str(config.WORKDIR / edited_path)])
        if activated:
            self.tracer.log("skills_activated", names=activated)

    def _observe_verification_tool(self, result_r) -> None:
        """根据 bash / symbol check / verify 工具结果更新验证状态。"""
        if result_r.executed and result_r.name == "bash":
            self.vm.observe_bash(
                result_r.tool_input,
                result_r.output,
                result_r.dispatch_failed,
                result_r.command_failed,
            )
            self._record_bash_failure(result_r)
        if (result_r.executed and not result_r.dispatch_failed
                and result_r.name == "python_symbol_check"):
            self.vm.observe_symbol_check(result_r.output)
        if (result_r.executed and not result_r.dispatch_failed
                and result_r.name == "verify_changed_files"):
            self.vm.observe_verify_changed_files(result_r.output)

    def _record_bash_failure(self, result_r) -> None:
        """把失败测试摘要写入 scratchpad，减少同一 session 内重复踩坑。"""
        if not result_r.command_failed:
            return
        from nz_coder.recovery import _extract_failed_tests, _extract_traceback
        failed = _extract_failed_tests(result_r.output)
        tb = _extract_traceback(result_r.output, max_chars=300)
        if not failed and not tb:
            return
        note = ""
        if failed:
            note += "Failed: " + ", ".join(failed[:3])
        if tb:
            first_line = tb.splitlines()[-1][:120] if tb.splitlines() else ""
            note += (" | " if note else "") + first_line
        if note:
            self._sp.update("failure", note[:500])

    def _observe_runtime_tool(self, result_r) -> None:
        """把成功工具调用写入 RuntimeState。"""
        if not (result_r.executed and not result_r.dispatch_failed):
            return
        self.runtime_state.observe_tool(result_r.name, result_r.tool_input, result_r.output)
        if self.runtime_state.has_diff and not config.BLOCK_BROAD_TESTS:
            config.BLOCK_BROAD_TESTS = True

    def _trace_tool_result(self, result_r, output: str) -> None:
        """记录工具调用 trace。"""
        self.tracer.log(
            "tool_call",
            name=result_r.name,
            status=(
                "error" if output.startswith("Error:") or output.startswith("Denied")
                else ("nonzero" if output.startswith("Command exited with code") else "ok")
            ),
            output_len=len(output),
            output=output,
        )

    def _append_tool_recovery_diagnostic(self, messages: list, name: str, output: str) -> None:
        """工具失败时注入恢复诊断。"""
        diagnostic = self.recovery.tool_failure_diagnostic(name, output)
        if not diagnostic:
            return
        if _last_user_has_frustration(messages):
            diagnostic = (
                "<user-frustration-context>\n"
                "The user seems frustrated. Acknowledge the difficulty briefly, "
                "then focus immediately on the concrete fix below.\n"
                "</user-frustration-context>\n" + diagnostic
            )
        messages.append({"role": "user", "content": diagnostic})
        self.tracer.log("tool_failure_diagnostic", name=name)

    def _finish_tool_transaction(self, has_write: bool, all_succeeded: bool,
                                 messages: list) -> None:
        """根据工具分发结果提交或回滚事务。"""
        if not has_write:
            return
        if all_succeeded:
            self.txn.commit()
            return
        rollback_report = self.txn.rollback()
        if not rollback_report:
            return
        self.tracer.log("transaction_rollback", report=rollback_report)
        messages.append({
            "role": "user",
            "content": f"<transaction-rollback>\n{rollback_report}\n</transaction-rollback>",
        })

    def _maybe_add_todo_reminder(self, messages: list, used_todo: bool) -> None:
        """按 todo 使用情况追加提醒。"""
        self.rounds_without_todo = 0 if used_todo else self.rounds_without_todo + 1
        reminder = get_reminder(self.rounds_without_todo)
        if reminder:
            messages.append({"role": "user", "content": reminder})

    def _manual_compact_if_requested(self, messages: list, manual_compact: bool,
                                     on_text=None) -> None:
        """处理 compact 工具触发的手动压缩。"""
        if not manual_compact:
            return
        if on_text:
            on_text("[manual compact]")
        self.tracer.log("compact", kind="manual")
        messages[:] = auto_compact(messages, self.client, config.MODEL_ID)

    def _finalize(self, messages: list, status: str, on_text=None, on_token=None,
                  stream: bool = True, content_text: str | None = None,
                  max_turns: int | None = None) -> dict:
        """统一处理所有 run 退出路径。"""
        if status == "aborted":
            if on_text:
                on_text(f"Agent aborted after {self.recovery.consecutive_errors} consecutive errors")
            self.last_status = {
                "status": "aborted",
                "errors": self.recovery.consecutive_errors,
                "last_error": self.recovery.last_error,
            }
            self.tracer.log("run_end", status="aborted", errors=self.recovery.consecutive_errors)
            self._persist_runtime_state(active=False)
            return self.last_status

        if status == "max_turns" and on_text:
            on_text(f"Agent stopped after reaching max_turns={max_turns}")
        elif content_text and on_text and not stream:
            on_text(content_text)
        if status in {"completed", "completed_unverified"} and stream and on_token:
            on_token(None)

        self.last_status = {
            "status": status,
            "errors": self.recovery.consecutive_errors if status == "max_turns" else 0,
            **self.vm.status(),
            "runtime": self._runtime_summary(),
        }
        self.tracer.log("run_end", status=status, message_count=len(messages), **self.vm.status())
        self._maybe_save_learnings(messages)
        self._persist_runtime_state(active=False)
        return self.last_status

    def _persist_runtime_state(self, active: bool = True) -> None:
        """将 RuntimeState 持久化到 .nz-coder/runtime_state.json。"""
        if not config.RUNTIME_STATE_PERSIST:
            return
        try:
            self.runtime_state.save(self._runtime_state_path, active=active)
        except OSError as exc:
            self.tracer.log("runtime_state_persist_failed", error=str(exc))

    def clear_scratchpad(self) -> None:
        """清除 scratchpad 工作记忆。供 CLI /clear 命令使用。"""
        self._sp.clear()

    def _runtime_summary(self) -> dict:
        """返回 RuntimeState 的关键字段摘要，嵌入 result dict。"""
        rs = self.runtime_state
        return {
            "turn_count": rs.turn_count,
            "edits": rs.edits_this_run,
            "last_edit_turn": rs.last_edit_turn,
            "has_diff": rs.has_diff,
            "diff_chars": rs.diff_chars,
            "broad_tests": rs.broad_test_attempts,
            "env_noise": rs.env_noise_seen,
            "task_complexity": rs.task_complexity(),
            "acceptance_criteria": rs.acceptance_criteria,
            "plan_generated": rs.plan_generated,
            "replan_count": rs.replan_count,
        }

    def _maybe_save_learnings(self, messages: list) -> None:
        """auto 模式下从对话历史提取经验，保存到持久 memory。

        默认保持同步规则提取；开启 MEMORY_LLM_EXTRACT 时可调用 LLM，开启
        MEMORY_ASYNC_WRITE 时后台写入，避免交互式响应被记忆提取阻塞。
        """
        if self.permissions.mode != "auto":
            return

        snapshot = [
            {"role": msg.get("role"), "content": msg.get("content", "")}
            for msg in messages
            if isinstance(msg, dict)
        ]

        def persist() -> None:
            from nz_coder.memory import extract_session_learnings, memory_mgr

            client = self.client if config.MEMORY_LLM_EXTRACT else None
            model = config.MODEL_ID if config.MEMORY_LLM_EXTRACT else None
            candidates = extract_session_learnings(snapshot, client=client, model=model)
            for c in candidates:
                result = memory_mgr.save(c["name"], c["description"], c["type"], c["content"])
                self.tracer.log("auto_save_learning", name=c["name"], result=result)
            if candidates:
                self._last_memory_query = ""
                self._last_memory_block = ""

        if config.MEMORY_ASYNC_WRITE:
            import threading

            threading.Thread(target=persist, daemon=True).start()
        else:
            persist()

    # ── API 调用层 ────────────────────────────────────────────────────────────

    def _call_streaming(self, api_messages: list, on_token=None):
        """Streaming LLM 调用。

        Returns:
            (content_text, tool_calls_list)    — 成功
            (None, None, diag_message)          — 400/422 客户端错误，注入诊断
            None                                — 不可恢复错误，终止
        """
        while True:
            content_parts = []
            reasoning_parts = []
            tool_calls_map = {}  # index -> {id, function: {name, arguments}}
            try:
                stream = self.client.chat.completions.create(
                    model=config.MODEL_ID,
                    messages=api_messages,
                    tools=get_specs(),
                    max_tokens=8000,
                    stream=True,
                )

                # 逐 chunk 积累流式响应
                for chunk in stream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta is None:
                        continue

                    # 文本内容
                    if delta.content:
                        content_parts.append(delta.content)
                        if on_token:
                            on_token(delta.content)
                    reasoning_content = _extract_model_field(delta, "reasoning_content")
                    if reasoning_content:
                        reasoning_parts.append(reasoning_content)

                    # 工具调用 delta：按 index 积累 name 和 arguments
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
                tool_calls_list = (
                    [tool_calls_map[i] for i in sorted(tool_calls_map)]
                    if tool_calls_map else []
                )
                extra = {}
                if reasoning_parts:
                    extra["reasoning_content"] = "".join(reasoning_parts)
                return LLMResult(content=content_text, tool_calls=tool_calls_list, extra=extra)

            except Exception as e:
                if content_parts and on_token:
                    on_token(f"\n[stream interrupted: {e}]\n")
                if _is_client_error(e):
                    self.tracer.log("api_error", count=1, error=str(e))
                    return LLMResult(diagnostic=self._make_client_error_diag(str(e)))
                if not self._handle_api_error(e):
                    return LLMResult(aborted=True)

    def _call_non_streaming(self, api_messages: list):
        """Non-streaming LLM 调用。

        Returns:
            (content_text, tool_calls_list)    — 成功
            (None, None, diag_message)          — 400/422 客户端错误，注入诊断
            None                                — 不可恢复错误，终止
        """
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
                tool_calls_list = [tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else []
                extra = {}
                reasoning_content = _extract_model_field(msg, "reasoning_content")
                if reasoning_content:
                    extra["reasoning_content"] = reasoning_content
                return LLMResult(content=msg.content or "", tool_calls=tool_calls_list, extra=extra)

            except Exception as e:
                if _is_client_error(e):
                    self.tracer.log("api_error", count=1, error=str(e))
                    return LLMResult(diagnostic=self._make_client_error_diag(str(e)))
                if not self._handle_api_error(e):
                    return LLMResult(aborted=True)

    def _make_client_error_diag(self, error_str: str) -> str:
        """生成 400/422 错误的诊断消息，注入对话让模型自行修正。"""
        return (
            "<api-error-diagnostic>\n"
            f"Your last request was rejected by the API with an error:\n{error_str}\n\n"
            "This usually means a tool call argument contained invalid JSON "
            "(e.g. unescaped quotes, raw newlines inside a string, trailing comma). "
            "Do NOT retry the same call. Instead:\n"
            "1. Use a simpler tool (e.g. replace_lines or edit_file instead of apply_patch).\n"
            "2. Keep string values short and avoid special characters.\n"
            "3. Verify any string containing quotes or backslashes is properly escaped.\n"
            "</api-error-diagnostic>"
        )

    def _handle_api_error(self, error) -> bool:
        """处理瞬态 API 错误（5xx / 限速 / 超时），带 backoff 重试。

        Returns False 表示应终止。
        注意：400/422 客户端错误应在调用方用 _is_client_error() 先过滤，不应到达这里。
        """
        error_info = self.recovery.record_error(error)
        self.tracer.log("api_error", count=error_info["count"], error=error_info["error"])
        if error_info["should_abort"]:
            return False
        self.recovery.backoff_wait()
        return True

    def _sanitize_messages(self, messages: list) -> list:
        """规范化消息列表，确保 API 兼容性。

        对标 Claude Code normalizeMessagesForAPI() 的关键子集：
        1. 剥离 reasoning_content 等非标准字段（某些 provider 不认识会报 400）
        2. 空 content 的 assistant 消息填充空字符串
        3. 过滤 whitespace-only 的 assistant 消息（API 拒绝空 content）
        4. 合并连续 user 消息（Bedrock 等不支持连续 user turns）
        5. 剥离孤立 tool_result（找不到对应 tool_use 的 tool_result，会触发 API 400）
        6. 记录 assistant 消息时间戳（供时间触发 micro_compact 使用）
        """
        now = time.time()

        # Step 1: 基础清理 — 填充空 content，剥离非标准字段，记录时间戳
        # reasoning_content: 某些 provider（DeepSeek/QwQ）支持并需要传回；
        # 其他 provider 收到会报 400。通过 PASS_REASONING_CONTENT 配置控制。
        _PASS_REASONING = getattr(config, "PASS_REASONING_CONTENT",
                                  True)  # default True 保持向后兼容
        _strip_extra = set() if _PASS_REASONING else {"reasoning_content"}
        base: list[dict] = []
        for msg in messages:
            role = msg.get("role", "")
            strip_keys = {"_timestamp"} | _strip_extra
            clean = {k: v for k, v in msg.items() if k not in strip_keys}
            if role == "assistant":
                if clean.get("content") is None:
                    clean["content"] = ""
                # 记录时间戳，供时间触发 micro_compact 使用
                clean["_timestamp"] = msg.get("_timestamp", now)
            base.append(clean)

        # Step 2: 收集所有有效 tool_use id（用于后面清理孤立 tool_result）
        valid_tool_use_ids: set[str] = set()
        for msg in base:
            if msg.get("role") == "assistant":
                for tc in (msg.get("tool_calls") or []):
                    tid = tc.get("id") or tc.get("tool_call_id")
                    if tid:
                        valid_tool_use_ids.add(tid)

        # Step 3: 过滤 + 合并
        result: list[dict] = []
        for msg in base:
            role = msg.get("role", "")

            # 过滤 whitespace-only assistant 消息
            if role == "assistant":
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")
                if isinstance(content, str) and not content.strip() and not tool_calls:
                    continue  # 空 assistant，跳过
                # 去掉时间戳再传给 API（内部字段不应发送）
                api_msg = {k: v for k, v in msg.items() if k != "_timestamp"}
                result.append(api_msg)
                continue

            # 剥离孤立 tool_result（对应的 tool_use 不存在）
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and tid not in valid_tool_use_ids:
                    continue  # 孤立，跳过
                result.append(msg)
                continue

            # user 消息：合并连续 user（diagnostic 消息 + tool_result 可能相邻）
            if role == "user":
                if result and result[-1].get("role") == "user":
                    # 合并到上一条 user 消息（追加换行分隔）
                    prev = result[-1]
                    prev_content = prev.get("content", "")
                    curr_content = msg.get("content", "")
                    if isinstance(prev_content, str) and isinstance(curr_content, str):
                        result[-1] = dict(prev, content=prev_content + "\n\n" + curr_content)
                    else:
                        # 非纯文本 content（含 tool_result blocks）：直接追加
                        result.append(msg)
                else:
                    result.append(msg)
                continue

            result.append(msg)

        return result


# ── Module-level helpers ──────────────────────────────────────────────────────

import concurrent.futures as _futures


def _build_context_layers(
    system_prompt: str,
    memory_block: str,
    state_block: str,
    scratch_block: str,
    max_tokens: int,
) -> tuple[str, str, dict]:
    """按固定/半固定/动态层构建上下文，并执行预算守卫。"""
    memory = memory_block or ""
    state = state_block or ""
    scratch = scratch_block or ""
    before = {
        "fixed_tokens": _estimate_text_tokens(system_prompt),
        "memory_tokens": _estimate_text_tokens(memory),
        "state_tokens": _estimate_text_tokens(state),
        "scratch_tokens": _estimate_text_tokens(scratch),
    }

    max_tokens = max(1000, int(max_tokens or 6000))
    if sum(before.values()) > max_tokens:
        if _estimate_text_tokens(scratch) > 1000:
            scratch = _truncate_text_tokens(scratch, 1000, keep_tail=True)
        used_without_memory = (
            _estimate_text_tokens(system_prompt)
            + _estimate_text_tokens(state)
            + _estimate_text_tokens(scratch)
        )
        memory_budget = max(0, max_tokens - used_without_memory)
        memory = _truncate_text_tokens(memory, memory_budget, keep_tail=False)

        used_without_scratch = (
            _estimate_text_tokens(system_prompt)
            + _estimate_text_tokens(state)
            + _estimate_text_tokens(memory)
        )
        scratch_budget = max(0, max_tokens - used_without_scratch)
        scratch = _truncate_text_tokens(scratch, scratch_budget, keep_tail=True)

        used_without_state = (
            _estimate_text_tokens(system_prompt)
            + _estimate_text_tokens(memory)
            + _estimate_text_tokens(scratch)
        )
        state_budget = max(0, max_tokens - used_without_state)
        state = _truncate_text_tokens(state, state_budget, keep_tail=False)

    stable_system = system_prompt + memory
    dynamic_parts = [part for part in (state, scratch) if part]
    dynamic_context = ""
    if dynamic_parts:
        dynamic_context = (
            "<context-injection>\n"
            "Runtime state and working memory for this turn. "
            "Treat as context, not as a new user request.\n"
            + "\n".join(dynamic_parts)
            + "\n</context-injection>"
        )

    after = {
        "fixed_tokens": _estimate_text_tokens(system_prompt),
        "memory_tokens": _estimate_text_tokens(memory),
        "state_tokens": _estimate_text_tokens(state),
        "scratch_tokens": _estimate_text_tokens(scratch),
    }
    return stable_system, dynamic_context, {
        "budget_tokens": max_tokens,
        "before_total_tokens": sum(before.values()),
        "after_total_tokens": sum(after.values()),
        **{f"before_{k}": v for k, v in before.items()},
        **{f"after_{k}": v for k, v in after.items()},
    }


def _inject_dynamic_context(messages: list[dict], dynamic_context: str) -> list[dict]:
    """把动态上下文放入首条 user 消息，保持 system prompt 前缀稳定。"""
    if not dynamic_context:
        return messages
    injected = [dict(msg) for msg in messages]
    for idx, msg in enumerate(injected):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            injected[idx] = dict(msg, content=dynamic_context + "\n\n" + content)
            return injected
    return [{"role": "user", "content": dynamic_context}, *injected]


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return ascii_chars // 4 + non_ascii_chars


def _truncate_text_tokens(text: str, max_tokens: int, keep_tail: bool = False) -> str:
    if not text or max_tokens <= 0:
        return ""
    if _estimate_text_tokens(text) <= max_tokens:
        return text
    marker = "\n[... truncated by context budget ...]\n"
    marker_tokens = _estimate_text_tokens(marker)
    payload_tokens = max(0, max_tokens - marker_tokens)
    if payload_tokens <= 0:
        return marker if max_tokens >= marker_tokens else ""
    max_chars = max(1, payload_tokens * 4)
    if keep_tail:
        return marker + text[-max_chars:]
    return text[:max_chars] + marker


def _execute_concurrent(executor, tool_calls_raw: list) -> list:
    """Execute read-only tool calls concurrently using a thread pool.

    Returns list of (index, tc, ToolExecutionResult) in the original order,
    preserving message insertion order for API correctness.

    Only called when all tools in the batch are non-write tools (has_write=False).
    Write tools must run sequentially to avoid shared-state races (transaction
    manager, change tracker, verification manager).
    """
    if len(tool_calls_raw) <= 1:
        return [(i, tc, executor.execute_one(tc, i)) for i, tc in enumerate(tool_calls_raw)]

    max_workers = min(len(tool_calls_raw), 4)  # cap at 4 to avoid hammering the FS
    with _futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(executor.execute_one, tc, i): (i, tc)
            for i, tc in enumerate(tool_calls_raw)
        }
        # Collect results preserving original order
        results_by_index = {}
        for fut in _futures.as_completed(futures):
            i, tc = futures[fut]
            try:
                result_r = fut.result()
            except Exception as exc:
                # Safety net: if a tool raises unexpectedly, surface as error
                from nz_coder.tool_executor import ToolExecutionResult
                fn_name = tool_calls_raw[i]["function"]["name"]
                result_r = ToolExecutionResult(
                    name=fn_name,
                    tool_input={},
                    output=f"Error: tool execution raised: {exc}",
                    executed=True,
                    dispatch_failed=True,
                    command_failed=False,
                    is_write=False,
                )
            results_by_index[i] = (i, tc, result_r)

    return [results_by_index[i] for i in range(len(tool_calls_raw))]


def _inject_missing_tool_results(messages: list) -> None:
    """Heal orphaned tool_calls left by a prior interrupted run.

    When the agent loop is interrupted (KeyboardInterrupt, timeout, etc.)
    after an assistant message with tool_calls was appended but before all
    tool role messages were written, the conversation history is in an illegal
    state: the API requires every tool_use block to have a matching tool_result.

    This function scans backwards from the end of messages, finds any assistant
    message whose tool_calls lack corresponding tool results, and injects
    synthetic error tool_results so the next API request is well-formed.

    No-op if the history is already healthy.
    """
    if not messages:
        return

    # Collect all tool_call_ids that already have a result
    answered: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if tid:
                answered.add(tid)

    # Find the last assistant message that has unanswered tool_calls
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            break  # last assistant had no tools → nothing to heal
        missing = [tc for tc in tool_calls if tc.get("id") not in answered]
        if not missing:
            break  # all tool_calls already answered
        # Inject synthetic error results for the missing ones
        for tc in missing:
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": (
                    "<interrupted>\n"
                    "This tool call was interrupted before it could complete. "
                    "The previous run was stopped mid-execution. "
                    "Please re-assess the current state of the workspace and continue.\n"
                    "</interrupted>"
                ),
            })
        break  # only heal the most recent assistant message


import re as _re


def _extract_last_user_text(messages: list) -> str:
    """Return the text of the most recent user message for memory query."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return msg["content"][:300]  # cap to avoid huge query tokens
    return ""


_KEEP_GOING_RE = _re.compile(
    r"^(?:please\s+)?(?:keep\s+going|go\s+on|继续|keep\s+working)$"
    r"|^continue$",
    _re.IGNORECASE,
)

_NEGATIVE_RE = _re.compile(
    r"\b(wtf|wth|ffs|shit|damn\s+it|broken|useless|terrible|awful|horrible"
    r"|what\s+the\s+(fuck|hell)|so\s+frustrating|this\s+sucks|screw\s+this"
    r"|不对|不行|不对劲|怎么回事|搞什么)\b",
    _re.IGNORECASE,
)


def _is_keep_going(messages: list) -> bool:
    """True if the most recent user message is a pure keep-going/continue signal.

    Only matches when the entire message (trimmed) is a continuation intent —
    e.g. "continue", "keep going", "继续". Messages with additional content
    (like "continue fixing the bug") are treated as new instructions.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            text = msg["content"].strip()
            return bool(_KEEP_GOING_RE.match(text))
    return False


def _last_user_has_frustration(messages: list) -> bool:
    """True if the most recent user message shows frustration."""
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return bool(_NEGATIVE_RE.search(msg["content"]))
    return False

_BUDGET_SHORTHAND_RE = _re.compile(
    r"(?:^|\s)\+(\d+(?:\.\d+)?)\s*(k|m)\b", _re.IGNORECASE
)
_BUDGET_VERBOSE_RE = _re.compile(
    r"\buse\s+(\d+(?:\.\d+)?)\s*(k|m)?\s*turns?\b", _re.IGNORECASE
)
_BUDGET_BARE_RE = _re.compile(
    r"(?:^|\s)\+(\d+)\s*turns?\b", _re.IGNORECASE
)

_MULTIPLIERS = {"k": 1_000, "m": 1_000_000}
# Shorthand token-scale hints (+50k, +1m) are interpreted as "give me a lot
# of turns". We map them to a capped turns value rather than the raw number,
# since 50,000 turns is nonsensical. The cap is 10× MAX_AGENT_TURNS.
_TOKEN_HINT_TURNS = 200  # turns granted when user says "+Nk/Nm"


def _parse_turn_budget(messages: list) -> int | None:
    """Extract a per-run turn budget from the last user message.

    Recognised patterns (case-insensitive):
      +50k  +1.5m         — token-scale hint → grants _TOKEN_HINT_TURNS turns
      use 100 turns        — explicit turn count
      use 2k turns         — turn count with multiplier (2000 turns)
      +30 turns            — bare turn count

    Returns the parsed integer capped at 10 × MAX_AGENT_TURNS, or None.
    Only the last user message is scanned.
    """
    last_user_text = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            last_user_text = msg["content"]
            break
    if not last_user_text:
        return None

    # Token-scale shorthand: +50k / +1.5m → generous but capped turn count
    if _BUDGET_SHORTHAND_RE.search(last_user_text):
        return min(_TOKEN_HINT_TURNS, config.MAX_AGENT_TURNS * 10)

    # Explicit turn count: "use 2k turns" or "use 100 turns"
    m = _BUDGET_VERBOSE_RE.search(last_user_text)
    if m:
        mult = _MULTIPLIERS.get((m.group(2) or "").lower(), 1)
        value = float(m.group(1)) * mult
        return min(max(1, int(value)), config.MAX_AGENT_TURNS * 10)

    # Bare count: "+30 turns"
    m = _BUDGET_BARE_RE.search(last_user_text)
    if m:
        return min(max(1, int(m.group(1))), config.MAX_AGENT_TURNS * 10)

    return None
