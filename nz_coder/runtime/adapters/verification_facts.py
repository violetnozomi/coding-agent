"""把内置执行器的私有回执绑定到运行；公共 metadata 不具有事实权威。"""
from __future__ import annotations

from pathlib import Path
import re
import shlex
import sys

from nz_coder.runtime.verification.domain.identity import (
    check_output_redirected, command_identity, display_check_body, observed_check_subject,
)
from nz_coder.runtime.verification.domain.models import ExecutionFact, ExecutionReceipt, RunScope, WorkspaceVersion


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


def command_capabilities(command: str) -> tuple[str, ...]:
    """识别已支持的命令入口；不从成功文字猜测试意图。"""
    from nz_coder.intelligence.verification_planner import classify_verification_segments, verification_command_segments

    from nz_coder.runtime.agent.task_policy import _package_script_name, _python_entry

    capabilities = []
    for words in verification_command_segments(command):
        if not words:
            continue
        selected = command_identity(shlex.join(words), ".")
        if not selected.opaque:
            words = list(selected.argv)
        if not words:
            continue
        executable = Path(words[0]).name
        args = words[1:]
        if any(arg in {"--help", "-h", "--version"} for arg in args):
            continue
        entry = _python_entry(words)
        if entry is not None:
            runner, _runner_args, module = entry
            if module and runner in {"py_compile", "compileall"}:
                capability = "syntax"
            elif module and runner in {"pytest", "py.test", "unittest"}:
                capability = "test"
            elif module and runner in {"mypy", "pyright"}:
                capability = "type"
            elif module and runner == "ruff":
                capability = "lint"
            elif any(stage in {"targeted", "regression"} for stage, _ in classify_verification_segments(shlex.join(words))):
                capability = "test"
            else:
                capability = "custom_assert"
        elif executable in {"pytest", "py.test", "pytest3"}:
            capability = "test"
        elif executable == "node" and any(arg in {"--check", "-c"} for arg in args):
            capability = "syntax"
        elif (executable in {"ruff", "eslint", "pylint", "flake8"}
              or executable == "go" and args[:1] == ["vet"]
              or executable == "cargo" and args[:1] == ["clippy"]):
            capability = "lint"
        elif executable in {"mypy", "pyright", "tsc"}:
            capability = "type"
        elif executable == "cargo" and args[:1] == ["check"]:
            capability = "build"
        elif executable in {"go", "cargo"} and args[:1] == ["build"]:
            capability = "build"
        elif executable == "go" and args[:1] == ["test"] and any(arg in {"^$", "-run=^$"} for arg in args):
            capability = "build"
        elif executable in {"npm", "pnpm", "yarn"}:
            classified = classify_verification_segments(shlex.join(words))
            if _package_script_name(words) == "build":
                capability = "build"
            elif any(stage in {"targeted", "regression"} for stage, _ in classified):
                capability = "test"
            elif classified:
                action = _package_script_name(words)
                capability = {"lint": "lint", "typecheck": "type"}.get(action)
                if capability is None:
                    continue
            else:
                continue
        elif executable in {"node", "go", "cargo", "ruby", "bundle"} and classify_verification_segments(shlex.join(words)):
            capability = "test"
        elif executable in {"node", "bash", "sh", "ruby", "perl"} and args and not args[0].startswith("-"):
            capability = "custom_assert"
        elif Path(executable).suffix in {".py", ".js", ".cjs", ".mjs", ".sh", ".bash", ".rb", ".pl"}:
            capability = "custom_assert"
        else:
            continue
        classified = classify_verification_segments(shlex.join(words))
        node_syntax = executable == "node" and any(arg in {"--check", "-c"} for arg in args) and not any(
            arg in {"--help", "-h", "--version", "-v"} for arg in args)
        if capability in {"test", "syntax", "type", "lint"} and not classified and not node_syntax:
            continue
        if capability == "build" and any(arg in {"--help", "-h", "--version"} for arg in args):
            continue
        if capability not in capabilities:
            capabilities.append(capability)
    return tuple(capabilities)


def command_receipt(
    producer: str, command: str, cwd: str, *, outcome: str, exit_code: int | None,
    output: str = "", output_complete: bool = True, targets: tuple[str, ...] = (),
    capabilities: tuple[str, ...] | None = None, environment_error: bool = False,
    output_reference: str = "",
    pipefail: bool = False,
) -> ExecutionReceipt:
    from nz_coder.intelligence.verification_planner import verification_output_has_no_tests, verification_success_is_reliable
    from nz_coder.intelligence.verification_planner import classify_verification_segments, verification_command_segments
    from nz_coder.runtime.agent.task_policy import test_command_targets

    caps = command_capabilities(command) if capabilities is None else capabilities
    counts = re.findall(r"(?:Ran\s+(\d+)\s+tests?|# tests\s+(\d+)|\b(\d+) passed\b)", output)
    count = sum(int(value) for group in counts for value in group if value) if counts else None
    # TAP 的 tests 包含 skip/todo；真正执行数由 pass/fail 汇总给出。
    tap_executed = re.findall(r"^[#ℹ] (?:pass|fail)\s+(\d+)\s*$", output, re.MULTILINE)
    if tap_executed:
        count = sum(map(int, tap_executed))
    identity = command_identity(command, cwd)
    display_body = display_check_body(identity)
    single_check = len(classify_verification_segments(command)) <= 1
    if "test" in caps and verification_output_has_no_tests(output):
        count = 0
    if display_body is not None or not single_check:
        # 合并输出没有逐段来源，展示文本或其他检查的计数不能归给某个主体。
        # 零计数也是计数，不能在消除混合归属后再由展示文字补回。
        count = None
    subject = observed_check_subject(identity) if identity.opaque else None
    checked_targets = targets or tuple(test_command_targets(subject.original if subject else command))
    captured_output_complete = output_complete and not check_output_redirected(identity)
    if not checked_targets and "syntax" in caps:
        from nz_coder.runtime.agent.task_policy import _python_entry

        compiled = []
        for words in verification_command_segments(command):
            entry = _python_entry(words)
            if entry is not None and entry[2] and entry[0] == "py_compile":
                compiled.extend(arg for arg in entry[1] if not arg.startswith("-"))
        checked_targets = tuple(dict.fromkeys(compiled))
    if output_complete and exit_code is not None and exit_code != 0:
        from nz_coder.runtime.verification.evidence import is_environment_verification_failure
        from nz_coder.runtime.core.execution_context import strict_local_tools
        from nz_coder.tools.files import _get_change_tracker
        tracker = _get_change_tracker()
        environment_error = environment_error or is_environment_verification_failure(command,
            f"Command exited with code {exit_code}\n{output}",
            tuple(tracker.changed_paths()) if tracker is not None else (),
            strict_offline=strict_local_tools(), exit_code=exit_code)
    from nz_coder.runtime.verification.domain.rules import classify_test_selection_failure, validate_test_selection_attestation
    reliable = verification_success_is_reliable(command, pipefail=pipefail, cwd=cwd)
    if "test" in caps and count is None:
        for words in verification_command_segments(command):
            selected = command_identity(shlex.join(words), cwd)
            argv = selected.argv if not selected.opaque else words
            if argv and Path(argv[0]).name in {"node", "nodejs"} and "--test" in argv[1:]:
                # dot/JUnit 等输出若没有可提取的实际执行数，只证明命令退出。
                reliable = False
                break
    # 原生检查的空选择退出 5 会短路 && 展示尾段；仍要求既有主体、退出和正文共同作证。
    selection_count = 0 if display_body is not None and single_check and exit_code == 5 else count
    classification = classify_test_selection_failure(identity, outcome=outcome, exit_code=exit_code, test_count=selection_count, capabilities=caps, output=output, output_complete=captured_output_complete, reliable=reliable)
    if classification == "empty_selection":
        count = 0
    validate_test_selection_attestation(identity, outcome=outcome, exit_code=exit_code, test_count=count, capabilities=caps, output_complete=captured_output_complete, reliable=reliable, test_selection_failure=classification)
    return ExecutionReceipt(producer, command, cwd, outcome, exit_code, caps,
        checked_targets, output_complete=captured_output_complete,
        reliable=reliable,
        environment_error=environment_error or exit_code in {126, 127},
        test_count=count, output_reference=output_reference, reason=output[-2000:] or (f"Command exited with code {exit_code}" if exit_code else ""), test_selection_failure=classification)


def bind_execution_facts(
    result, scope: RunScope, before: WorkspaceVersion, after: WorkspaceVersion, *, started_revision: int | None = None,
    origin: str = "model", zone: str = "",
) -> tuple[ExecutionFact, ...]:
    if not result.executed or result.permission_denied:
        return ()
    from nz_coder.runtime.verification.domain.rules import validate_test_selection_attestation
    for receipt in result.execution_facts:
        validate_test_selection_attestation(command_identity(receipt.command, receipt.cwd), outcome=receipt.outcome, exit_code=receipt.exit_code, test_count=receipt.test_count, capabilities=receipt.capabilities, output_complete=receipt.output_complete, reliable=receipt.reliable, test_selection_failure=receipt.test_selection_failure)
    return tuple(ExecutionFact(
        receipt.execution_id, scope, command_identity(receipt.command, receipt.cwd), receipt.outcome,
        before, after, receipt.capabilities, receipt.targets, receipt.exit_code, receipt.in_process,
        receipt.test_count, receipt.output_complete, before.scope_complete and after.scope_complete,
        receipt.reliable, receipt.environment_error, receipt.output_reference, receipt.reason,
        started_revision=started_revision,
        origin=origin, zone=zone, test_selection_failure=receipt.test_selection_failure,
    ) for receipt in result.execution_facts if receipt.outcome != "started")
