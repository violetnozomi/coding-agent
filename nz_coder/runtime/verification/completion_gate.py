"""Deterministic completion decisions based on requirement evidence."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex

from nz_coder.protocol.message_schema import stamp_user_message
from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView, is_generic_artifact_requirement
from nz_coder.runtime.verification.report_evidence import DeniedCommands
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


def _inline_code(text: str) -> str:
    # 行内代码不能用反斜线转义反引号，围栏须比实际内容中的连续反引号更长。
    backticks = re.findall(r"`+", text)
    if backticks:
        fence = "`" * (1 + max(map(len, backticks)))
        return f"{fence} {text} {fence}"
    return f"`{text}`"


def _report_file_name(path: str) -> str:
    text = "".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii") for char in path)
    return _inline_code(text[:300] + "…" if len(text) > 300 else text)


def _append_recorded_changes(lines: list[str], snapshot) -> None:
    paths = sorted({fact.path for fact in snapshot.artifacts
                    if fact.complete and fact.operation in {"create", "change", "delete"}})
    if paths:
        lines.extend(["", "当前任务已记录变更的文件：" + "、".join(_report_file_name(path) for path in paths[:12])])
        if len(paths) > 12:
            lines.append(f"共 {len(paths)} 个文件，以上列出 12 个。")


MODEL_NOTE_HINT = "用 /message 查看本轮模型说明。"


def host_test_report(snapshot, *, model_note: bool = False, status: str = "completed", stop_reason: str = "") -> str:
    """从冻结域展示真实测试命令的范围和结论，评审分类与模型原稿不提供执行证据。"""
    from nz_coder.runtime.verification.domain.models import is_behavior_failure

    interrupted = status in {"cancelled", "interrupted"}
    lines = []
    if status not in {"completed", "completed_unverified"} and not interrupted:
        lines.append(f"本次运行已结束（{status}）。")
        if stop_reason:
            lines.append(stop_reason)
        lines.append("")
    if snapshot is None:
        return "" if interrupted else "\n".join([*lines, "本轮没有已采信的测试结果。"])
    # 用户主动取消时，只有实际开始过测试，才需要一份单独的测试报告。
    if interrupted and not any(
        fact.scope == snapshot.scope and "test" in fact.capabilities for fact in snapshot.executions
    ):
        return ""
    test_ids = {spec.id for spec in snapshot.specs if spec.capability == "test"}
    decisions = {(check.execution_id, check.status) for check in snapshot.checks if check.check_id in test_ids}
    rows = []
    latest_confirmed = {}
    passed = failed = 0
    for fact in snapshot.executions:
        if "test" not in fact.capabilities:
            continue
        same_run = fact.scope == snapshot.scope
        current_version = (fact.before.acceptance_generation == fact.after.acceptance_generation
                           == snapshot.version.acceptance_generation)
        # 由域决定回执是否仍有效。只改文档时，早先执行的测试仍可支撑当前验收。
        if (fact.execution_id, "passed") in decisions and snapshot.current_success(fact):
            label = "测试命令通过"
            if fact.after.generation != snapshot.version.generation:
                label += f"（本轮第 {fact.after.generation} 代执行，回执对当前验收版本仍有效）"
            passed += 1
            latest_confirmed["passed"] = len(rows)
        elif not same_run or not current_version:
            label = (f"历史记录（轮次 {_report_file_name(fact.scope.interaction_run_id)}，"
                     f"验收版本 {fact.after.acceptance_generation}）")
        elif (fact.execution_id, "failed") in decisions and is_behavior_failure(fact):
            label = "测试命令失败"
            failed += 1
            latest_confirmed["failed"] = len(rows)
        elif fact.environment_error:
            label = "测试环境阻塞"
        elif fact.test_count == 0:
            label = "没有可用于验收的测试用例"
        else:
            label = "未采信的测试命令记录"
        exit_info = f"，退出码 {fact.exit_code}" if fact.exit_code is not None else ""
        rows.append(f"- {label}：{_report_file_name(fact.command.original)}，"
                    f"工作目录 {_report_file_name(fact.command.cwd)}{exit_info}。")

    lines.append("**本轮测试结果（宿主执行回执）**")
    if passed or failed:
        counts = [f"{count} 次测试命令{outcome}" for count, outcome in ((passed, "通过"), (failed, "失败")) if count]
        lines.append("当前已采信：" + "，".join(counts) + "。")
    else:
        lines.append("本轮没有已采信的测试结果。")
    # 优先保留各一条有效通过和失败，后续大量无结果命令不能挤掉已知结论的范围。
    shown = set(latest_confirmed.values())
    for index in reversed(range(len(rows))):
        if len(shown) >= 12:
            break
        shown.add(index)
    lines.extend(rows[index] for index in sorted(shown))
    if len(rows) > len(shown):
        lines.append(f"共记录 {len(rows)} 次测试命令调用，此处列出 {len(shown)} 次。")
    _append_recorded_changes(lines, snapshot)
    lines.extend(["", verification_appendix(snapshot.to_dict())])
    if model_note:
        lines.extend(["", MODEL_NOTE_HINT])
    return "\n".join(lines)


def host_execution_report(receipts: DeniedCommands, verification: dict, snapshot=None, *,
                          stop_reason: str = "", model_note: bool = False) -> str:
    """一次调用在启动前被拒时，默认正文由可追溯的宿主事实生成。"""
    lines = ["**本轮被拦下、未执行的命令**"]
    for entry in receipts.entries:
        lines.append(f"- 调用 {_inline_code(entry.call_id)}（回执 {_inline_code(entry.receipt_id)}），工作目录 {_inline_code(entry.cwd)}，"
                     f"命令 {_inline_code(entry.command)}" + ("（显示内容有截断）" if entry.truncated else ""))
    if receipts.total > len(receipts.entries):
        lines.append(f"共 {receipts.total} 次，此处列出最近 {len(receipts.entries)} 次。")
    if snapshot is not None:
        _append_recorded_changes(lines, snapshot)
        # 普通 Shell 命令的退出码只属于这次调用与目录，不能替另一环境或整套测试作证。
        checks = {decision.execution_id for decision in snapshot.checks}
        successes = [fact for fact in snapshot.executions if fact.scope == snapshot.scope
                     and fact.execution_id not in checks and fact.outcome == "succeeded" and fact.exit_code == 0]
        if successes:
            lines.extend(["", "本轮另有以下命令以退出码 0 结束："])
            for fact in successes[-4:]:
                lines.append(f"- {_report_file_name(fact.command.original)}，工作目录 {_report_file_name(fact.command.cwd)}。")
    if stop_reason:
        lines.extend(["", "本轮停止原因：" + stop_reason])
    if "verification_state" in verification:
        lines.extend(["", verification_appendix(verification)])
    if model_note:
        lines.extend(["", MODEL_NOTE_HINT])
    return "\n".join(lines)


def verification_appendix(snapshot: dict) -> str:
    """只展示冻结的验证事实，不从业务输出猜测试数或功能统计。"""
    def command(item):
        raw = str(item.get("command") or "unknown")
        if len(raw) > 500:
            raw = raw[:450] + "…（命令已截断，原文见 verification 元数据）"
        return raw.replace("`", "\\`").replace("\n", " ")
    stages = (snapshot.get("verification_pipeline") or {}).get("stages", [])
    checks = [item for stage in stages for item in stage.get("commands", []) + stage.get("observed", [])]
    # 域已按验收范围作废过期回执。只改 README 可以沿用测试，不能再按全工作区版本过滤。
    passed = [item for item in checks
              if item.get("status") == "passed" and item.get("last_command") and item.get("scope")]
    blocked = [item for item in checks if item.get("status") == "blocked_environment"]
    failures = snapshot.get("ordinary_failures", [])
    state = snapshot.get("verification_state")
    lines = ["**运行时验证记录**（来源：VerificationDomain）"]
    if any(item.get("capability") == "test" for item in passed):
        lines.append("- 执行检查：已有通过的测试回执，范围见下列命令。")
    elif passed or state == "passed":
        lines.append("- 执行检查：已有通过的检查记录，没有已采信的测试通过回执。")
    elif failures or any(item.get("status") == "failed" for item in checks):
        lines.append("- 执行检查：曾记录检查失败，当前没有已采信的通过回执。")
    elif state == "unverified" and not blocked and not snapshot.get("verification_needed"):
        lines.append("- 未评估：本次没有已采信的验证执行。当前没有待完成的必需检查。")
    else:
        lines.append("- 执行检查：尚无可确认的通过结果。")
    if state == "failed_repairable":
        lines.append("- 仍有未解决的检查失败。")
    elif snapshot.get("verification_needed"):
        lines.append("- 检查或工作区范围仍有待确认项。")
    review = snapshot.get("delivery_review") or {}
    if review.get("accepted") is False:
        lines.append("- 交付评审未通过。")
    elif review.get("accepted") is True:
        lines.append("- 交付评审已通过。" + ("另有评审要求待确认。" if snapshot.get("review_pending") else ""))
    elif snapshot.get("review_pending"):
        lines.append("- 交付评审尚未确认。")
    else:
        lines.append("- 本次没有独立交付评审的结论。")

    # 顶层环境阻塞仅收集必需检查；自选测试也可能因环境问题没有取得结果。
    for check in blocked[:3]:
        outcome = "，此次没有得到测试结果" if check.get("capability") == "test" else ""
        lines.append(f"- 检查 `{command(check)}` 因环境问题未能完成{outcome}。")
    if len(blocked) > 3:
        lines.append(f"- 另有 {len(blocked) - 3} 项检查遇到环境问题，详见 verification.verification_pipeline。")
    for failure in failures:
        lines.append(f"- 保留的失败记录：`{command(failure)}`；版本 {failure.get('generation')}，退出码 {failure.get('exit_code')}。当前交付评审不将该记录改写成通过。")
    syntax = snapshot.get("changed_python_syntax") or {}
    if syntax.get("status") == "unknown":
        lines.append("- 工作区清单无法确认，变更的 Python 文件及其语法检查范围未知。")
    elif syntax.get("status") == "missing_current_pass":
        paths = syntax["paths"]
        names = "、".join(_report_file_name(path) for path in paths)
        if syntax["count"] > len(paths):
            names += f" 等 {syntax['count']} 个文件"
        lines.append(f"- 变更的 Python 文件 {names} 没有本代通过的语法检查记录。")
    if snapshot.get("mutation_scope_unknown"):
        lines.append("- 工作区范围尚未确认；已知文件检查通过不代表完整覆盖，也不能据此断定发生了源码修改。")
    last = snapshot.get("last_verification") or {}
    if last:
        label = {"passed": "已采信检查", "failed": "最近检查（失败）",
                 "blocked_environment": "最近检查（环境阻塞）"}.get(last.get("status"), "最近检查（未确认）")
        lines.append(f"- {label}：`{command(last)}`；cwd=`{last.get('cwd') or 'unknown'}`。")
    seen = {(last.get("command"), last.get("scope") or last.get("cwd"))} if last else set()
    accepted = []
    for item in passed:
        # 合同可代替计划检查；展示实际执行的 last_command，不能把计划名当执行记录。
        key = (item["last_command"], item["scope"])
        if key not in seen:
            seen.add(key)
            accepted.append({"command": key[0], "cwd": key[1]})
    for item in accepted[:5]:
        lines.append(f"- 已采信检查：`{command(item)}`；cwd=`{item['cwd']}`。")
    if len(accepted) > 5:
        lines.append(f"- 另有 {len(accepted) - 5} 项已采信检查，详见 verification.verification_pipeline。")
    for ignored in (snapshot.get("ignored_verifications") or [])[-3:]:
        lines.append(f"- 历史结果未采信：`{command(ignored)}`；cwd=`{ignored.get('cwd') or 'unknown'}`；{ignored.get('reason') or '原因未知'}。")
    pending = [item for item in checks if item.get("required") and item.get("status") not in {"passed", "covered"}]
    pending.extend(item for item in snapshot.get("test_obligations", []) if item.get("status") != "passed")
    pending = list({(item.get("command"), item.get("cwd", ".")):item for item in pending}.values())
    for item in pending[:3]:
        lines.append(f"- 待验证（不是执行记录）：`{command(item)}`。")
    if snapshot.get("verification_unavailable"):
        lines.append("- 验证命令未执行：调用被拒或未能调度，详见 verification.verification_unavailable。")
    if snapshot.get("environment_blocker"):
        lines.append("- 环境阻塞，详见 verification.environment_blocker。")
    return "\n".join(lines)
