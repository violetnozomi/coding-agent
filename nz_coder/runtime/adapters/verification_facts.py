"""把内置执行器的私有回执绑定到运行；公共 metadata 不具有事实权威。"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re
import shlex
import sys

from nz_coder.protocol.public_error import PublicError
from nz_coder.runtime.verification.domain.identity import (
    check_output_redirected, command_identity, display_check_body, observed_check_subject,
)
from nz_coder.runtime.verification.domain.models import CommandIdentity, ExecutionFact, ExecutionReceipt, RunScope, WorkspaceVersion


_HOST_PRODUCERS = {
    "bash": ("nz_coder.tools.bash", "run_bash"),
    "process": ("nz_coder.tools.process", "run_process"),
    "python_symbol_check": ("nz_coder.tools.python_ast", "python_symbol_check"),
    "verify_changed_files": ("nz_coder.tools.repo_intel", "verify_changed_files"),
    "verify_project_build": ("nz_coder.project_creation.verifier", "verify_project_build"),
}


def is_host_verification_tool(name: str) -> bool:
    """调用开始时识别真实内置检查器，不依赖尚未产生的回执或公开 metadata。"""
    from nz_coder.tools import TOOL_HANDLERS

    expected = _HOST_PRODUCERS.get(name)
    if expected is None:
        return False
    module = sys.modules.get(expected[0])
    return module is not None and TOOL_HANDLERS.get(name) is getattr(module, expected[1], None)


def host_execution_receipts(name: str, raw_output) -> tuple[ExecutionReceipt, ...]:
    """只接受当前内置 handler 的私有类型，动态工具不能靠同名字段升级。"""
    from nz_coder.tools import ToolOutput

    expected = _HOST_PRODUCERS.get(name)
    if expected is None or not isinstance(raw_output, ToolOutput):
        return ()
    if not is_host_verification_tool(name):
        return ()
    receipts = raw_output.execution_facts
    if not all(isinstance(item, ExecutionReceipt) and item.producer == name for item in receipts):
        return ()
    return receipts


def _runs_pytest(identity: CommandIdentity) -> bool:
    from nz_coder.runtime.agent.task_policy import _python_entry

    # 复合命令的最终退出码也可能来自别的程序，不能直接套用 pytest 的退出语义。
    if identity.opaque or not identity.argv:
        return False
    if Path(identity.argv[0]).name in {"pytest", "py.test", "pytest3"}:
        return True
    entry = _python_entry(identity.argv)
    return entry is not None and entry[2] and entry[0] in {"pytest", "py.test"}


_ANSI_COLOR = re.compile(r"\x1b\[[0-9;]*m")


def _pytest_summary_line(output: str) -> str:
    """只读末行，断言和日志里的数字不参与统计。"""
    last_line = output.rstrip().rsplit("\n", 1)[-1]
    return _ANSI_COLOR.sub("", last_line).strip("= \t\r")


def _pytest_progress_count(output: str) -> int | None:
    """-qq 只留下点号进度。须整段属于原生进度且已经到达 100%。"""
    lines = _ANSI_COLOR.sub("", output).strip().splitlines()
    if not lines:
        return None
    confirmed = 0
    previous = 0
    for line in lines:
        match = re.fullmatch(r"([.s]+)\s+\[\s*(\d{1,3})%\]", line.rstrip())
        if match is None or not previous <= int(match[2]) <= 100:
            return None
        confirmed += match[1].count(".")
        previous = int(match[2])
    return confirmed if previous == 100 else None


def _pytest_counts(summary: str) -> dict[str, int]:
    stats, separator, duration = summary.partition(" in ")
    if separator and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s(?: \([0-9a-z,: ]+\))?", duration):
        return {}
    counts = {}
    for field in stats.split(", "):
        match = re.fullmatch(r"([0-9]{1,9}) ([a-z]+)", field)
        if not match:
            return {}
        value, label = match.groups()
        counts[label] = int(value)
    return counts


def _pytest_rejected_arguments(output: str) -> bool:
    return bool(
        re.search(r"(?im)^ERROR: usage:", output)
        and re.search(r"(?im)^\S+: error: (?:unrecognized arguments?:|argument )", output)
    )


def _unittest_count(output: str) -> int | None:
    # unittest 把跳过的方法也算进 Ran；只采用运行器相邻的两行总结。
    matches = list(re.finditer(
        r"(?m)(?:\A|^-{3,}\r?\n)Ran (\d+) tests? in [\d.]+s\r?\n\r?\n"
        r"(?:OK|FAILED|NO TESTS RAN)(?: \(([^)\n]*)\))?[ \t\r]*$", output,
    ))
    # stdout 有缓冲，测试自己的 print 可能晚于 stderr 的总结。多份总结无法归属。
    if len(matches) != 1:
        return None
    summary = matches[0]
    total, details = summary.groups()
    details = details or ""
    skipped = re.search(r"(?:^|, )skipped=(\d+)(?:, |$)", details)
    # 跳过的子用例有时多于方法数。安静模式只能证明这个下界。
    confirmed = max(0, int(total) - (int(skipped[1]) if skipped else 0))
    if skipped:
        # -v 的顶层 ok 能证明该方法通过，子用例的 skip 不应扣掉另一个已通过的方法。
        verified_methods = re.findall(
            r"(?m)^\S+ \([^()\r\n]+\) \.\.\. (?:ok|expected failure|unexpected success)\r?$",
            output[:summary.start()],
        )
        confirmed = max(confirmed, min(int(total), len(verified_methods)))
    if not confirmed and re.search(r"(?:failures|errors|successes)=[1-9]", details):
        return None
    return confirmed


def _runs_unittest(identity: CommandIdentity) -> bool:
    from nz_coder.runtime.agent.task_policy import _python_entry

    entry = _python_entry(identity.argv) if not identity.opaque and identity.argv else None
    return entry is not None and entry[2] and entry[0] == "unittest"


def _before_display_output(identity: CommandIdentity, output: str) -> str | None:
    """剥离可逐字核对的展示尾段，避免把 echo 打印的数字算成测试。"""
    from nz_coder.intelligence.verification_planner import _shell_parts

    segments, _ = _shell_parts(identity.original)
    suffix = ""
    for words, _ in reversed(segments):
        if not words or words[0] not in {"echo", "printf"}:
            break
        if words[0] == "echo":
            if (len(words) > 1 and re.fullmatch(r"-[nEe]+", words[1])) or any("\\" in arg for arg in words[1:]):
                return None
            line = " ".join(words[1:]) + "\n"
        elif words[:2] == ["printf", "%s\\n"]:
            line = "\n".join(words[2:]) + "\n"
        elif len(words) == 2 and words[0] == "printf" and "%" not in words[1]:
            line = words[1].replace("\\n", "\n")
            if "\\" in line:
                return None
        else:
            return None
        suffix = line + suffix
    end = suffix.rstrip("\r\n")
    actual = output.rstrip("\r\n")
    if not end:
        return actual
    boundary = "\n" + end
    return actual[:-len(boundary)] if actual.endswith(boundary) else None


def _test_count(
    identity: CommandIdentity, output: str, exit_code: int | None, complete: bool, *, test_command: bool,
) -> int | None:
    from nz_coder.intelligence.verification_planner import verification_output_has_no_tests

    if test_command and _runs_pytest(identity):
        if not complete:
            return None
        if exit_code == 4:
            # 同一退出码也用于无效路径；只有参数解析器拒绝调用才能确定执行数为零。
            return 0 if _pytest_rejected_arguments(output) else None
        stats = _pytest_counts(_pytest_summary_line(output))
        if stats and stats.keys() <= {
            "passed", "failed", "skipped", "deselected", "xfailed", "xpassed", "warning", "warnings",
        } and stats.keys() - {"warning", "warnings"}:
            confirmed = sum(stats.get(label, 0) for label in ("passed", "failed", "xpassed"))
            return None if not confirmed and stats.get("xfailed") else confirmed
        if exit_code == 0:
            summary = _pytest_summary_line(output)
            if re.fullmatch(r"\d+ tests? collected in [\d.]+s", summary):
                return 0
            # -qq 可省略摘要，仍有整行的原生执行进度。
            progress = _pytest_progress_count(output)
            if progress is not None:
                return progress
        if exit_code == 5 and stats.get("deselected"):
            return 0
        if exit_code in {0, 5} and verification_output_has_no_tests(output):
            return 0
        return None

    unittest_count = _unittest_count(output) if complete and "Ran " in output else None
    if unittest_count is not None or (test_command and _runs_unittest(identity)):
        # 直接运行脚本也可能产生 unittest 总结。计数不会给普通脚本新增测试权限。
        return unittest_count
    if test_command and verification_output_has_no_tests(output):
        return 0
    # TAP 的 tests 包含 skip/todo，真正执行数由 pass/fail 汇总给出。
    tap_counts = re.findall(r"^[#ℹ] (?:pass|fail)\s+(\d+)\s*$", output, re.MULTILINE)
    if tap_counts:
        return sum(map(int, tap_counts))
    # 脚本内也可以调用 unittest；保留它报告的运行数。普通脚本打印的 “N passed” 缺少来源。
    counts = re.findall(r"(?:Ran\s+(\d+)\s+tests?|# tests\s+(\d+))", output)
    reported = [int(value) for group in counts for value in group if value]
    if test_command:
        reported.extend(map(int, re.findall(r"\b(\d+) passed\b", output)))
    return sum(reported) if reported else None


def bash_failure_reason(receipt: ExecutionReceipt) -> PublicError | None:
    """从宿主回执生成公开原因。断言和子进程正文仍留在给模型的工具结果中。"""
    code = receipt.exit_code
    if receipt.producer != "bash" or receipt.outcome != "failed" or type(code) is not int or code == 0:
        return None
    if not receipt.output_complete or not _runs_pytest(command_identity(receipt.command, receipt.cwd)):
        return PublicError("command_failed", f"命令以非零状态退出，退出码为 {code}。", metadata={"exit_code": code})

    metadata = {"exit_code": code, "runner": "pytest"}
    if code == 4:
        if receipt.test_count == 0:
            return PublicError(
                "command_invalid_arguments", "命令错误：参数无效，测试未运行。",
                metadata={**metadata, "tests_ran": False},
            )
        return PublicError("command_usage_error", "命令错误：pytest 报告调用方式或测试目标有误。", metadata=metadata)
    if code == 5 and receipt.test_count == 0:
        return PublicError("no_tests_collected", "pytest 未选中可运行的用例，测试未运行。",
                           metadata={**metadata, "tests_ran": False})
    if code == 1 and "test" in receipt.capabilities:
        summary = _pytest_summary_line(receipt.reason)
        counts = _pytest_counts(summary)
        failed = counts.get("failed", 0)
        errors = counts.get("error", 0) + counts.get("errors", 0)
        if failed:
            passed = counts.get("passed", 0)
            message = f"测试失败：测试已运行，{failed} 项未通过，{passed} 项通过。"
            metadata.update(tests_ran=True, failed=failed, passed=passed)
            if errors:
                message += f"另外报告了 {errors} 项测试执行错误。"
                metadata["errors"] = errors
            return PublicError("test_failed", message, metadata=metadata)
        if errors:
            return PublicError("test_execution_error", f"pytest 报告了 {errors} 项测试执行错误，详情见工具输出。",
                               metadata={**metadata, "errors": errors})
        # -qq 会省略统计，但仍逐项列出失败用例。只能确认失败，不能从这行倒推通过数。
        if re.match(r"FAILED .+::\S+", summary):
            return PublicError("test_failed", "测试失败：pytest 已列出失败用例，未报告完整统计。",
                               metadata={**metadata, "tests_ran": True})
    return PublicError("command_failed", f"pytest 退出，未取得可确认的用例结果（退出码为 {code}）。", metadata=metadata)


def _segment_arguments(command: str, cwd: str) -> Iterator[list[str]]:
    """取各命令片段的实际参数，跳过环境变量前缀。"""
    from nz_coder.intelligence.verification_planner import verification_command_segments

    for words in verification_command_segments(command):
        identity = command_identity(shlex.join(words), cwd)
        argv = words if identity.opaque else list(identity.argv)
        if argv:
            yield argv


_PYTHON_MODULE_CAPABILITIES = {
    "py_compile": "syntax", "compileall": "syntax",
    "pytest": "test", "py.test": "test", "unittest": "test",
    "mypy": "type", "pyright": "type", "ruff": "lint",
}
_EXECUTABLE_CAPABILITIES = {
    "pytest": "test", "py.test": "test", "pytest3": "test",
    "mypy": "type", "pyright": "type", "tsc": "type",
    "ruff": "lint", "eslint": "lint", "pylint": "lint", "flake8": "lint",
}


def _segment_capability(words: list[str]) -> str | None:
    from nz_coder.runtime.agent.task_policy import _package_script_name, _python_entry

    executable = Path(words[0]).name
    args = words[1:]
    if any(arg in {"--help", "-h", "--version"} for arg in args):
        return None

    def verification_stages() -> set[str]:
        from nz_coder.intelligence.verification_planner import classify_verification_segments

        return {stage for stage, _ in classify_verification_segments(shlex.join(words))}

    entry = _python_entry(words)
    if entry is not None:
        runner, _, is_module = entry
        stages = verification_stages()
        if is_module and runner in _PYTHON_MODULE_CAPABILITIES:
            # 规划器会排除只收集用例、只展示信息等调用方式。
            return _PYTHON_MODULE_CAPABILITIES[runner] if stages else None
        return "test" if stages & {"targeted", "regression"} else "custom_assert"

    if executable in {"npm", "pnpm", "yarn"}:
        script = _package_script_name(words)
        if script == "build":
            return "build"
        stages = verification_stages()
        if stages & {"targeted", "regression"}:
            return "test"
        return {"lint": "lint", "typecheck": "type"}.get(script) if stages else None

    action = (executable, args[0] if args else "")
    if action in {("go", "build"), ("cargo", "build"), ("cargo", "check")}:
        return "build"
    if action == ("go", "test") and any(arg in {"^$", "-run=^$"} for arg in args):
        # 这个选择模式用于只编译测试包。
        return "build"

    capability = _EXECUTABLE_CAPABILITIES.get(executable)
    if action in {("go", "vet"), ("cargo", "clippy")}:
        capability = "lint"
    elif executable == "node" and any(arg in {"--check", "-c"} for arg in args):
        # 单独的 node --check 也能证明语法检查。-v 会显示版本，须有规划器分类才能采用。
        if "-v" not in args:
            return "syntax"
        capability = "syntax"
    if capability is not None:
        return capability if verification_stages() else None
    if executable in {"node", "go", "cargo", "ruby", "bundle"} and verification_stages():
        return "test"
    if executable in {"node", "bash", "sh", "ruby", "perl"} and args and not args[0].startswith("-"):
        return "custom_assert"
    if Path(executable).suffix in {".py", ".js", ".cjs", ".mjs", ".sh", ".bash", ".rb", ".pl"}:
        return "custom_assert"
    return None


def command_capabilities(command: str) -> tuple[str, ...]:
    """识别命令入口，按片段中首次出现的顺序返回检查能力。"""
    capabilities = []
    for words in _segment_arguments(command, "."):
        capability = _segment_capability(words)
        if capability is not None and capability not in capabilities:
            capabilities.append(capability)
    return tuple(capabilities)


def _inferred_targets(command: str, identity: CommandIdentity, capabilities: tuple[str, ...]) -> tuple[str, ...]:
    from nz_coder.intelligence.verification_planner import verification_command_segments
    from nz_coder.runtime.agent.task_policy import _python_entry, test_command_targets

    subject = observed_check_subject(identity) if identity.opaque else None
    targets = test_command_targets(subject.original if subject else command)
    if targets or "syntax" not in capabilities:
        return targets

    compiled = []
    for words in verification_command_segments(command):
        entry = _python_entry(words)
        if entry is None:
            continue
        runner, args, is_module = entry
        if is_module and runner == "py_compile":
            compiled.extend(arg for arg in args if not arg.startswith("-"))
    return tuple(dict.fromkeys(compiled))


def _validate_receipt_selection(identity: CommandIdentity, receipt: ExecutionReceipt) -> None:
    from nz_coder.runtime.verification.domain.rules import validate_test_selection_attestation

    validate_test_selection_attestation(
        identity,
        outcome=receipt.outcome,
        exit_code=receipt.exit_code,
        test_count=receipt.test_count,
        capabilities=receipt.capabilities,
        output_complete=receipt.output_complete,
        reliable=receipt.reliable,
        test_selection_failure=receipt.test_selection_failure,
    )


def command_receipt(
    producer: str,
    command: str,
    cwd: str,
    *,
    outcome: str,
    exit_code: int | None,
    output: str = "",
    output_complete: bool = True,
    targets: tuple[str, ...] = (),
    capabilities: tuple[str, ...] | None = None,
    environment_error: bool = False,
    output_reference: str = "",
    pipefail: bool = False,
) -> ExecutionReceipt:
    from nz_coder.intelligence.verification_planner import classify_verification_segments, verification_success_is_reliable
    from nz_coder.runtime.verification.domain.rules import classify_test_selection_failure

    if capabilities is None:
        capabilities = command_capabilities(command)
    identity = command_identity(command, cwd)
    has_display_tail = display_check_body(identity) is not None
    subject = observed_check_subject(identity) or identity
    single_check = len(classify_verification_segments(command)) <= 1
    captured_output_complete = output_complete and not check_output_redirected(identity)

    # 拼接输出没有逐段来源，只有能归到同一检查的统计才进入回执。
    test_count = None
    measured_output = output
    if has_display_tail:
        measured_output = _before_display_output(identity, output) if exit_code == 0 else None
    if single_check and measured_output is not None:
        test_count = _test_count(
            subject, measured_output, exit_code=exit_code, complete=captured_output_complete, test_command="test" in capabilities,
        )
    checked_targets = targets or _inferred_targets(command, identity, capabilities)

    if output_complete and exit_code is not None and exit_code != 0:
        from nz_coder.runtime.core.execution_context import strict_local_tools
        from nz_coder.runtime.verification.evidence import is_environment_verification_failure
        from nz_coder.tools.files import _get_change_tracker

        tracker = _get_change_tracker()
        environment_error = environment_error or is_environment_verification_failure(
            command,
            f"Command exited with code {exit_code}\n{output}",
            tuple(tracker.changed_paths()) if tracker is not None else (),
            strict_offline=strict_local_tools(),
            exit_code=exit_code,
        )

    reliable = verification_success_is_reliable(command, pipefail=pipefail, cwd=cwd)
    if "test" in capabilities and test_count is None:
        if exit_code == 0 and single_check and (_runs_pytest(subject) or _runs_unittest(subject)):
            reliable = False
        for argv in _segment_arguments(command, cwd):
            if Path(argv[0]).name in {"node", "nodejs"} and "--test" in argv[1:]:
                # dot/JUnit 等报告缺少实际执行数时，只能确认命令退出。
                reliable = False
                break

    # 原生检查退出 5 时，&& 后的展示命令不会运行。先用零计数检查空选择，正文也证实后才写入回执。
    selection_count = 0 if has_display_tail and single_check and exit_code == 5 else test_count
    selection_failure = classify_test_selection_failure(
        identity,
        outcome=outcome,
        exit_code=exit_code,
        test_count=selection_count,
        capabilities=capabilities,
        output=output,
        output_complete=captured_output_complete,
        reliable=reliable,
    )
    if selection_failure == "empty_selection":
        test_count = 0

    reason = output[-2000:]
    if not reason and exit_code:
        reason = f"Command exited with code {exit_code}"
    receipt = ExecutionReceipt(
        producer=producer,
        command=command,
        cwd=cwd,
        outcome=outcome,
        exit_code=exit_code,
        capabilities=capabilities,
        targets=checked_targets,
        output_complete=captured_output_complete,
        reliable=reliable,
        environment_error=environment_error or exit_code in {126, 127},
        test_count=test_count,
        output_reference=output_reference,
        reason=reason,
        test_selection_failure=selection_failure,
    )
    _validate_receipt_selection(identity, receipt)
    return receipt


def bind_execution_facts(
    result, scope: RunScope, before: WorkspaceVersion, after: WorkspaceVersion, *,
    started_revision: int | None = None, origin: str = "model", zone: str = "",
) -> tuple[ExecutionFact, ...]:
    if not result.executed or result.permission_denied:
        return ()

    facts = []
    for receipt in result.execution_facts:
        identity = command_identity(receipt.command, receipt.cwd)
        _validate_receipt_selection(identity, receipt)
        # 启动回执也要经过校验，等进程结束后才生成执行事实。
        if receipt.outcome == "started":
            continue
        facts.append(ExecutionFact(
            execution_id=receipt.execution_id,
            scope=scope,
            command=identity,
            outcome=receipt.outcome,
            before=before,
            after=after,
            capabilities=receipt.capabilities,
            targets=receipt.targets,
            exit_code=receipt.exit_code,
            in_process=receipt.in_process,
            test_count=receipt.test_count,
            output_complete=receipt.output_complete,
            scope_complete=before.scope_complete and after.scope_complete,
            reliable=receipt.reliable,
            environment_error=receipt.environment_error,
            output_reference=receipt.output_reference,
            reason=receipt.reason,
            started_revision=started_revision,
            origin=origin,
            zone=zone,
            test_selection_failure=receipt.test_selection_failure,
        ))
    return tuple(facts)
