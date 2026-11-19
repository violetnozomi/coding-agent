"""Deterministic completion decisions based on requirement evidence."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex

from nz_coder.protocol.message_schema import stamp_user_message
from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView, is_generic_artifact_requirement
from nz_coder.runtime.verification.verification_contract import effective_acceptance_generation


COMPLETION_GATE_REANIMATE_BUDGET = 2


def completion_feedback(snapshot, assessment) -> str:
    """只格式化域给出的明确缺项，不从命令语法追加业务义务。"""
    if not assessment.blocking_ids:
        return ""
    missing = set(assessment.blocking_ids)
    lines = ["The current task has explicit requirements that remain unconfirmed:"]
    for spec in snapshot.specs:
        if spec.id in missing:
            decision = snapshot.decision(spec.id)
            lines.append(f"- {spec.command.original} (cwd={spec.command.cwd}; status={decision.status}): {decision.reason}")
    for item in snapshot.requirements:
        if item.requirement.id in missing:
            lines.append(f"- {item.requirement.id}: {item.requirement.description}; status={item.status}")
    lines.append("Resolve these conditions against the current workspace, or explain the concrete blocker. "
                 "Use actual tool evidence; do not repeat unchanged successful checks. "
                 "Then provide the complete delivery response for the original task.")
    return "\n".join(lines)

# 两种补证入口都必须交还完整用户任务，不能把补测结果变成新的交付范围。
_DELIVERY_CONTINUATION = (
    "This is a runtime-owned continuation of the current user task. "
    "Once all required checks are settled, give the complete final response for "
    "that task and its genuine follow-ups: summarize the work and all relevant "
    "confirmed validation, requested run/use instructions, and remaining gaps. "
    "Do not limit the final response to the last verification command, and do not "
    "reuse unsupported claims from earlier completion drafts."
)


@dataclass(frozen=True)
class CompletionDecision:
    """One bounded decision returned at a natural Agent stop boundary."""

    ready: bool
    missing_ids: tuple[str, ...] = ()
    message: str = ""


class CompletionGate:
    """Prevent a final answer while objective contract work remains unresolved."""

    def __init__(self, *, max_missing: int = 6) -> None:
        self.max_missing = max(1, int(max_missing))

    def evaluate(
        self,
        ledger: RequirementLedgerView,
        *,
        mutation_generation: int,
        acceptance_required: bool = True,
    ) -> CompletionDecision:
        unresolved = ledger.unresolved()
        if not unresolved:
            return CompletionDecision(ready=True)
        selected = unresolved[: self.max_missing]
        runtime_owned = [
            item for item in selected
            if ledger.semantic_review_ready(
                item, mutation_generation, acceptance_required=acceptance_required,
            )
        ]
        actionable = [item for item in selected if item not in runtime_owned]
        lines = [
            "TaskContract is not complete. Continue with only these unresolved requirements:"
        ]
        needs_execution = False
        generic_execution_paths = []
        for item in actionable:
            artifacts = ", ".join(item.requirement.expected_artifacts)
            details = f"status={item.status}"
            if is_generic_artifact_requirement(item.requirement):
                has_modification = any(e.type in {"file_created", "file_changed"} for e in item.evidence)
                if not has_modification:
                    kind = "test" if item.requirement.kind == "test" else "documentation"
                    details += f", missing observed {kind} modification"
                else:
                    details += ", missing current verification evidence"
                    if item.requirement.kind == "test":
                        generic_execution_paths.extend(e.path for e in item.evidence if e.type in {"file_created", "file_changed"})
                    else:
                        needs_execution = True
            else:
                needs_execution = True
            if artifacts:
                details += f", expected artifacts: {artifacts}"
            lines.append(
                f"- {item.requirement.id}: {item.requirement.description} "
                f"({details})"
            )
        if runtime_owned:
            lines.append(
                "Runtime-owned evidence pending. Do not edit code solely to satisfy "
                "these requirements:"
            )
            lines.extend(
                f"- {item.requirement.id}: {item.requirement.description} "
                "(required evidence: semantic_review)"
                for item in runtime_owned
            )
        if len(unresolved) > len(selected):
            lines.append(f"- ... {len(unresolved) - len(selected)} more")
        if actionable:
            lines.append(
                "Use existing repository evidence and make a narrow repair for the stated "
                "missing evidence. Do not restart broad exploration."
            )
            if generic_execution_paths:
                lines.append(
                    "Run a standalone actual test for these observed candidate files or their package, "
                    "without test exclusions or selection filters, pipes, redirections, or extra commands: "
                    + ", ".join(dict.fromkeys(generic_execution_paths))
                    + ". File association is not proof of semantic coverage; Runtime will review the contents."
                )
            # 补证指引不改变可信规则；尤其不能把 tail 等命令的成功当成测试通过。
            if needs_execution and not generic_execution_paths and acceptance_required:
                lines.append(
                    "If the required edits are already present, run the user-declared "
                    "acceptance command exactly in its declared working directory. "
                    "Do not substitute a static check or a different test command."
                )
            elif needs_execution and not generic_execution_paths:
                lines.append(
                    "If the required edits are already present, obtain current test evidence "
                    "before making further edits. Rerun the same focused test as a single bash "
                    "call, without pipes, redirections, or other commands. Keep the test "
                    "selectors and working directory unchanged; tool output is already captured. "
                    "After a clean pass, return a truthful summary so Runtime can perform any "
                    "pending independent semantic review; do not repeat unchanged checks."
                )
        else:
            lines.append(
                "Do not make speculative edits. Return a truthful summary without extra "
                "tool calls so Runtime can retry its independent semantic review."
            )
        return CompletionDecision(
            ready=False,
            missing_ids=tuple(item.requirement.id for item in selected),
            message="\n".join(lines),
        )


def append_completion_guidance(
    messages: list,
    state,
) -> tuple[CompletionDecision, bool]:
    """Append one generation-scoped repair prompt for an unresolved ledger.

    Both the native terminal boundary and legacy-backed policy adapter use this
    helper so a mixed deterministic/semantic ledger cannot fall between their
    two completion paths.  Repeated natural stops without new workspace
    evidence reuse the existing prompt instead of inflating context.
    """
    ledger_data = getattr(state, "requirement_ledger", None)
    ledger_provider = getattr(state, "requirement_ledger_snapshot", None)
    if not (isinstance(ledger_data, dict) and ledger_data and callable(ledger_provider)):
        return CompletionDecision(ready=True), False
    generation = int(getattr(state, "mutation_generation", 0) or 0)
    decision = CompletionGate().evaluate(
        ledger_provider(),
        mutation_generation=effective_acceptance_generation(state),
        acceptance_required=bool(getattr(state, "verification_contract", None)),
    )
    if decision.ready:
        return decision, False
    prompt_count = int(getattr(state, "completion_gate_prompts", 0) or 0)
    if prompt_count >= COMPLETION_GATE_REANIMATE_BUDGET:
        return decision, False
    signature = f"{generation}|{'|'.join(decision.missing_ids)}"
    state.completion_gate_signature = signature
    state.completion_gate_prompts = prompt_count + 1
    messages.append(stamp_user_message({
        "role": "user",
        "content": (
            "<requirement-completion-gate>\n"
            + decision.message
            + "\n\n" + _DELIVERY_CONTINUATION
            + "\n</requirement-completion-gate>"
        ),
        "_nz_synthetic": True,
        "_nz_completion_gate": True,
    }))
    return decision, True


__all__ = [
    "CompletionDecision",
    "CompletionGate",
    "COMPLETION_GATE_REANIMATE_BUDGET",
    "append_completion_guidance",
]


def ordinary_verification_guidance(snapshot: dict, workspace: Path) -> str:
    """只建议仍有执行条件的原范围检查，不执行或改写原命令。"""
    blockers = [item for item in (snapshot.get("environment_blocker"), snapshot.get("verification_unavailable")) if item]
    if any(not item.get("command") for item in blockers):
        return ""
    candidates = []
    for stage in snapshot.get("verification_pipeline", {}).get("stages", []):
        candidates.extend(item for item in stage.get("commands", [])
                          if item.get("required") and item.get("status") not in {"passed", "covered"})
    candidates.extend(item for item in snapshot.get("test_obligations", [])
                      if item.get("status") != "passed")
    candidates = list({(item.get("command"), item.get("cwd", ".")):item for item in candidates}.values())
    if not candidates and snapshot.get("mutation_scope_unknown"):
        # 已知义务已完成也不能靠重跑同一窄检查证明未知变更范围。
        return ""
    if not candidates and snapshot.get("next_required_stage") == "targeted":
        candidates = [item for stage in snapshot.get("verification_pipeline", {}).get("stages", [])
                      if stage.get("name") == "targeted" for item in stage.get("commands", [])
                      if item.get("retry_eligible") and item.get("status") not in {"passed", "covered"}]
    lines = []
    for item in candidates:
        command = str(item.get("retry_command") or item.get("command") or "")
        if "scope" in item and item["scope"] is None:
            continue
        cwd = (workspace / str(item.get("scope") or item.get("cwd") or ".")).resolve()
        if any(blocker.get("command") == command and
               (workspace / str(blocker.get("scope") or blocker.get("cwd") or ".")).resolve() == cwd
               for blocker in blockers):
            continue
        if not command or not cwd.is_dir():
            continue
        try:
            shlex.split(command)
        except ValueError:
            continue
        # 参数可能是 -p 模式或 -k 选择式，交给原命令处理，不能猜作文件。
        lines.append(f"- original command: {command}\n  cwd: {item.get('cwd') or '.'}")
        if len(lines) == 6:
            break
    if not lines:
        return ""
    return ("Verification remains unconfirmed. Keep the original selectors and working "
            "directory. Submit the focused verification as one standalone tool call without "
            "pipes or extra commands; captured output needs no tail. Use normal permissions. "
            "Do not recreate deleted temporary helpers to satisfy an old plan.\n"
            + "\n".join(lines)
            + "\n\n" + _DELIVERY_CONTINUATION)


def verification_appendix(snapshot: dict) -> str:
    """只展示冻结的验证事实，不从业务输出猜测试数或功能统计。"""
    def command(item):
        raw = str(item.get("command") or "unknown")
        if len(raw) > 500:
            raw = raw[:450] + "…（命令已截断，原文见 verification 元数据）"
        return raw.replace("`", "\\`").replace("\n", " ")
    lines = ["**运行时验证记录**（来源：VerificationDomain）"]
    failures = snapshot.get("ordinary_failures", [])
    for failure in failures:
        lines.append(f"- 保留的失败记录：`{command(failure)}`；版本 {failure.get('generation')}，退出码 {failure.get('exit_code')}。当前交付评审不将该记录改写成通过。")
    if snapshot.get("verification_needed"):
        lines.append("- 验证未确认：仍有缺项，不能据此声明全部测试通过。")
    elif snapshot.get("verification_state") == "unverified":
        if not failures:
            lines.append("- 未评估：本次没有已采信的验证执行。")
    else:
        lines.append("- 验证状态：" + str(snapshot.get("verification_state") or "unknown"))
    review = snapshot.get("delivery_review") or {}
    if review.get("accepted") is False:
        lines.append("- 交付评审未通过。")
    elif snapshot.get("review_pending"):
        lines.append("- 交付评审尚未确认。")
    if snapshot.get("mutation_scope_unknown"):
        lines.append("- 工作区范围尚未确认；已知文件检查通过不代表完整覆盖，也不能据此断定发生了源码修改。")
    last = snapshot.get("last_verification") or {}
    if last:
        label = "已采信检查" if last.get("status") == "passed" else "最近检查（" + str(last.get("status") or "unknown") + "）"
        if (last.get("status") == "passed" and snapshot.get("verification_needed")
                and last.get("evidence_generation") != snapshot.get("mutation_generation")):
            label = "历史通过（当前版本未确认）"
        lines.append(f"- {label}：`{command(last)}`；cwd=`{last.get('cwd') or 'unknown'}`。")
    generation = snapshot.get("mutation_generation")
    seen = {(last.get("command"), last.get("scope") or last.get("cwd"))} if last else set()
    accepted = []
    for stage in (snapshot.get("verification_pipeline") or {}).get("stages", []):
        for item in stage.get("commands", []) + stage.get("observed", []):
            # 合同可代替计划检查；展示实际执行的 last_command，不能把计划名当执行记录。
            actual = item.get("last_command")
            scope = item.get("scope")
            if (item.get("status") != "passed" or generation is None
                    or item.get("evidence_generation") != generation or not actual or not scope):
                continue
            key = (actual, scope)
            if key not in seen:
                seen.add(key)
                accepted.append({"command": actual, "cwd": scope})
    for item in accepted[:5]:
        lines.append(f"- 已采信检查：`{command(item)}`；cwd=`{item['cwd']}`。")
    if len(accepted) > 5:
        lines.append(f"- 另有 {len(accepted) - 5} 项本代通过检查，详见 verification.verification_pipeline。")
    for ignored in (snapshot.get("ignored_verifications") or [])[-3:]:
        lines.append(f"- 历史结果未采信：`{command(ignored)}`；cwd=`{ignored.get('cwd') or 'unknown'}`；{ignored.get('reason') or '原因未知'}。")
    pending = [item for stage in (snapshot.get("verification_pipeline") or {}).get("stages", [])
               for item in stage.get("commands", [])
               if item.get("required") and item.get("status") not in {"passed", "covered"}]
    pending.extend(item for item in snapshot.get("test_obligations", []) if item.get("status") != "passed")
    pending = list({(item.get("command"), item.get("cwd", ".")):item for item in pending}.values())
    for item in pending[:3]:
        lines.append(f"- 待验证（不是执行记录）：`{command(item)}`。")
    if snapshot.get("verification_unavailable"):
        lines.append("- 验证命令未执行：调用被拒或未能调度，详见 verification.verification_unavailable。")
    if snapshot.get("environment_blocker"):
        lines.append("- 环境阻塞，详见 verification.environment_blocker。")
    return "\n".join(lines)
