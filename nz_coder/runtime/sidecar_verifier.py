"""InfCodeX-style coding Sidecar Verifier contracts and pure gate logic."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from nz_coder.runtime.hooks import StopHookDecision
from nz_coder.runtime.llm_judge import (
    JudgeRequest,
    JudgeResponse,
    invoke_llm_judge,
)


ROLLING_BUFFER_SIZE = 24
ROUNDS_VERIFY_THRESHOLD = 10
TRIVIAL_LINES = 20

VERIFIER_SYSTEM_PROMPT = """You are a verification sidecar for an autonomous coding agent. A DIFFERENT agent (the "main agent") has just emitted what it considers its final answer for the user's current request. Your job is to do a second-pass judgment by reading the main agent's recent transcript + the file edits it made + the user's original ask.

# IMPORTANT — role separation

The transcript shown to you contains the MAIN AGENT's past messages and tool calls. You are NOT the author of those messages. You are a third-party observer judging whether that agent satisfied the user's request. Do not say "I edited the file" or "my reasoning" — the actions belong to the main agent. Your only action is to call `emit_sidecar_verdict` once.

# Three-state verdict

Call `emit_sidecar_verdict` with one of three verdict values:

## verdict = "accept"

The main agent's output satisfies the user's current ask:
- The text answer addresses what the user asked
- IF the task required code changes: the file edits shown actually implement what the agent claimed
- No obvious correctness issues in the diff (compile-breaking syntax, missing imports, wrong API usage)
- The agent did not hallucinate completion of work it never performed

A reasonable workaround that satisfies the user's stated ask is `accept`, not `revise`. When the agent explained why the literal approach was not viable and the workaround achieves the goal, accept it — do not penalize a valid divergence.

## verdict = "revise"

The main agent's output is missing the literal thing the user named in the current turn. Use revise when ONE more iteration could plausibly close a gap that the user actually asked about:
- A sub-requirement explicitly named in the user's ask was not satisfied
- The agent claimed completion but the file-edit summary contradicts the claim (intent-vs-action gap)
- The text answer is too vague where the user asked for specifics

Scope discipline (important — over-revising is a failure mode):
- If the user asked for feature X and the diff implements feature X (even imperfectly), that is `accept`, not `revise`. Hardening, cleanup, leak-prevention, and best-practice polish are NOT "missing pieces" — they are unrequested improvements.
- If the user named one call site and the agent edited only that call site, do NOT revise to ask for unrelated call sites.
- Do not revise to ask the agent to re-show or re-verify work the transcript already shows. Trust the transcript.

When you choose revise, populate `reason` with a concrete, actionable correction the main agent should make. The main agent will see this as a user message — write it like a user follow-up, not like a third-party report.

## verdict = "blocked"

The main agent has stopped because human input or external action is needed before another iteration can help:
- The agent stopped to ask the user a clarifying question
- Task requires resources or permissions the agent does not have
- The agent is fundamentally on the wrong track and revising will not recover

When you choose blocked, populate `reason` with what the user needs to do to unblock.

# Output format

Output ONLY the `emit_sidecar_verdict` tool call — no narration, no other tool calls, no free-form text."""

VERIFIER_REPORT_TOOL = {
    "name": "emit_sidecar_verdict",
    "description": "Report verification of the Main Agent final answer.",
    "parameters": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["accept", "revise", "blocked"],
            },
            "reason": {"type": "string"},
            "suggestedFix": {"type": "string"},
        },
        "required": ["verdict", "reason"],
        "additionalProperties": False,
    },
}

REVISE_RETROSPECTIVE = (
    "A previous attempt at this task failed Sidecar Verifier review. Treat "
    "prior failed todo items as ground truth: the same approach will not pass "
    "twice. Read the failure note before retrying and add a distinct todo when "
    "the correction requires a fundamentally different step."
)


@dataclass(frozen=True)
class VerifierContext:
    """Independent evidence packet for one natural-stop judgement."""

    current_turn_user_queries: tuple[str, ...]
    recent_transcript: tuple[dict[str, Any], ...]
    file_edit_summary: tuple[tuple[str, str], ...]
    last_assistant_text: str
    additional_criteria: str = ""


@dataclass(frozen=True)
class VerifierGateMetrics:
    """Objective task-scale facts that cannot be supplied by final narration."""

    risky_shell_ops: int = 0
    unattributed_write_ops: int = 0
    write_ops: int = 0
    files_changed: int = 0
    estimated_changed_lines: int = 0
    has_plan: bool = False
    rounds: int = 0
    any_tool_use: bool = False


@dataclass(frozen=True)
class VerifierVerdict:
    """Strict three-state verifier result with a diagnostic trace tag."""

    verdict: str
    reason: str
    suggested_fix: str = ""
    trace: str = "verifier_ok"

    def as_dict(self) -> dict[str, str]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "suggested_fix": self.suggested_fix,
            "trace": self.trace,
        }


@dataclass(frozen=True)
class ResolvedVerifierProvider:
    """Provider/client/model selected for one Agent-owned verifier."""

    provider: Any
    client: Any
    model: str
    provider_name: str
    source: str
    owns_client: bool = False


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _extract_current_turn_user_queries(
    transcript: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    queries: list[str] = []
    for message in reversed(transcript):
        role = message.get("role")
        if role == "assistant":
            if queries:
                break
            continue
        if role != "user" or bool(message.get("_nz_synthetic")):
            continue
        text = _message_text(message)
        if text.strip():
            queries.insert(0, text)
    return tuple(queries)


def build_verifier_context(
    transcript: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    last_assistant_text: str,
    *,
    file_edits: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    additional_criteria: str = "",
) -> VerifierContext:
    """Build the last-turn query, rolling transcript, and actual edit evidence."""
    filtered = [dict(message) for message in transcript if message.get("role") != "system"]
    recent = tuple(filtered[-ROLLING_BUFFER_SIZE:])
    edits = tuple(
        (
            str(item.get("path") or ""),
            str(item.get("diff_hint") or item.get("diffHint") or ""),
        )
        for item in file_edits
        if str(item.get("path") or "").strip()
    )
    return VerifierContext(
        current_turn_user_queries=_extract_current_turn_user_queries(transcript),
        recent_transcript=recent,
        file_edit_summary=edits,
        last_assistant_text=str(last_assistant_text or ""),
        additional_criteria=str(additional_criteria or ""),
    )


def _truncate(text: str, maximum: int) -> str:
    return text if len(text) <= maximum else text[:maximum] + "…[truncated]"


def _render_transcript(messages: tuple[dict[str, Any], ...]) -> str:
    lines: list[str] = []
    for message in messages:
        role = message.get("role")
        text = _message_text(message)
        if role == "user":
            lines.append(f"[USER]: {_truncate(text, 800)}")
        elif role == "assistant":
            if text:
                lines.append(f"[MAIN AGENT TEXT]: {_truncate(text, 800)}")
            for call in message.get("tool_calls", []) or []:
                function = call.get("function", {}) if isinstance(call, dict) else {}
                name = str(function.get("name") or "?")
                arguments = function.get("arguments", "")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                lines.append(
                    f"[MAIN AGENT TOOL]: {name}({_truncate(arguments, 300)})"
                )
    return "\n".join(lines)


def build_verifier_user_message(context: VerifierContext) -> str:
    """Render one third-person verifier message instead of prior assistant history."""
    sections = ["=== USER REQUEST (CURRENT TURN) ==="]
    sections.extend(
        context.current_turn_user_queries
        or ("(no current-turn user queries — evidence missing)",)
    )
    sections.extend(("", "=== RECENT MAIN AGENT TRANSCRIPT ==="))
    sections.append(_render_transcript(context.recent_transcript) or "(empty)")
    sections.extend(("", "=== FILE EDITS PERFORMED THIS TURN ==="))
    if context.file_edit_summary:
        sections.extend(
            f"- {path}: {_truncate(hint, 400)}"
            for path, hint in context.file_edit_summary
        )
    else:
        sections.append(
            "(no file edits — text-only response, OR the agent did not actually "
            "edit anything despite claiming it did)"
        )
    sections.extend((
        "",
        "=== MAIN AGENT FINAL TEXT (the answer the agent is delivering) ===",
        context.last_assistant_text or "(empty text response)",
    ))
    if context.additional_criteria.strip():
        sections.extend((
            "",
            "=== ADDITIONAL VERIFICATION CRITERIA ===",
            context.additional_criteria.strip(),
        ))
    sections.extend((
        "",
        "Now call `emit_sidecar_verdict` exactly once with verdict ∈ "
        "{accept, revise, blocked} and a `reason`. Remember: when "
        "verdict=revise, the `reason` becomes a synthetic user follow-up the "
        "main agent will see — write it as the user would.",
    ))
    return "\n".join(sections)


_GREETING = re.compile(
    r"^(你好|您好|嗨|嘿|早安|早上好|早|hi|hello|hey|thanks|thank\s*you|谢谢|"
    r"多谢|谢|byebye|bye|再见|拜拜|ok|okay|好的|好|嗯|哦|noted|got\s*it|sure|👋|🙏)",
    re.IGNORECASE,
)
_IMPERATIVE = re.compile(
    r"(?:^|\s|，|。|；|,)(查|读|看|找|搜|搜索|修|改|删|增|加|创|写|做|执行|"
    r"实现|完成|检查|审查|分析|诊断|测试|验证|确认|生成|创建|编译|构建|运行|部署|"
    r"run|fix|check|show|implement|build|debug|test|create|delete|find|search|"
    r"investigate|analyze|verify|generate|compile|deploy|install)",
    re.IGNORECASE,
)


def _transcript_has_tool_use(transcript: tuple[dict[str, Any], ...]) -> bool:
    last_real_user = -1
    for index, message in enumerate(transcript):
        if message.get("role") == "user" and not message.get("_nz_synthetic"):
            last_real_user = index
    return any(
        message.get("role") == "assistant" and bool(message.get("tool_calls"))
        for message in transcript[last_real_user + 1:]
    )


def compose_gate_decision(
    transcript: tuple[dict[str, Any], ...],
    metrics: VerifierGateMetrics,
    *,
    env: dict[str, str],
) -> tuple[bool, str]:
    """Apply InfCodeX FEATURE_196's first-match verifier fire gate."""
    if env.get("KODAX_VERIFIER_ALWAYS") == "1":
        return True, "escape-hatch"
    observable = (
        metrics.write_ops > 0
        or metrics.risky_shell_ops > 0
        or metrics.unattributed_write_ops > 0
        or metrics.has_plan
        or metrics.any_tool_use
        or _transcript_has_tool_use(transcript)
    )
    if observable:
        if metrics.risky_shell_ops > 0:
            return True, "risky-shell"
        if metrics.unattributed_write_ops > 0:
            return True, "unattributed-write"
        if metrics.has_plan:
            return True, "has-plan"
        if metrics.rounds > ROUNDS_VERIFY_THRESHOLD:
            return True, "long-run"
        if metrics.files_changed >= 2:
            return True, "multi-file"
        if metrics.estimated_changed_lines > TRIVIAL_LINES:
            return True, "large-edit"
        return False, "trivial-observed-work"
    real_users = [
        _message_text(message).strip()
        for message in transcript
        if message.get("role") == "user" and not message.get("_nz_synthetic")
    ]
    text = real_users[-1] if real_users else ""
    if (
        0 < len(text) <= 20
        and _GREETING.search(text)
        and not _IMPERATIVE.search(text)
    ):
        return False, "conversational-intent"
    return True, "default-fire"


def parse_verifier_report(block: dict[str, Any], exact: bool) -> VerifierVerdict:
    """Parse strict report input and degrade unsafe malformed verdicts to accept."""
    raw_input = block.get("input", {})
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    raw_verdict = str(tool_input.get("verdict") or "").strip().lower()
    if raw_verdict not in {"accept", "revise", "blocked"}:
        return VerifierVerdict("accept", "", trace="invalid_verdict_value")
    reason = str(tool_input.get("reason") or "").strip()
    if raw_verdict in {"revise", "blocked"} and not reason:
        return VerifierVerdict("accept", "", trace="missing_reason")
    return VerifierVerdict(
        raw_verdict,
        reason,
        suggested_fix=str(
            tool_input.get("suggestedFix") or tool_input.get("suggested_fix") or ""
        ).strip(),
        trace="verifier_ok" if exact else "fuzzy_tool_match",
    )


def map_verdict_to_stop_decision(verdict: VerifierVerdict) -> StopHookDecision:
    """Land accept/revise/blocked on the Agent Core three-state StopHook surface."""
    if verdict.verdict == "revise":
        return StopHookDecision(
            action="reanimate",
            message=f"{verdict.reason}\n\n{REVISE_RETROSPECTIVE}",
            source="sidecar-verifier",
        )
    if verdict.verdict == "blocked":
        return StopHookDecision(
            action="abort",
            message=verdict.reason,
            source="sidecar-verifier",
        )
    return StopHookDecision()


def _field(owner: Any, name: str, default: Any = None) -> Any:
    if isinstance(owner, dict):
        return owner.get(name, default)
    return getattr(owner, name, default)


def _provider_judge_response(response: Any) -> JudgeResponse:
    choices = _field(response, "choices", []) or []
    message = _field(choices[0], "message") if choices else None
    tool_blocks: list[dict[str, Any]] = []
    for call in _field(message, "tool_calls", []) or []:
        function = _field(call, "function", {}) or {}
        name = str(_field(function, "name", "") or "")
        raw_arguments = _field(function, "arguments", {})
        if isinstance(raw_arguments, str):
            try:
                tool_input = json.loads(raw_arguments)
            except json.JSONDecodeError:
                tool_input = {}
        else:
            tool_input = raw_arguments if isinstance(raw_arguments, dict) else {}
        tool_blocks.append({"name": name, "input": tool_input})
    content = str(_field(message, "content", "") or "").strip()
    if not tool_blocks and content:
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            tool_blocks.append({
                "name": "emit_sidecar_verdict",
                "input": payload,
            })
    return JudgeResponse(tool_blocks=tuple(tool_blocks), text=content)


def _default_verifier_verdict(reason: str) -> VerifierVerdict:
    trace = reason if reason in {
        "provider_error",
        "timeout",
        "cancelled",
        "no_tool_call",
    } else "no_tool_call"
    return VerifierVerdict("accept", "", trace=trace)


def invoke_sidecar_verifier(
    *,
    provider: Any,
    client: Any,
    model: str,
    context: VerifierContext,
    timeout_seconds: float = 15.0,
    cancel_event=None,
) -> VerifierVerdict:
    """Invoke one isolated forced verdict request through the NZ Provider."""
    from nz_coder.runtime.model_gateway import (
        ModelCall,
        ModelCallPurpose,
        ModelCallStatus,
        ModelSelectionRequest,
        ProductionModelGateway,
        resolve_model_runtime,
    )

    request = JudgeRequest(
        system_prompt=VERIFIER_SYSTEM_PROMPT,
        user_message=build_verifier_user_message(context),
        report_tool={
            "type": "function",
            "function": VERIFIER_REPORT_TOOL,
        },
        report_tool_name="emit_sidecar_verdict",
        max_output_tokens=1024,
    )
    runtime = resolve_model_runtime(ModelSelectionRequest(
        provider_name=str(getattr(provider, "name", "") or ""),
        model_id=model,
        provider=provider,
        client=client,
        owns_client=False,
    ))
    gateway = ProductionModelGateway(runtime, max_retries=1)

    def invoke(judge_request: JudgeRequest) -> JudgeResponse:
        outcome = gateway.complete_sync(ModelCall(
            purpose=ModelCallPurpose.VERIFIER,
            messages=[
                {"role": "system", "content": judge_request.system_prompt},
                {"role": "user", "content": judge_request.user_message},
            ],
            tools=[judge_request.report_tool],
            tool_choice={
                "type": "function",
                "function": {"name": judge_request.report_tool_name},
            },
            max_output_tokens=judge_request.max_output_tokens,
            timeout_seconds=timeout_seconds,
            capability_options={"stream": False},
        ), cancel_event=cancel_event)
        if outcome.status is not ModelCallStatus.COMPLETED:
            raise RuntimeError(outcome.error or outcome.status.value)
        tool_blocks = tuple({
                "name": str(call.get("function", {}).get("name") or ""),
                "input": _parse_tool_arguments(
                    call.get("function", {}).get("arguments")
                ),
            } for call in outcome.tool_calls)
        content = outcome.content.strip()
        if not tool_blocks and content:
            candidate = content
            if candidate.startswith("```"):
                candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                tool_blocks = ({
                    "name": judge_request.report_tool_name,
                    "input": payload,
                },)
        return JudgeResponse(tool_blocks=tool_blocks, text=content)

    return invoke_llm_judge(
        request=request,
        invoke=invoke,
        parse_tool_call=parse_verifier_report,
        default_verdict=_default_verifier_verdict,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    )


def _parse_tool_arguments(value: Any) -> dict:
    """Normalize a Gateway tool-call argument payload for the verifier parser."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def resolve_verifier_provider(
    *,
    main_provider: Any,
    main_client: Any,
    main_model: str,
    env: dict[str, str] | None = None,
    provider_factory: Callable[[str], Any] | None = None,
) -> ResolvedVerifierProvider:
    """Resolve a paired explicit override, otherwise inherit the Main Agent."""
    selected_env = dict(os.environ) if env is None else dict(env)
    provider_name = str(selected_env.get("KODAX_VERIFIER_PROVIDER") or "").strip()
    model = str(selected_env.get("KODAX_VERIFIER_MODEL") or "").strip()
    if provider_name and model:
        if provider_factory is None:
            from nz_coder.providers import create_provider

            provider_factory = create_provider
        try:
            explicit_provider = provider_factory(provider_name)
            if explicit_provider is not None:
                explicit_client = explicit_provider.create_client()
                return ResolvedVerifierProvider(
                    provider=explicit_provider,
                    client=explicit_client,
                    model=model,
                    provider_name=provider_name,
                    source="explicit-env",
                    owns_client=True,
                )
        except Exception:
            pass
    return ResolvedVerifierProvider(
        provider=main_provider,
        client=main_client,
        model=str(main_model or ""),
        provider_name=str(getattr(main_provider, "name", "") or "unknown"),
        source="inherit-main",
        owns_client=False,
    )


def _changed_line_count(diff: str) -> int:
    return sum(
        1
        for line in str(diff or "").splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


class SidecarVerifierHook:
    """Agent-owned async StopHook backed by an isolated verifier request."""

    def __init__(
        self,
        loop: Any,
        resolved: ResolvedVerifierProvider,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._loop = loop
        self._resolved = resolved
        self._env = dict(os.environ) if env is None else dict(env)
        self._timeout_seconds = timeout_seconds
        self.stats: dict[str, Any] = {
            "fire_count": 0,
            "skip_count": 0,
            "verdict_counts": {"accept": 0, "revise": 0, "blocked": 0},
            "last_gate_reason": "",
            "last_trace": "",
        }

    def _evidence(self, context) -> tuple[VerifierContext, VerifierGateMetrics]:
        tracker = getattr(self._loop, "change_tracker", None)
        changed: list[str] = []
        deleted: list[str] = []
        diff = ""
        if tracker is not None:
            try:
                changed = list(tracker.current_changed_paths())
                deleted = list(tracker.current_deleted_paths())
                diff = str(tracker.render_current_diff() or "")
            except Exception:
                changed, deleted, diff = [], [], ""
        paths = list(dict.fromkeys(changed + deleted))
        changed_lines = _changed_line_count(diff)
        hint = f"tracked change; run diff contains {changed_lines} changed line(s)"
        file_edits = [
            {"path": path, "diff_hint": ("deleted; " if path in deleted else "") + hint}
            for path in paths
        ]
        state = context.runtime_state if isinstance(context.runtime_state, dict) else {}
        criteria = state.get("acceptance_criteria") or []
        verifier_context = build_verifier_context(
            context.transcript,
            context.last_assistant_text,
            file_edits=file_edits,
            additional_criteria="\n".join(str(item) for item in criteria if str(item).strip()),
        )
        metrics = VerifierGateMetrics(
            risky_shell_ops=max(
                0,
                int(getattr(self._loop, "_sidecar_risky_shell_ops", 0) or 0),
            ),
            unattributed_write_ops=max(
                0,
                int(getattr(self._loop, "_sidecar_unattributed_write_ops", 0) or 0),
            ),
            write_ops=max(0, int(state.get("edits_this_run", 0) or 0)),
            files_changed=len(paths),
            estimated_changed_lines=changed_lines,
            has_plan=bool(state.get("plan_generated")),
            rounds=max(0, int(state.get("turn_count", 0) or 0)),
            any_tool_use=_transcript_has_tool_use(context.transcript),
        )
        return verifier_context, metrics

    async def __call__(self, context) -> StopHookDecision:
        verifier_context, metrics = self._evidence(context)
        fire, gate_reason = compose_gate_decision(
            context.transcript,
            metrics,
            env=self._env,
        )
        self.stats["last_gate_reason"] = gate_reason
        tracer = getattr(self._loop, "tracer", None)
        if tracer is not None:
            tracer.log(
                "sidecar_gate_decision",
                fire=fire,
                reason=gate_reason,
                files_changed=metrics.files_changed,
                changed_lines=metrics.estimated_changed_lines,
                rounds=metrics.rounds,
            )
        if not fire:
            self.stats["skip_count"] += 1
            return StopHookDecision()

        self.stats["fire_count"] += 1
        started = time.monotonic()
        if tracer is not None:
            tracer.log(
                "sidecar_started",
                provider=self._resolved.provider_name,
                model=self._resolved.model,
                source=self._resolved.source,
            )
        cancel_event = threading.Event()
        try:
            verdict = await asyncio.to_thread(
                invoke_sidecar_verifier,
                provider=self._resolved.provider,
                client=self._resolved.client,
                model=self._resolved.model,
                context=verifier_context,
                timeout_seconds=self._timeout_seconds,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except Exception:
            verdict = VerifierVerdict("accept", "", trace="provider_error")
        self.stats["last_trace"] = verdict.trace
        self.stats["verdict_counts"][verdict.verdict] += 1
        if tracer is not None:
            tracer.log(
                "sidecar_finished",
                verdict=verdict.verdict,
                trace=verdict.trace,
                reason=verdict.reason,
                elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                provider=self._resolved.provider_name,
                model=self._resolved.model,
                source=self._resolved.source,
            )
        return map_verdict_to_stop_decision(verdict)

    def close(self) -> None:
        """Close only a verifier client created by an explicit env override."""
        if not self._resolved.owns_client:
            return
        close = getattr(self._resolved.client, "close", None)
        if callable(close):
            close()


def create_sidecar_verifier_hook(
    loop: Any,
    resolved: ResolvedVerifierProvider,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 15.0,
) -> SidecarVerifierHook:
    """Create the production StopHook while retaining a direct test seam."""
    return SidecarVerifierHook(
        loop,
        resolved,
        env=env,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "REVISE_RETROSPECTIVE",
    "VERIFIER_REPORT_TOOL",
    "VERIFIER_SYSTEM_PROMPT",
    "VerifierContext",
    "VerifierGateMetrics",
    "VerifierVerdict",
    "build_verifier_context",
    "build_verifier_user_message",
    "compose_gate_decision",
    "create_sidecar_verifier_hook",
    "invoke_sidecar_verifier",
    "map_verdict_to_stop_decision",
    "parse_verifier_report",
    "resolve_verifier_provider",
]
