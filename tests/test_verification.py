"""tests/test_verification.py — VerificationManager 单元测试。

覆盖写文件触发验证需求、scratch 文件豁免、bash 验证命令清除/设置状态、
python_symbol_check 结果、环境错误不覆盖已通过的验证等场景。
"""
from unittest.mock import MagicMock

from nz_coder.verification import VerificationManager


# ── 测试辅助 ────────────────────────────────────────────────────────────────

def make_vm(plan: dict | None = None, plan_builder=None) -> VerificationManager:
    """创建一个带 mock 依赖的 VerificationManager。"""
    recovery = MagicMock()
    recovery.verification_gate_message.return_value = "Please verify your changes."
    tracer = MagicMock()
    if plan is not None:
        def plan_builder(_changed_files):
            return plan
    vm = VerificationManager(recovery, tracer, plan_builder=plan_builder)
    vm.reset()
    return vm


def staged_plan(
    static: tuple[str, ...] = (),
    targeted: tuple[str, ...] = (),
    optional_targeted: tuple[str, ...] = (),
    regression: tuple[str, ...] = (),
) -> dict:
    """Build a deterministic planner result for gate-state tests."""
    def items(commands: tuple[str, ...], required: bool) -> list[dict]:
        return [
            {"command": command, "reason": "test", "required": required}
            for command in commands
        ]

    return {
        "recommended": [],
        "fallback": [],
        "notes": [],
        "stages": [
            {"name": "static", "required": bool(static), "commands": items(static, True)},
            {
                "name": "targeted",
                "required": bool(targeted),
                "commands": items(targeted, True) + items(optional_targeted, False),
            },
            {"name": "regression", "required": False, "commands": items(regression, False)},
        ],
    }


# ── 基础写文件场景 ────────────────────────────────────────────────────────────

def test_no_gate_before_any_write():
    """初始状态不需要 gate。"""
    vm = make_vm()
    assert not vm.should_gate()


def test_verification_needed_after_write():
    """write_file 写非 scratch 文件后，should_gate() 应返回 True。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    assert vm.should_gate()


def test_scratch_file_no_verification():
    """根目录 test_*.py scratch 文件写入不应触发 gate。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "test_scratch.py"})
    assert not vm.should_gate()


def test_root_md_no_verification():
    """根目录 *.md 文档文件写入不触发 gate。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "CHANGES.md"})
    assert not vm.should_gate()


def test_edit_file_always_triggers():
    """edit_file 无论路径都视为实质修改，应触发 gate。"""
    vm = make_vm()
    vm.mark_write("edit_file", {"path": "test_something.py"})
    assert vm.should_gate()


def test_apply_patch_dry_run_does_not_trigger_verification():
    """纯预览没有改变工作区，不应打开 completion gate。"""
    vm = make_vm()
    vm.mark_write("apply_patch", {"changes": [{"path": "pkg/module.py"}], "dry_run": True})
    assert not vm.should_gate()


# ── bash 验证命令 ─────────────────────────────────────────────────────────────

def _bash_pass(vm: VerificationManager, command: str) -> None:
    """模拟 bash 验证命令执行成功。"""
    vm.observe_bash({"command": command}, "1 passed", False, False)


def _bash_fail(vm: VerificationManager, command: str, output: str = "1 failed") -> None:
    """模拟 bash 验证命令执行失败（非零退出）。"""
    vm.observe_bash({"command": command}, output, False, True)


def test_py_compile_clears_verification():
    """写文件后 py_compile 通过，should_gate() 应返回 False。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    assert vm.should_gate()
    _bash_pass(vm, "python3 -m py_compile nz_coder/loop.py")
    assert not vm.should_gate()


def test_pytest_failed_sets_verification():
    """写文件后 pytest 失败，should_gate() 应返回 True。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    _bash_pass(vm, "python3 -m py_compile nz_coder/loop.py")
    assert not vm.should_gate()
    _bash_fail(vm, "pytest tests/")
    assert vm.should_gate()


def test_module_not_found_reopens_gate():
    """ModuleNotFoundError 应作为失败验证反馈重新打开 gate。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    _bash_pass(vm, "python3 -m py_compile nz_coder/loop.py")
    assert not vm.should_gate()

    env_error_output = (
        "Command exited with code 1\n"
        "ModuleNotFoundError: No module named 'requests'"
    )
    _bash_fail(vm, "pytest tests/", env_error_output)
    assert vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "failed"


def test_traceback_output_with_zero_exit_is_not_a_pass():
    """管道吞掉非零退出码时，Traceback 输出仍应视为失败。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    vm.observe_bash(
        {"command": "pytest tests/ | tail -30"},
        "Traceback (most recent call last):\nAssertionError: bad",
        False,
        False,
    )
    assert vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "failed"


def test_no_module_named_output_with_zero_exit_keeps_gate_open():
    """输出 No module named 且退出 0 时，仍应视为失败反馈。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    vm.observe_bash(
        {"command": "python3 -c \"import django; print('Django setup failed: No module named cgi')\""},
        "Django setup failed: No module named 'cgi'",
        False,
        False,
    )
    assert vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "failed"


def test_no_module_named_output_with_zero_exit_replaces_prior_pass():
    """输出 No module named 且退出 0 时，应覆盖之前的通过状态为失败。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    _bash_pass(vm, "python3 -m py_compile nz_coder/loop.py")
    assert not vm.should_gate()

    vm.observe_bash(
        {"command": "python3 -c \"import django; print('Django setup failed: No module named cgi')\""},
        "Django setup failed: No module named 'cgi'",
        False,
        False,
    )
    assert vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "failed"
    assert vm.status()["last_verification"]["command"].startswith("python3 -c")


def test_manual_fail_output_with_zero_exit_is_not_a_pass():
    """手写验证脚本打印 FAIL 但退出 0 时，不应误判为验证通过。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    vm.observe_bash(
        {"command": "python3 -c \"import sys; print('Test 1 FAIL: wrong')\""},
        "Test 1 FAIL: wrong\nTest 2 PASS",
        False,
        False,
    )
    assert vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "failed"


# ── python_symbol_check ───────────────────────────────────────────────────────

def test_symbol_check_passed_clears():
    """python_symbol_check 返回 OK 后，should_gate() 应为 False。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    assert vm.should_gate()
    vm.observe_symbol_check("OK: all symbols verified")
    assert not vm.should_gate()


def test_symbol_check_failed_keeps_gate():
    """python_symbol_check 返回非 OK，should_gate() 保持 True。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    vm.observe_symbol_check("Error: symbol not found")
    assert vm.should_gate()


def test_symbol_check_only_completes_static_command_for_its_path():
    """单文件 symbol check 不能替另一个变更文件完成 static。"""
    vm = make_vm(staged_plan(static=(
        "python -m py_compile pkg/a.py",
        "python -m py_compile pkg/b.py",
    )))
    vm.mark_write("write_files_batch", {"files": [{"path": "pkg/a.py"}, {"path": "pkg/b.py"}]})

    vm.observe_symbol_check("OK: symbols verified", {"path": "pkg/a.py"})

    assert vm.should_gate()
    static = vm.status()["verification_pipeline"]["stages"][0]
    assert [item["status"] for item in static["commands"]] == ["passed", "pending"]


def test_verify_changed_files_ok_clears_gate():
    """verify_changed_files OK 应清除 verification gate。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    assert vm.should_gate()
    vm.observe_verify_changed_files("OK: py_compile changed files\nOK  nz_coder/loop.py")
    assert not vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "passed"


def test_verify_changed_files_failure_reopens_gate_after_prior_pass():
    """verify_changed_files FAIL 不应被之前的通过状态掩盖。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    vm.observe_symbol_check("OK: all symbols verified")
    assert not vm.should_gate()

    vm.observe_verify_changed_files("FAIL: py_compile changed files\nFAIL nz_coder/loop.py")

    assert vm.should_gate()
    assert vm.status()["last_verification"]["status"] == "failed"


# ── Gate 提示计数 ─────────────────────────────────────────────────────────────

def test_gate_prompt_counter():
    """increment_gate_prompt 每调一次加一，reset 后归零。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "src/main.py"})
    assert vm.gate_prompts == 0
    assert vm.increment_gate_prompt() == 1
    assert vm.increment_gate_prompt() == 2
    vm.reset()
    assert vm.gate_prompts == 0


# ── status() 快照 ──────────────────────────────────────────────────────────────

def test_status_reflects_state():
    """status() 返回 verification_needed 和 last_verification 字段。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        regression=("pytest",),
    ))
    s = vm.status()
    assert s["verification_needed"] is False
    assert s["last_verification"] is None

    vm.mark_write("write_file", {"path": "pkg/module.py"})
    s = vm.status()
    assert s["verification_needed"] is True

    _bash_pass(vm, "pytest tests/")
    s = vm.status()
    assert s["verification_needed"] is True
    assert s["last_verification"] is not None
    assert s["last_verification"]["status"] == "passed"

    _bash_pass(vm, "python -m py_compile pkg/module.py")
    s = vm.status()
    assert s["verification_needed"] is False
    assert s["verification_pipeline"]["stages"]


# ── 分层验证流水线 ───────────────────────────────────────────────────────────

def test_static_pass_waits_for_required_target():
    """静态检查不能替代已明确要求的目标测试。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        targeted=("pytest tests/test_module.py::test_fix",),
        regression=("pytest",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_pass(vm, "python3 -m py_compile pkg/module.py")
    assert vm.should_gate()
    assert vm.status()["verification_pipeline"]["next_required_stage"] == "targeted"

    _bash_pass(vm, "python -m pytest -q tests/test_module.py::test_fix")
    assert not vm.should_gate()


def test_target_pass_waits_for_static_stage():
    """目标测试先通过时，尚未执行的静态检查仍保持 gate。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        targeted=("pytest tests/test_module.py::test_fix",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_pass(vm, "pytest tests/test_module.py::test_fix")
    assert vm.should_gate()

    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert not vm.should_gate()


def test_all_required_static_commands_must_pass():
    """一个文件的 py_compile 不得代表所有变更文件都已验证。"""
    vm = make_vm(staged_plan(static=(
        "python -m py_compile pkg/a.py",
        "python -m py_compile pkg/b.py",
    )))
    vm.mark_write("write_files_batch", {"files": [{"path": "pkg/a.py"}, {"path": "pkg/b.py"}]})

    _bash_pass(vm, "python -m py_compile pkg/a.py")
    assert vm.should_gate()
    _bash_pass(vm, "python3 -m py_compile pkg/b.py")
    assert not vm.should_gate()


def test_verify_changed_files_passes_whole_static_stage():
    """聚合检查成功可以一次覆盖全部 static required command。"""
    vm = make_vm(staged_plan(static=(
        "python -m py_compile pkg/a.py",
        "python -m py_compile pkg/b.py",
    )))
    vm.mark_write("apply_patch", {"patch": "*** Update File: pkg/a.py\n*** Update File: pkg/b.py"})

    vm.observe_verify_changed_files("OK: py_compile changed files")

    assert not vm.should_gate()
    static = vm.status()["verification_pipeline"]["stages"][0]
    assert all(item["status"] == "passed" for item in static["commands"])


def test_optional_regression_failure_is_sticky_until_same_command_passes():
    """主动运行的可选回归一旦失败，不能被另一阶段的成功掩盖。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        regression=("pytest",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert not vm.should_gate()

    _bash_fail(vm, "pytest")
    assert vm.should_gate()
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert vm.should_gate()

    _bash_pass(vm, "pytest")
    assert not vm.should_gate()


def test_gate_reprints_failed_planned_optional_command():
    """已主动运行且失败的 optional command 应出现在重跑提示中。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        regression=("pytest",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    _bash_fail(vm, "pytest")

    message = vm.make_gate_message()

    assert "Failed checks that must be rerun after fixing:" in message
    assert "pytest" in message


def test_new_write_resets_previous_stage_results():
    """验证通过后再次修改代码，所需 stage 必须重新执行。"""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert not vm.should_gate()

    vm.mark_write("edit_file", {"path": "pkg/module.py"})

    assert vm.should_gate()
    static = vm.status()["verification_pipeline"]["stages"][0]
    assert static["commands"][0]["status"] == "pending"


def test_failed_target_before_edit_is_required_after_edit():
    """修复前观察到的精确失败测试必须在修改后重跑。"""
    plan = staged_plan(static=("python -m py_compile pkg/module.py",))
    vm = make_vm(plan)
    _bash_fail(
        vm,
        "pytest tests/test_module.py::test_fix",
        "FAILED tests/test_module.py::test_fix - AssertionError",
    )

    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert vm.should_gate()

    _bash_pass(vm, "pytest tests/test_module.py::test_fix")
    assert not vm.should_gate()


def test_failed_target_upgrades_matching_optional_related_command():
    """真实失败与 related 候选重合时，必须把原 optional item 升级为 required。"""
    target = "pytest tests/test_module.py::test_fix"
    plan = staged_plan(
        static=("python -m py_compile pkg/module.py",),
        optional_targeted=(target,),
    )
    vm = make_vm(plan)
    _bash_fail(vm, target, "FAILED tests/test_module.py::test_fix - AssertionError")

    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["commands"][0]["required"] is True
    assert plan["stages"][1]["commands"][0]["required"] is False

    _bash_pass(vm, "python -m pytest -q tests/test_module.py::test_fix")
    assert not vm.should_gate()


def test_planner_failure_falls_back_to_legacy_single_check_semantics():
    """planner 异常不得让未知项目永久卡在 completion gate。"""
    def broken_plan(_changed_files):
        raise RuntimeError("profile unavailable")

    vm = make_vm(plan_builder=broken_plan)
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "pytest")

    assert not vm.should_gate()


def test_gate_message_lists_required_checks_not_optional_broad_runner():
    """gate 不应建议会被 broad-test policy 拦截的可选全量测试。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        regression=("pytest",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    message = vm.make_gate_message()

    assert "python -m py_compile pkg/module.py" in message
    assert "pytest" not in message


def test_text_search_for_pytest_is_not_verification():
    """echo/rg 中出现 pytest 文本不能误清 verification gate。"""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    vm.observe_bash({"command": "rg pytest tests"}, "tests/test_a.py", False, False)

    assert vm.should_gate()
    assert vm.status()["last_verification"] is None


def test_printed_required_command_inside_compound_does_not_count_as_executed():
    """打印计划命令再运行另一 static 工具，不能伪造 required command 覆盖。"""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    vm.observe_bash(
        {"command": "echo 'python -m py_compile pkg/module.py' && ruff check pkg/module.py"},
        "All checks passed!",
        False,
        False,
    )

    assert vm.should_gate()
    static = vm.status()["verification_pipeline"]["stages"][0]
    assert static["commands"][0]["status"] == "pending"


def test_compound_static_and_targeted_commands_complete_both_stages():
    """同一 shell 调用里的真实 static 与 targeted segment 都应独立记账。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        targeted=("pytest tests/test_module.py::test_fix",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_pass(
        vm,
        "python3 -m py_compile pkg/module.py && "
        "python -m pytest -q tests/test_module.py::test_fix",
    )

    assert not vm.should_gate()
    stages = vm.status()["verification_pipeline"]["stages"]
    assert [stage["status"] for stage in stages[:2]] == ["passed", "passed"]


def test_semantic_command_matching_handles_wrapper_and_argument_order():
    """wrapper 与无关参数顺序差异不能导致已执行命令永远 pending。"""
    vm = make_vm(staged_plan(static=("go test ./pkg -run '^$'",)))
    vm.mark_write("write_file", {"path": "pkg/main.go"})

    _bash_pass(vm, "env GOFLAGS=-mod=mod go test -run '^$' ./pkg")

    assert not vm.should_gate()


def test_shell_or_chain_cannot_hide_failed_static_check():
    """最终 true 的退出码不能把 ``cargo check || true`` 当成通过。"""
    vm = make_vm(staged_plan(static=("cargo check",)))
    vm.mark_write("write_file", {"path": "src/lib.rs"})

    vm.observe_bash(
        {"command": "cargo check || true"},
        "error: could not compile demo",
        False,
        False,
    )

    assert vm.should_gate()
    static = vm.status()["verification_pipeline"]["stages"][0]
    assert static["commands"][0]["status"] == "pending"


def test_non_execution_compile_help_cannot_clear_static_gate():
    """py_compile --help 只展示帮助，不能证明文件可编译。"""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_pass(vm, "python -m py_compile --help pkg/module.py")

    assert vm.should_gate()
    assert vm.status()["last_verification"] is None


def test_pytest_collect_only_cannot_clear_required_target():
    """pytest collect-only 没有执行测试，required target 必须保持 pending。"""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        targeted=("pytest tests/test_module.py::test_fix",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_pass(vm, "pytest --collect-only tests/test_module.py::test_fix")

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["commands"][0]["status"] == "pending"


def test_bash_dispatch_failure_is_not_an_executed_verification():
    """策略阻断等 dispatch failure 只保留 pending，不制造测试失败证据。"""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    vm.observe_bash(
        {"command": "python -m py_compile pkg/module.py"},
        "Error: command blocked by policy",
        True,
        False,
    )

    assert vm.should_gate()
    assert vm.status()["last_verification"] is None


def test_cargo_harness_list_cannot_clear_required_target():
    """Cargo libtest --list 只枚举测试，不能满足 required target。"""
    vm = make_vm(staged_plan(targeted=("cargo test test_parser",)))
    vm.mark_write("write_file", {"path": "src/lib.rs"})

    _bash_pass(vm, "cargo test test_parser -- --list")

    assert vm.should_gate()
    assert vm.status()["last_verification"] is None


def test_extra_pytest_selector_cannot_claim_exact_target_coverage():
    """新增 -k 可能排除 required test，不能靠 token 子集误判为覆盖。"""
    target = "pytest tests/test_module.py::test_fix"
    vm = make_vm(staged_plan(targeted=(target,)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_pass(vm, target + " -k never_matches")

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["commands"][0]["status"] == "pending"
