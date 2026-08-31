"""InfCodeX-style coding Sidecar Verifier contracts and pure gate logic."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.verification.hooks import StopHookDecision
from nz_coder.runtime.verification.llm_judge import (
    JudgeRequest,
    JudgeResponse,
    invoke_llm_judge,
)
from nz_coder.runtime.verification.verification_contract import effective_acceptance_generation


ROLLING_BUFFER_SIZE = 24
ROUNDS_VERIFY_THRESHOLD = 10
TRIVIAL_LINES = 20
VERIFIER_DIFF_MAX_EACH = 8000
VERIFIER_DIFF_MAX_TOTAL = 12000

SEMANTIC_CONTRACT_CERTIFICATION = """SEMANTIC CONTRACT CERTIFICATION MODE
- Audit every explicit requirement and constraint independently against the actual diff. An explicit compatibility, preservation, regression, security, or data-integrity promise is a blocking requirement, not optional hardening.
- The permissive "feature X even imperfectly" rule applies only to incidental quality not named by the user. It never permits a missing or contradicted explicit sub-requirement.
- Do not infer compatibility or semantic correctness from the main agent narration or from a passing test command alone. Check whether tests actually exercise each promised behavior.
- A newly added test cannot prove compatibility when it assigns new behavior to a legacy input; that is regression evidence unless the user requested the semantic change.
- Check interactions between explicitly requested syntaxes or behaviors, not only each feature in isolation.
- An `Exact acceptance (passed-current-generation)` line and its trusted output are Runtime-owned execution facts. Do not request that command be re-run or re-shown. This fact proves execution only; continue auditing semantic compatibility from the diff and targeted evidence.
- If the evidence is insufficient to prove every hard requirement, return revise with one concrete counterexample or missing check."""

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
- Added or changed API calls follow the exact local registration and validation pattern; method-name differences such as public versus internal registration surfaces can be correctness-critical
- The patch does not bypass uniqueness or integrity conflicts by deleting existing persisted data unless the user's request explicitly authorizes that data loss and the repository proves the records are disposable duplicates

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


def _truncate_inside_budget(text: str, maximum: int, marker: str) -> str:
    """Truncate text while counting the evidence marker inside *maximum*."""
    if maximum <= 0:
        return ""
    if len(text) <= maximum:
        return text
    if maximum <= len(marker):
        return marker[:maximum]
    return text[:maximum - len(marker)].rstrip() + marker


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
        remaining = VERIFIER_DIFF_MAX_TOTAL
        for path, hint in context.file_edit_summary:
            if remaining <= 0:
                sections.append(f"- {path}: (diff evidence omitted: budget exhausted)")
                continue
            limit = min(VERIFIER_DIFF_MAX_EACH, remaining)
            bounded = _truncate_inside_budget(hint, limit, "…[truncated]")
            sections.append(f"- {path}: {bounded}")
            remaining -= len(bounded)
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
_NO_TOOL_REQUEST = re.compile(
    r"(?:do\s+not|don't|without)\s+(?:call|use|invoke)(?:\s+any)?\s+tools?"
    r"|(?:不要|无需|不必|禁止).{0,8}(?:调用|使用).{0,4}工具",
    re.IGNORECASE,
)
_NO_MUTATION_REQUEST = re.compile(
    r"(?:do\s+not|don't|without)\s+(?:modify|edit|change|write)(?:\s+any)?\s+files?"
    r"|(?:不要|无需|不必|禁止).{0,8}(?:修改|编辑|改动|写入).{0,4}文件",
    re.IGNORECASE,
)
_HISTORY_REPORT_INTENT = re.compile(
    r"report|summari[sz]e|recap|复述|汇报|总结|回顾",
    re.IGNORECASE,
)
_HISTORY_REFERENCE = re.compile(
    r"previous|prior|earlier|already\s+obtained|history|上一(?:轮|次)|上次|之前|"
    r"此前|已经(?:获得|得到)|历史",
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


def _is_grounded_history_report(
    transcript: tuple[dict[str, Any], ...],
    text: str,
) -> bool:
    """Recognize an explicitly read-only report backed by an earlier tool turn."""
    last_real_user = -1
    for index, message in enumerate(transcript):
        if message.get("role") == "user" and not message.get("_nz_synthetic"):
            last_real_user = index
    prior_tool_evidence = any(
        message.get("role") == "assistant" and bool(message.get("tool_calls"))
        for message in transcript[:last_real_user]
    )
    return bool(
        prior_tool_evidence
        and _NO_TOOL_REQUEST.search(text)
        and _NO_MUTATION_REQUEST.search(text)
        and _HISTORY_REPORT_INTENT.search(text)
        and _HISTORY_REFERENCE.search(text)
        and not _IMPERATIVE.search(text)
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
    if _is_grounded_history_report(transcript, text):
        return False, "grounded-history-report"
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


def _merge_compatibility_risk(
    verdict: VerifierVerdict,
    compatibility_risk: str,
) -> VerifierVerdict:
    """Expose every deterministic risk in the same bounded repair request."""
    risk = str(compatibility_risk or "").strip()
    if not risk or verdict.verdict == "blocked":
        return verdict
    reasons = [risk]
    model_reason = str(verdict.reason or "").strip()
    if verdict.verdict == "revise" and model_reason and model_reason not in risk:
        reasons.append("Additional verifier finding:\n" + model_reason)
    return VerifierVerdict(
        "revise",
        "\n\n".join(reasons),
        suggested_fix=verdict.suggested_fix,
        trace="deterministic_compatibility_guard",
    )


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


def _verifier_capability_options(runtime: Any) -> dict[str, Any]:
    """Keep structured verifier calls bounded on known reasoning models."""
    options: dict[str, Any] = {"stream": False}
    capabilities = getattr(runtime, "capabilities", None)
    family = str(getattr(capabilities, "family", "") or "").casefold()
    model_id = str(getattr(runtime, "model_id", "") or "").casefold()
    if family == "deepseek" and "deepseek-v4" in model_id:
        options["extra_body"] = {"thinking": {"type": "disabled"}}
    return options


def invoke_sidecar_verifier(
    *,
    provider: Any,
    client: Any,
    model: str,
    context: VerifierContext,
    timeout_seconds: float = 15.0,
    cancel_event=None,
    observer: Callable[[str, dict], None] | None = None,
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
    gateway = ProductionModelGateway(
        runtime,
        max_retries=1,
        observer=observer,
    )

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
            capability_options=_verifier_capability_options(runtime),
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


def _semantic_review_pending(runtime_state: dict[str, Any]) -> bool:
    """Return whether the persisted ledger lacks only semantic evidence."""
    raw = runtime_state.get("requirement_ledger")
    if not isinstance(raw, dict) or not raw:
        return False
    try:
        from nz_coder.runtime.agent.task_contract import RequirementLedger

        return RequirementLedger.from_dict(raw).semantic_review_pending_only()
    except (TypeError, ValueError):
        return False


def _persistent_data_deletion_signals(
    runtime_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return review signals that require a data-integrity judgment."""
    patch_risk = runtime_state.get("patch_risk")
    if not isinstance(patch_risk, dict):
        return []
    signals = patch_risk.get("risk_signals")
    if not isinstance(signals, list):
        return []
    return [
        item
        for item in signals
        if isinstance(item, dict)
        and item.get("category") == "persistent_data_deletion"
    ]


def _render_contract_criteria(runtime_state: dict[str, Any]) -> list[str]:
    """Render task-owned verification criteria from persisted runtime state."""
    lines: list[str] = []
    current_round = str(
        runtime_state.get("current_round_instruction_text") or ""
    ).strip()
    if current_round:
        lines.append(
            "Current round instruction: " + _truncate(current_round, 2000)
        )
    contract = runtime_state.get("task_contract")
    if isinstance(contract, dict):
        objective = str(contract.get("objective") or "").strip()
        if objective:
            lines.append(f"Task objective: {objective}")
        for raw in contract.get("requirements") or []:
            if not isinstance(raw, dict):
                continue
            requirement_id = str(raw.get("id") or "?").strip()
            kind = str(raw.get("kind") or "behavior").strip()
            description = str(raw.get("description") or "").strip()
            if description:
                lines.append(
                    f"Requirement {requirement_id} [{kind}]: {description}"
                )
            for evidence in raw.get("required_evidence") or []:
                value = str(evidence or "").strip()
                if value:
                    lines.append(f"Required evidence: {value}")
        for constraint in contract.get("constraints") or []:
            value = str(constraint or "").strip()
            if value:
                lines.append(f"Constraint: {value}")
    verification = runtime_state.get("verification_contract")
    if isinstance(verification, dict):
        command = str(verification.get("command") or "").strip()
        generation = effective_acceptance_generation(runtime_state)
        if command:
            current = bool(
                verification.get("passed") is True
                and int(verification.get("attempted_generation", -1)) == generation
            )
            state = "passed-current-generation" if current else "not-current"
            lines.append(f"Exact acceptance ({state}): {command}")
            output = str(verification.get("output") or "").strip()
            if current and output:
                lines.append(
                    "Trusted exact-acceptance output: "
                    + _truncate(output, 1200)
                )
    return lines


def _rank_semantic_paths(
    paths: list[str],
    runtime_state: dict[str, Any],
) -> list[str]:
    """Put contract-owned source artifacts before tests, docs, and logs."""
    expected: set[str] = set()
    contract = runtime_state.get("task_contract")
    if isinstance(contract, dict):
        for raw in contract.get("requirements") or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("kind") or "") not in {"behavior", "compatibility"}:
                continue
            expected.update(
                str(item).replace("\\", "/")
                for item in raw.get("expected_artifacts") or []
                if str(item).strip()
            )
    expected_stems = {
        item.rsplit("/", 1)[-1].rsplit(".", 1)[0].casefold()
        for item in expected
    }

    def rank(path: str) -> tuple[int, int, str]:
        normalized = str(path).replace("\\", "/")
        lowered = normalized.casefold()
        basename = lowered.rsplit("/", 1)[-1]
        related_test = int(not any(
            basename.startswith(f"test_{stem}.")
            or basename == f"test_{stem}.py"
            for stem in expected_stems
        ))
        if normalized in expected:
            priority = 0
        elif "/test" in lowered or basename.startswith("test_"):
            priority = 2
        elif basename.startswith(("readme", "changelog")) or lowered.endswith(
            (".md", ".rst", ".txt")
        ):
            priority = 3
        elif lowered.endswith((".jsonl", ".log", ".trace")):
            priority = 4
        else:
            priority = 1
        return priority, related_test, normalized

    return sorted(dict.fromkeys(paths), key=rank)


def _diff_sections(diff: str) -> list[str]:
    """Split one unified diff into stable per-file sections."""
    text = str(diff or "")
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", text)]
    if not starts:
        return [text] if text else []
    return [
        text[start:(starts[index + 1] if index + 1 < len(starts) else len(text))]
        for index, start in enumerate(starts)
    ]


def _compatibility_delta_evidence(
    diff: str,
    paths: list[str],
    *,
    max_total: int = 10000,
) -> str:
    """Extract compact before/after deltas that can invalidate compatibility."""
    sections = _diff_sections(diff)
    if not sections:
        return ""
    blocks: list[str] = []
    remaining = max_total
    for path in paths:
        if remaining <= 0:
            break
        normalized = str(path).replace("\\", "/")
        lowered = normalized.casefold()
        is_test = "/test" in lowered or lowered.rsplit("/", 1)[-1].startswith(
            "test_"
        )
        is_docs = lowered.endswith((".md", ".rst", ".txt"))
        if is_docs or lowered.endswith((".jsonl", ".log", ".trace")):
            continue
        section = next(
            (
                item for item in sections
                if f"a/{normalized}" in item or f"b/{normalized}" in item
            ),
            "",
        )
        if not section:
            continue
        selected: list[str] = []
        hunks = re.split(r"(?m)(?=^@@ )", section)
        for hunk in hunks[1:]:
            changed = [
                line for line in hunk.splitlines()
                if (line.startswith("+") and not line.startswith("+++"))
                or (line.startswith("-") and not line.startswith("---"))
            ]
            if not changed:
                continue
            removed = any(line.startswith("-") for line in changed)
            test_signal = is_test and any(
                "test" in line.casefold() or "assert" in line.casefold()
                for line in changed
            )
            if removed or test_signal:
                selected.extend(changed)
        if not selected:
            continue
        rendered = f"[{normalized}]\n" + "\n".join(selected)
        rendered = _truncate(rendered, remaining)
        blocks.append(rendered)
        remaining -= len(rendered)
    return "\n\n".join(blocks)


_INPUT_GATING_MARKER = re.compile(
    r"(?:is_name|named|name_(?:token|syntax|input)|token_(?:kind|type|value)|"
    r"input_(?:kind|type|syntax)|prefix|suffix|literal|pattern|format)",
    re.IGNORECASE,
)


def _broad_compatibility_relaxation(
    diff: str,
    paths: list[str],
) -> str:
    """Detect a legacy guard relaxed by a field-wide rather than input gate."""
    sections = _diff_sections(diff)
    for path in paths:
        normalized = str(path).replace("\\", "/")
        lowered = normalized.casefold()
        basename = lowered.rsplit("/", 1)[-1]
        if (
            "/test" in lowered
            or basename.startswith("test_")
            or lowered.endswith((".md", ".rst", ".txt", ".jsonl", ".log"))
        ):
            continue
        section = next(
            (
                item for item in sections
                if f"a/{normalized}" in item or f"b/{normalized}" in item
            ),
            "",
        )
        if not section:
            continue
        removed = [
            line[1:].strip()
            for line in section.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        added = [
            line[1:].strip()
            for line in section.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        assignments: dict[str, str] = {}
        for line in added:
            match = re.match(r"([A-Za-z_]\w*)\s*=\s*(.+)$", line)
            if match:
                assignments[match.group(1)] = match.group(2)
        aliases_by_expression: dict[str, set[str]] = {}
        for name, expression in assignments.items():
            normalized_expression = re.sub(r"\s+", "", expression)
            aliases_by_expression.setdefault(normalized_expression, set()).add(name)

        def input_specific(expression: str) -> bool:
            if _INPUT_GATING_MARKER.search(expression):
                return True
            return any(
                _INPUT_GATING_MARKER.search(assignments.get(identifier, ""))
                for identifier in re.findall(r"\b[A-Za-z_]\w*\b", expression)
            )

        for old_line in removed:
            old_match = re.match(r"if\s+(.+?)\s*:\s*$", old_line)
            if not old_match:
                continue
            old_condition = old_match.group(1)
            normalized_old = re.sub(r"\s+", "", old_condition)
            aliases = aliases_by_expression.get(normalized_old, set())
            for new_line in added:
                new_match = re.match(r"if\s+(.+?)\s*:\s*$", new_line)
                if not new_match:
                    continue
                new_condition = new_match.group(1)
                normalized_new = re.sub(r"\s+", "", new_condition)
                preserves_old_guard = normalized_old in normalized_new or any(
                    re.search(rf"\b{re.escape(alias)}\b", new_condition)
                    for alias in aliases
                )
                if not preserves_old_guard:
                    continue
                relaxation = re.search(
                    r"\band\s+not\s+\(?\s*([A-Za-z_]\w*)",
                    new_condition,
                )
                if relaxation is None:
                    continue
                gate = relaxation.group(1)
                gate_definition = assignments.get(gate, "")
                if input_specific(gate) or input_specific(gate_definition):
                    continue
                return (
                    "Detected broad compatibility relaxation in "
                    f"{normalized}: legacy guard `if {old_condition}:` is "
                    f"bypassed by `if {new_condition}:` through `{gate}`, which "
                    "is not derived from the new input syntax. Preserve legacy "
                    "inputs with an input-specific gate and add a targeted "
                    "before/after compatibility probe."
                )

        # Another common rewrite moves the legacy rejection into ``else``
        # and puts a positive field-wide gate in front of the new behavior:
        #
        #   - if lo > hi: reject()
        #   + if allow_wrap and lo > hi: expand_wrap()
        #   + else:
        #   +     if lo > hi: reject()
        #
        # The old condition is still visible, but every input in the field
        # now enters the feature branch.  Only a condition derived from the
        # newly introduced token syntax (for example both endpoints being
        # names) is a valid compatibility gate.
        introduces_wrap_expansion = bool(
            re.search(
                r"list\s*\(\s*range\s*\([^\n]+\)\s*\)\s*\+\s*"
                r"list\s*\(\s*range\s*\(",
                "\n".join(added),
            )
            or re.search(r"\bexpand_?wrap\s*\(", "\n".join(added), re.IGNORECASE)
        )
        if introduces_wrap_expansion:
            for old_line in removed:
                old_match = re.match(r"if\s+(.+?)\s*:\s*$", old_line)
                if old_match is None:
                    continue
                old_condition = old_match.group(1).strip()
                normalized_old = re.sub(r"\s+", "", old_condition)
                for new_line in added:
                    new_match = re.match(r"if\s+(.+?)\s*:\s*$", new_line)
                    if new_match is None:
                        continue
                    new_condition = new_match.group(1).strip()
                    normalized_new = re.sub(r"\s+", "", new_condition)
                    if (
                        normalized_new == normalized_old
                        or normalized_old not in normalized_new
                        or not re.search(r"\b(?:and|or)\b", new_condition)
                        or input_specific(new_condition)
                    ):
                        continue
                    return (
                        "Detected broad compatibility relaxation in "
                        f"{normalized}: legacy guard `if {old_condition}:` is "
                        f"now handled by field-wide branch `if {new_condition}:`. "
                        "The added gate is not derived from the new input syntax, "
                        "so legacy inputs can enter the new wrap behavior. Preserve "
                        "the old rejection with an input-specific gate and add a "
                        "targeted before/after compatibility probe."
                    )

        # Also catch a deleted unconditional guard reintroduced with a
        # positive field-wide branch and the old rejection in `else`.
        # This is the structural twin of `if old and not gate`: it permits all
        # legacy inputs in that field rather than only the newly added syntax.
        raw_lines = section.splitlines()
        removed_bodies = {
            line[1:].strip()
            for line in raw_lines
            if line.startswith("-") and not line.startswith("---")
        }
        for old_line in removed:
            old_match = re.match(r"if\s+(.+?)\s*:\s*$", old_line)
            if old_match is None:
                continue
            old_condition = old_match.group(1)
            normalized_old = re.sub(r"\s+", "", old_condition)
            for index, line in enumerate(raw_lines):
                if not line.startswith("+") or line.startswith("+++"):
                    continue
                outer_body = line[1:]
                outer = re.match(r"\s*if\s+(.+?)\s*:\s*$", outer_body)
                if outer is None or re.sub(r"\s+", "", outer.group(1)) != normalized_old:
                    continue
                outer_indent = len(outer_body) - len(outer_body.lstrip())
                nested_gate = ""
                repeated_rejection = ""
                for following in raw_lines[index + 1:index + 14]:
                    if following.startswith("@@ "):
                        break
                    if not following.startswith("+") or following.startswith("+++"):
                        continue
                    candidate = following[1:]
                    candidate_indent = len(candidate) - len(candidate.lstrip())
                    if candidate_indent <= outer_indent:
                        break
                    nested = re.match(r"\s*if\s+(.+?)\s*:\s*$", candidate)
                    if nested is not None and not nested_gate:
                        nested_gate = nested.group(1).strip()
                    if candidate.strip() in removed_bodies:
                        repeated_rejection = candidate.strip()
                if (
                    not nested_gate
                    or not repeated_rejection
                    or input_specific(nested_gate)
                ):
                    continue
                return (
                    "Detected broad compatibility relaxation in "
                    f"{normalized}: legacy guard `if {old_condition}:` now "
                    f"permits inputs through nested field gate `if {nested_gate}:` "
                    f"and relegates the prior rejection `{repeated_rejection}` "
                    "to a fallback branch. The gate is not derived from the new "
                    "input syntax; preserve legacy inputs and add a targeted "
                    "before/after compatibility probe."
                )

        # A unified diff can retain the old outer guard as context while
        # moving its rejecting body underneath a new nested gate:
        #
        #   if legacy_invalid:
        # -     reject()
        # +     if not broad_feature_flag:
        # +         reject()
        #
        # The first detector cannot see this shape because the legacy `if`
        # line was not deleted. Reconstruct the local before/after body and
        # reject the same field-wide bypass unless it proves new input syntax.
        for index, line in enumerate(raw_lines):
            if not line.startswith("+") or line.startswith("+++"):
                continue
            added_body = line[1:]
            nested = re.match(r"\s*if\s+not\s+(.+?)\s*:\s*$", added_body)
            if nested is None:
                continue
            gate_expression = nested.group(1).strip()
            if input_specific(gate_expression):
                continue
            gate_indent = len(added_body) - len(added_body.lstrip())
            repeated_rejection = ""
            for following in raw_lines[index + 1:index + 8]:
                if following.startswith("@@ "):
                    break
                if not following.startswith("+") or following.startswith("+++"):
                    continue
                candidate = following[1:]
                candidate_indent = len(candidate) - len(candidate.lstrip())
                if candidate_indent <= gate_indent:
                    break
                if candidate.strip() in removed_bodies:
                    repeated_rejection = candidate.strip()
                    break
            if not repeated_rejection:
                continue
            outer_condition = ""
            for prior in reversed(raw_lines[max(0, index - 12):index]):
                if prior.startswith("@@ "):
                    break
                if not prior.startswith(" "):
                    continue
                prior_body = prior[1:]
                prior_indent = len(prior_body) - len(prior_body.lstrip())
                if prior_indent >= gate_indent:
                    continue
                outer = re.match(r"\s*if\s+(.+?)\s*:\s*$", prior_body)
                if outer is not None:
                    outer_condition = outer.group(1).strip()
                    break
            if not outer_condition:
                continue
            return (
                "Detected broad compatibility relaxation in "
                f"{normalized}: legacy guard `if {outer_condition}:` now "
                f"rejects only under nested gate `if not {gate_expression}:`; "
                f"the prior rejection `{repeated_rejection}` is therefore "
                "bypassed for legacy inputs without a new-syntax-specific "
                "condition. Preserve the old behavior and add a targeted "
                "before/after compatibility probe."
            )
    return ""


def _wrapped_sequence_step_risk(
    diff: str,
    paths: list[str],
    *,
    compatibility_context: str = "",
) -> str:
    """Detect step filtering that resets at a newly introduced wrap point."""
    sections = _diff_sections(diff)
    normalized_context = str(compatibility_context or "").casefold()
    endpoint_alias_required = bool(
        re.search(r"\b0\s*(?:/|and)\s*7\b", normalized_context)
        or re.search(r"0\s*(?:和|与)\s*7", normalized_context)
    ) and any(
        marker in normalized_context
        for marker in ("sunday", "周日", "星期日", "alias", "别名", "等价")
    )
    for path in paths:
        normalized = str(path).replace("\\", "/")
        lowered = normalized.casefold()
        basename = lowered.rsplit("/", 1)[-1]
        if (
            "/test" in lowered
            or basename.startswith("test_")
            or lowered.endswith((".md", ".rst", ".txt", ".jsonl", ".log"))
        ):
            continue
        section = next(
            (
                item for item in sections
                if f"a/{normalized}" in item or f"b/{normalized}" in item
            ),
            "",
        )
        if not section:
            continue
        added_text = "\n".join(
            line[1:]
            for line in section.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        segmented_delta_steps = re.findall(
            r"for\s+([A-Za-z_]\w*)\s+in\s+range\s*\([^\n]+\)\s*:"
            r"[\s\S]{0,400}?"
            r"\(\s*\1\s*-\s*([A-Za-z_]\w*)\s*\)\s*%\s*"
            r"(?:step|[A-Za-z_]\w*step[A-Za-z_]*)",
            added_text,
            re.IGNORECASE,
        )
        if (
            len(segmented_delta_steps) >= 2
            and re.search(r"\bwrap(?:ped|_range)?\b", added_text, re.IGNORECASE)
        ):
            value_name, origin_name = segmented_delta_steps[0]
            return (
                f"Detected segmented wrapped range step risk in {normalized}: "
                "multiple wrap segments independently filter with numeric delta "
                f"`({value_name} - {origin_name}) % step`. The second segment "
                "does not continue the first segment's position, so it can select "
                "the wrong values after the wrap point. Build one canonical "
                "sequence or carry a continuous traversal index across segments. "
                + (
                    "Because the contract aliases 0 and 7 to Sunday, that "
                    "canonical sequence must contain Sunday exactly once before "
                    "the step is applied. "
                    if endpoint_alias_required else ""
                )
                + (
                "then add a boundary-crossing range/step probe."
                )
            )
        introduces_wrapped_sequence = bool(
            re.search(
                r"list\s*\(\s*range\s*\([^\n]+\)\s*\)\s*\+\s*"
                r"list\s*\(\s*range\s*\(",
                added_text,
            )
            or (
                re.search(r"\bwrap(?:ped|_range)?\b", added_text, re.IGNORECASE)
                and re.search(r"\bspan\b", added_text)
            )
        )
        if not introduces_wrapped_sequence:
            continue
        after_text = "\n".join(
            line[1:]
            for line in section.splitlines()
            if (
                (line.startswith(" ") or line.startswith("+"))
                and not line.startswith("+++")
            )
        )
        endpoint_inclusive_wrapped_variables = set(re.findall(
            r"\b([A-Za-z_]\w*)\s*=\s*list\s*\(\s*range\s*\("
            r"[^,\n]+,\s*[A-Za-z_]\w*\s*\+\s*1\s*\)\s*\)\s*\+\s*"
            r"list\s*\(\s*range\s*\(",
            added_text,
        ))
        wrapped_variables = set(re.findall(
            r"\b([A-Za-z_]\w*)\s*=\s*list\s*\(\s*range\s*\([^\n]+\)\s*\)"
            r"\s*\+\s*list\s*\(\s*range\s*\(",
            added_text,
        ))
        value_delta_step = None
        for wrapped_variable in wrapped_variables:
            value_delta_step = re.search(
                rf"for\s+([A-Za-z_]\w*)\s+in\s+{re.escape(wrapped_variable)}\s*:"
                r"[\s\S]{0,600}?"
                r"\(\s*\1\s*-\s*([A-Za-z_]\w*)\s*\)\s*%\s*"
                r"(?:step|[A-Za-z_]\w*step[A-Za-z_]*)",
                after_text,
                re.IGNORECASE,
            )
            if value_delta_step is not None:
                break
        if value_delta_step is None:
            stride_step = re.search(
                r"\[[^\]\n]*::\s*(?:step|[A-Za-z_]\w*step[A-Za-z_]*)\s*\]",
                after_text,
                re.IGNORECASE,
            )
            index_step = None
            for wrapped_variable in endpoint_inclusive_wrapped_variables:
                index_step = re.search(
                    rf"for\s+([A-Za-z_]\w*)\s*,\s*[A-Za-z_]\w*\s+in\s+"
                    rf"enumerate\s*\(\s*{re.escape(wrapped_variable)}\s*\)\s*:"
                    r"[\s\S]{0,600}?\b\1\s*%\s*"
                    r"(?:step|[A-Za-z_]\w*step[A-Za-z_]*)",
                    after_text,
                    re.IGNORECASE,
                )
                if index_step is not None:
                    break
            step_operation = stride_step or index_step
            discard_alias_normalization = re.search(
                r"\.discard\(\s*([A-Za-z_0-9]+)\s*\).*?"
                r"\.add\(\s*([A-Za-z_0-9]+)\s*\)",
                after_text,
                re.IGNORECASE | re.DOTALL,
            )
            ordered_alias_deduplication = re.search(
                r"dict\.fromkeys\s*\([^\n]{0,300}?"
                r"(?:0\s+if\s+[A-Za-z_]\w*\s*==\s*7\s+else\s+[A-Za-z_]\w*|"
                r"[A-Za-z_]\w*\s+if\s+[A-Za-z_]\w*\s*!=\s*7\s+else\s*0)",
                after_text,
                re.IGNORECASE,
            )
            alias_normalization = (
                discard_alias_normalization or ordered_alias_deduplication
            )
            if (
                step_operation is None
                or not endpoint_inclusive_wrapped_variables
                or (
                    alias_normalization is not None
                    and step_operation.start() >= alias_normalization.start()
                )
                or (
                    alias_normalization is None
                    and not endpoint_alias_required
                )
            ):
                continue
            high_alias, low_alias = (
                discard_alias_normalization.groups()
                if discard_alias_normalization is not None else ("7", "0")
            )
            return (
                f"Detected wrapped range step risk in {normalized}: the wrapped "
                f"sequence is strided before alias endpoints `{high_alias}` and "
                f"`{low_alias}` are normalized. Equivalent endpoints therefore "
                "occupy separate traversal positions and change which values a "
                "step selects. Canonicalize or de-duplicate aliases before "
                "applying the step, then add a boundary-crossing range/step probe."
            )
        value_name, origin_name = value_delta_step.groups()
        return (
            f"Detected wrapped range step risk in {normalized}: the newly "
            "introduced wrapped sequence is filtered with numeric delta "
            f"`({value_name} - {origin_name}) % step`. Values reset at the "
            "wrap point, so this does not represent every Nth item in traversal "
            "order. Apply the step to the wrapped sequence's traversal position "
            "(for example, its enumerate index) and add a boundary-crossing "
            "range/step probe."
        )
    return ""


def _bounded_diff_hints(
    diff: str,
    paths: list[str],
    *,
    max_each: int = VERIFIER_DIFF_MAX_EACH,
    max_total: int = VERIFIER_DIFF_MAX_TOTAL,
) -> dict[str, str]:
    """Return bounded per-file unified diff evidence for the verifier."""
    text = str(diff or "")
    if not text.strip() or not paths:
        return {}
    sections = _diff_sections(text)
    result: dict[str, str] = {}
    remaining = max_total
    for path in paths:
        normalized = str(path).replace("\\", "/")
        selected = next(
            (
                section for section in sections
                if f"a/{normalized}" in section
                or f"b/{normalized}" in section
                or normalized in "\n".join(section.splitlines()[:4])
            ),
            sections[0] if len(paths) == 1 else "",
        )
        if not selected or remaining <= 0:
            continue
        limit = min(max_each, remaining)
        bounded = _truncate_inside_budget(
            selected,
            limit,
            "\n... [diff truncated]",
        )
        result[normalized] = bounded
        remaining -= len(bounded)
    return result


def _nearby_source_context(
    workspace: Any,
    diff: str,
    paths: list[str],
    *,
    radius: int = 32,
    max_each: int = 2500,
    max_total: int = 4000,
) -> dict[str, str]:
    """Read bounded source around changed hunks without escaping the workspace."""
    try:
        root = Path(workspace).resolve()
    except (OSError, TypeError, ValueError):
        return {}
    if not root.is_dir():
        return {}
    sections = _diff_sections(diff)
    result: dict[str, str] = {}
    remaining = max_total
    for path in paths:
        if remaining <= 0:
            break
        normalized = str(path).replace("\\", "/")
        selected = next(
            (
                section for section in sections
                if f"+++ b/{normalized}" in section
                or normalized in "\n".join(section.splitlines()[:4])
            ),
            sections[0] if len(paths) == 1 else "",
        )
        match = re.search(
            r"(?m)^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@",
            selected,
        )
        if match is None:
            continue
        try:
            target = (root / normalized).resolve()
            target.relative_to(root)
            if not target.is_file() or target.stat().st_size > 1_000_000:
                continue
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            continue
        start = max(1, int(match.group(1)))
        span = max(1, int(match.group(2) or 1))
        first = max(1, start - max(0, radius))
        last = min(len(lines), start + span - 1 + max(0, radius))
        if first > last:
            continue
        rendered = "\n".join(
            f"{line_number}: {lines[line_number - 1]}"
            for line_number in range(first, last + 1)
        )
        limit = min(max_each, remaining)
        bounded = _truncate_inside_budget(
            rendered,
            limit,
            "\n... [source context truncated]",
        )
        result[normalized] = bounded
        remaining -= len(bounded)
    return result


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
        self._accepted_evidence_key: tuple[Any, ...] | None = None
        self.stats: dict[str, Any] = {
            "fire_count": 0,
            "skip_count": 0,
            "verdict_counts": {"accept": 0, "revise": 0, "blocked": 0},
            "last_gate_reason": "",
            "last_trace": "",
        }

    @staticmethod
    def _accepted_cache_key(
        context: VerifierContext,
        metrics: VerifierGateMetrics,
        state: dict[str, Any],
    ) -> tuple[Any, ...]:
        """Identify semantic evidence while excluding final-answer wording."""
        try:
            contract = json.dumps(
                state.get("task_contract") or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            contract = str(state.get("task_contract") or "")
        return (
            max(0, int(state.get("mutation_generation", 0) or 0)),
            context.current_turn_user_queries,
            context.file_edit_summary,
            str(state.get("current_round_instruction_text") or ""),
            contract,
            metrics.risky_shell_ops,
            metrics.unattributed_write_ops,
        )

    def _evidence(
        self,
        context,
    ) -> tuple[VerifierContext, VerifierGateMetrics, str]:
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
        state = context.runtime_state if isinstance(context.runtime_state, dict) else {}
        semantic_pending = _semantic_review_pending(state)
        data_integrity_signals = _persistent_data_deletion_signals(state)
        verification = state.get("verification") or {}
        blocked_environment = bool(
            strict_local_tools()
            and verification.get("verification_state") == "blocked_environment"
        )
        if semantic_pending or blocked_environment or data_integrity_signals:
            paths = _rank_semantic_paths(paths, state)
            diff_hints = _bounded_diff_hints(
                diff,
                paths,
                max_each=5000,
                max_total=8000 if blocked_environment else 12000,
            )
        else:
            diff_hints = _bounded_diff_hints(diff, paths)
        source_context = (
            _nearby_source_context(
                getattr(self._loop, "workdir", None),
                diff,
                paths,
            )
            if blocked_environment
            else {}
        )
        summary = f"tracked change; run diff contains {changed_lines} changed line(s)"
        file_edits = [
            {
                "path": path,
                "diff_hint": (
                    ("deleted; " if path in deleted else "")
                    + summary
                    + (
                        "\nNEARBY SOURCE CONTEXT (repository evidence; compare "
                        "exact sibling API and registration patterns):\n"
                        + source_context[path]
                        if path in source_context else ""
                    )
                    + ("\n" + diff_hints[path] if path in diff_hints else "")
                ),
            }
            for path in paths
        ]
        criteria = [
            *(str(item) for item in state.get("acceptance_criteria") or []),
            *_render_contract_criteria(state),
        ]
        if blocked_environment:
            blocker = verification.get("environment_blocker") or {}
            criteria.append(
                "RUNTIME-OWNED STRICT OFFLINE BLOCKER\n"
                "- Treat this environment classification as a trusted execution fact.\n"
                "- Package installation, network access, Git history/remotes, and "
                "outside-workspace probes are forbidden to the Main Agent. Do not "
                "request package installation or any other forbidden operation.\n"
                "- Judge the source diff semantically from the available repository "
                "evidence. Return revise only for a concrete code defect or unmet "
                "user requirement, not to demand more unavailable verification.\n"
                "- Compare added or changed API calls against NEARBY SOURCE CONTEXT. "
                "A mismatch in the exact method, registration surface, argument "
                "shape, or validation path is a concrete code defect.\n"
                "- Do not claim knowledge of a gold patch or external reference fix; "
                "only the supplied repository and runtime evidence is authoritative.\n"
                "- Treat deleting existing persisted data to bypass a uniqueness or "
                "integrity conflict as a concrete defect unless the user explicitly "
                "authorized the data loss and the records are proven disposable.\n"
                f"- Blocked command: {_truncate(str(blocker.get('command') or ''), 600)}\n"
                f"- Runtime evidence: {_truncate(str(blocker.get('output') or ''), 1200)}"
            )
        if data_integrity_signals:
            rendered_signals = "\n".join(
                "- " + _truncate(str(item.get("detail") or item), 800)
                for item in data_integrity_signals[:5]
            )
            criteria.append(
                "PERSISTENT DATA-INTEGRITY REVIEW\n"
                "The deterministic impact analyzer found a newly added persistent "
                "deletion in a sensitive source path. This signal requests semantic "
                "review; it is not an automatic rejection. Verify that data loss is "
                "explicitly required, correctly scoped, and cannot bypass uniqueness "
                "or integrity behavior. Return revise for a concrete unsafe deletion; "
                "otherwise accept with a source-grounded reason.\n"
                + rendered_signals
            )
        if semantic_pending:
            criteria.append(SEMANTIC_CONTRACT_CERTIFICATION)
            delta_evidence = _compatibility_delta_evidence(diff, paths)
            if delta_evidence:
                criteria.append(
                    "COMPATIBILITY DELTA EVIDENCE (before/after changes that "
                    "must be audited, not automatically accepted):\n"
                    + delta_evidence
                )
        compatibility_risk = ""
        if semantic_pending:
            compatibility_risk = "\n\n".join(filter(None, (
                _broad_compatibility_relaxation(diff, paths),
                _wrapped_sequence_step_risk(
                    diff,
                    paths,
                    compatibility_context="\n".join(criteria),
                ),
            )))
        if compatibility_risk:
            criteria.append(
                "DETERMINISTIC COMPATIBILITY RISK (must be resolved before "
                "acceptance):\n" + compatibility_risk
            )
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
        return verifier_context, metrics, compatibility_risk

    async def __call__(self, context) -> StopHookDecision:
        verifier_context, metrics, compatibility_risk = self._evidence(context)
        state = context.runtime_state if isinstance(context.runtime_state, dict) else {}
        accepted_cache_key = self._accepted_cache_key(
            verifier_context,
            metrics,
            state,
        )
        tracer = getattr(self._loop, "tracer", None)
        semantic_pending = _semantic_review_pending(state)
        data_integrity_risk = bool(_persistent_data_deletion_signals(state))
        verification = state.get("verification") or {}
        blocked_environment = bool(
            strict_local_tools()
            and verification.get("verification_state") == "blocked_environment"
        )
        deterministic_verification_pending = (
            strict_local_tools()
            and bool(verification.get("verification_needed"))
            and verification.get("verification_state")
            in {"unverified", "verifying", "failed_repairable"}
        )
        mandatory_review = (
            deterministic_verification_pending
            or blocked_environment
            or semantic_pending
            or data_integrity_risk
        )
        if (
            not mandatory_review
            and accepted_cache_key == self._accepted_evidence_key
        ):
            self.stats["last_gate_reason"] = "accepted-evidence-cache"
            self.stats["skip_count"] += 1
            if tracer is not None:
                tracer.log(
                    "sidecar_gate_decision",
                    fire=False,
                    reason="accepted-evidence-cache",
                    files_changed=metrics.files_changed,
                    changed_lines=metrics.estimated_changed_lines,
                    rounds=metrics.rounds,
                )
            return StopHookDecision()
        if deterministic_verification_pending:
            # In strict SWE runs the deterministic generation gate follows this
            # hook and owns repair/retry guidance.  Let it converge before
            # paying for semantic review; the Sidecar will run on the next
            # natural stop once current-generation evidence has settled.
            fire, gate_reason = False, "deterministic-verification-pending"
        elif blocked_environment:
            fire, gate_reason = True, "blocked-environment-semantic-review"
        elif semantic_pending:
            fire, gate_reason = True, "semantic-contract"
        elif data_integrity_risk:
            fire, gate_reason = True, "data-integrity-risk"
        else:
            fire, gate_reason = compose_gate_decision(
                context.transcript,
                metrics,
                env=self._env,
            )
        self.stats["last_gate_reason"] = gate_reason
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

        # Deterministic compatibility findings already prove that completion
        # is unsafe.  Calling the judge first only adds latency/cost and can
        # serialize independent defects across the two reanimation slots.
        # Return the complete aggregated guard result now; the judge will run
        # on the next clean generation after the model repairs every finding.
        if semantic_pending and compatibility_risk:
            self.stats["fire_count"] += 1
            verdict = _merge_compatibility_risk(
                VerifierVerdict("accept", "", trace="deterministic_guard"),
                compatibility_risk,
            )
            self.stats["last_trace"] = verdict.trace
            self.stats["verdict_counts"][verdict.verdict] += 1
            if tracer is not None:
                tracer.log(
                    "sidecar_finished",
                    verdict=verdict.verdict,
                    trace=verdict.trace,
                    reason=verdict.reason,
                    elapsed_ms=0.0,
                    provider=self._resolved.provider_name,
                    model=self._resolved.model,
                    source=self._resolved.source,
                    provider_invoked=False,
                )
            return map_verdict_to_stop_decision(verdict)

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
                observer=getattr(self._loop, "_model_gateway_observer", None),
            )
        except asyncio.CancelledError:
            cancel_event.set()
            raise
        except Exception:
            verdict = VerifierVerdict("accept", "", trace="provider_error")
        if semantic_pending:
            verdict = _merge_compatibility_risk(verdict, compatibility_risk)
        self.stats["last_trace"] = verdict.trace
        self.stats["verdict_counts"][verdict.verdict] += 1
        if (
            verdict.verdict == "accept"
            and verdict.trace in {"verifier_ok", "fuzzy_tool_match"}
        ):
            self._accepted_evidence_key = accepted_cache_key
            state = getattr(self._loop, "runtime_state", None)
            observe = getattr(state, "observe_requirement_semantic_review", None)
            if callable(observe):
                observe(
                    accepted=True,
                    fingerprint=f"{verdict.trace}:compatibility",
                )
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
