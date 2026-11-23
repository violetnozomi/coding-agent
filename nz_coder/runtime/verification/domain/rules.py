"""纯验证裁决：实际执行结果与是否满足当前义务分开。"""
from __future__ import annotations

from pathlib import Path
import re

from .identity import commands_equivalent, display_check_body, observed_check_subject, verified_check_subject
from .models import CHECK_SOURCES, CheckDecision, CheckSpec, CommandIdentity, ExecutionFact, TaskScope, WorkspaceVersion


def observed_check_stage(command: CommandIdentity, capability: str, detected_stage: str | None) -> str | None:
    """只有可归因的检查才自动承担验收责任；失败本身不建立检查意图。"""
    if command.opaque and observed_check_subject(command) is None:
        return None
    if detected_stage:
        return detected_stage
    if capability in {"syntax", "symbol", "type", "lint", "build"}:
        return "static"
    return None


def matches_check(spec: CheckSpec, fact: ExecutionFact) -> bool:
    if (spec.source == "user_declared" and fact.in_process
            or spec.coverage_mode == "in_process" and not fact.in_process):
        return False
    if (spec.command != fact.command and display_check_body(fact.command) is not None
            and (fact.outcome != "succeeded" or fact.exit_code != 0)):
        # 整条非零可能来自展示尾段；保留整条负事实，不能指定为主体失败。
        return False
    if commands_equivalent(spec.command, fact.command, native_runner_kind=spec.native_runner_kind):
        return True
    subject = verified_check_subject(fact.command) if fact.reliable else None
    if subject is not None and commands_equivalent(spec.command, subject, native_runner_kind=spec.native_runner_kind):
        return True
    return bool(spec.source == "planner" and spec.coverage_mode == "capability_targets"
                and spec.command.cwd == fact.command.cwd and spec.command.environment == fact.command.environment
                and (not fact.command.opaque or fact.in_process)
                and (len(fact.capabilities) == 1 or fact.in_process)
                and spec.targets and set(spec.targets).intersection(fact.targets)
                and spec.capability in fact.capabilities)


def supports_retry(spec: CheckSpec, fact: ExecutionFact, task: TaskScope) -> bool:
    """保留重试依据和消费该依据必须采用同一规则，不把失效通过升级为当前通过。"""
    subject = observed_check_subject(spec.command) if spec.native_runner_kind == "observed_output" else None
    retry = subject or spec.command
    return bool(spec.source in CHECK_SOURCES and fact.scope.task == task and not fact.in_process
                and type(fact.exit_code) is int and spec.capability in fact.capabilities
                and matches_check(spec, fact) and (not retry.opaque or fact.reliable))


def _native_selection(command: CommandIdentity):
    subject = observed_check_subject(command) if command.opaque else command
    return subject


def validate_test_selection_attestation(command: CommandIdentity, *, outcome, exit_code, test_count,
                                        capabilities, output_complete, reliable, test_selection_failure):
    """校验持久分类的可验证前提；完整正文只在生产时解析。"""
    from nz_coder.runtime.agent.task_policy import unittest_selectors
    if type(test_selection_failure) is not str or test_selection_failure not in {"", "empty_selection", "unittest_method_lookup"}:
        raise ValueError("invalid test selection classification")
    if not test_selection_failure:
        return
    subject = _native_selection(command)
    if subject is None or not subject.argv or type(exit_code) is not int or type(test_count) is not int or type(output_complete) is not bool or not output_complete:
        raise ValueError("invalid test selection attestation")
    executable = Path(subject.argv[0]).name
    if test_selection_failure == "empty_selection":
        native = executable in {"pytest", "pytest3", "py.test"} or (
            re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable)
            and subject.argv[1:3] in {("-m", "pytest"), ("-m", "unittest")})
        valid = native and exit_code == 5 and test_count == 0
    else:
        valid = (outcome == "failed" and exit_code == 1 and "test" in capabilities
                 and type(reliable) is bool and reliable and bool(unittest_selectors(subject.argv)))
    if not valid:
        raise ValueError("invalid test selection attestation")


def classify_test_selection_failure(command: CommandIdentity, *, outcome, exit_code, test_count,
                                    capabilities, output, output_complete, reliable) -> str:
    """从完整输出提取有界分类，不接受检查项。"""
    from nz_coder.runtime.agent.task_policy import unittest_selectors
    subject = _native_selection(command)
    if subject is None or not subject.argv:
        return ""
    common = dict(outcome=outcome, exit_code=exit_code, test_count=test_count,
                  capabilities=capabilities, output_complete=output_complete, reliable=reliable)
    try:
        validate_test_selection_attestation(command, **common, test_selection_failure="empty_selection")
    except ValueError:
        pass
    else:
        text = output.casefold()
        if (re.search(r"\bran\s+0\s+tests?\b|\bno tests (?:ran|collected)\b", text)
                and not re.search(r"\berrors?\b|\bfailed\b|traceback|assertion|collection error|importerror", text)):
            return "empty_selection"
    try:
        validate_test_selection_attestation(command, **common, test_selection_failure="unittest_method_lookup")
    except ValueError:
        return ""
    selectors = unittest_selectors(subject.argv)
    lines = output.splitlines()
    nonblank = [line for line in lines if line.strip()]
    footer = re.fullmatch(r"FAILED \(errors=([1-9][0-9]*)\)", nonblank[-1]) if nonblank else None
    if footer is None or any(line.startswith(("FAIL:", "Traceback (most recent call last):")) for line in lines):
        return ""
    counts = [match for line in lines if (match := re.fullmatch(r"Ran ([0-9]+) tests? in [0-9.]+s", line))]
    headers = [index for index, line in enumerate(lines) if line.startswith("ERROR:")]
    if (len(counts) != 1 or int(counts[0][1]) != test_count
            or int(counts[0][1]) < len(headers) or len(headers) != int(footer[1])):
        return ""
    for index in headers:
        header = re.fullmatch(r"ERROR: ([A-Za-z_][A-Za-z0-9_]*) \(unittest\.loader\._FailedTest\.([A-Za-z_][A-Za-z0-9_]*)\)", lines[index])
        if header is None or header[1] != header[2] or index + 2 >= len(lines):
            return ""
        if len(lines[index + 1]) < 3 or set(lines[index + 1]) != {"-"}:
            return ""
        body = []
        for line in lines[index + 2:]:
            if len(line) >= 3 and set(line) in ({"-"}, {"="}):
                break
            if line.strip():
                body.append(line)
        if len(body) != 1:
            return ""
        lookup = re.fullmatch(r"AttributeError: type object '([A-Za-z_][A-Za-z0-9_]*)' has no attribute '([A-Za-z_][A-Za-z0-9_]*)'", body[0])
        if lookup is None or lookup[2] != header[1]:
            return ""
        if sum(selector.endswith("." + lookup[1] + "." + lookup[2]) for selector in selectors) != 1:
            return ""
    return "unittest_method_lookup"



def is_test_selection_failure(spec: CheckSpec, fact: ExecutionFact) -> bool:
    return bool(spec.capability == "test" and "test" in fact.capabilities
                and matches_check(spec, fact) and fact.test_selection_failure)


def is_empty_test_selection(spec: CheckSpec, fact: ExecutionFact) -> bool:
    return is_test_selection_failure(spec, fact) and fact.test_selection_failure == "empty_selection"

def evaluate_check(spec: CheckSpec, fact: ExecutionFact, version: WorkspaceVersion) -> CheckDecision:
    def result(status: str, reason: str = "") -> CheckDecision:
        return CheckDecision(spec.id, status, fact.execution_id, reason,
                             fact.after.generation if status == "passed" else None)

    if not matches_check(spec, fact):
        return result("pending", "different command identity")
    if spec.capability not in fact.capabilities:
        return result("pending", "check capability not covered")
    if is_test_selection_failure(spec, fact):
        return result("pending", "no tests executed" if fact.test_count == 0 else "test selector failed to load")
    # 非零是已经发生的负事实，不能因为丢失输出或发生修改而抹掉。
    if fact.outcome == "failed" or (type(fact.exit_code) is int and fact.exit_code != 0):
        return result("blocked_environment" if fact.environment_error else "failed", fact.reason)
    if fact.outcome != "succeeded":
        return result("pending", fact.reason or "execution has no successful terminal outcome")
    if not fact.in_process and (type(fact.exit_code) is not int or fact.exit_code != 0):
        return result("pending", "process exit is unknown")
    if not fact.output_complete or not fact.reliable:
        return result("pending", "result is incomplete or cannot prove success")
    if not (fact.scope_complete and version.scope_complete
            and fact.before.scope_complete and fact.after.scope_complete):
        return result("pending", "workspace coverage is incomplete")
    if not (fact.before.acceptance_generation == fact.after.acceptance_generation
            == version.acceptance_generation):
        return result("pending", "relevant workspace version changed")
    if spec.targets and not set(spec.targets) <= set(fact.targets):
        from .identity import _django_selection
        _native, selection = _django_selection(fact.command.argv)
        translated = tuple(
            ".".join((path[6:-3].replace("/", "."), *nodes))
            if path.startswith("tests/") and path.endswith(".py") else target
            for target in spec.targets for path, *nodes in [target.split("::")]
        )
        if not (spec.native_runner_kind == "django"
                and commands_equivalent(spec.command, fact.command, native_runner_kind="django")
                and translated and set(translated) <= set(selection)):
            return result("pending", "required targets were not checked")
    if spec.capability == "test" and fact.test_count == 0:
        return result("pending", "no tests executed")
    return result("passed")
