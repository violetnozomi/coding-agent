"""tests/test_verification.py — VerificationManager 单元测试。

覆盖写文件触发验证需求、scratch 文件豁免、bash 验证命令清除/设置状态、
python_symbol_check 结果、环境错误不覆盖已通过的验证等场景。
"""
from unittest.mock import MagicMock

import pytest

from nz_coder.intelligence.verification import VerificationManager


# ── 测试辅助 ────────────────────────────────────────────────────────────────

def make_vm(
    plan: dict | None = None,
    plan_builder=None,
    *,
    require_targeted: bool = False,
) -> VerificationManager:
    """创建一个带 mock 依赖的 VerificationManager。"""
    recovery = MagicMock()
    recovery.verification_gate_message.return_value = "Please verify your changes."
    tracer = MagicMock()
    if plan is not None:
        def plan_builder(_changed_files):
            return plan
    vm = VerificationManager(
        recovery,
        tracer,
        plan_builder=plan_builder,
        require_targeted=require_targeted,
    )
    vm.reset()
    return vm


def staged_plan(
    static: tuple[str, ...] = (),
    targeted: tuple[str, ...] = (),
    optional_targeted: tuple[str, ...] = (),
    regression: tuple[str, ...] = (),
    native_runner_kind: str = "",
) -> dict:
    """Build a deterministic planner result for gate-state tests."""
    def items(commands: tuple[str, ...], required: bool) -> list[dict]:
        return [
            {"command": command, "reason": "test", "required": required}
            for command in commands
        ]

    plan = {
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
    if native_runner_kind:
        plan["native_runner_kind"] = native_runner_kind
    return plan


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
    vm.mark_write(
        "write_file",
        {"path": "test_scratch.py"},
        output="Created test_scratch.py (5 bytes)",
    )
    assert not vm.should_gate()


def test_nested_ephemeral_scratch_lifecycle_preserves_verification():
    """Creating and deleting an explicit scratch test must not stale source evidence."""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert not vm.should_gate()

    scratch = "scratch/probe_scratch.py"
    vm.mark_write(
        "write_file",
        {"path": scratch, "content": "def test_fix(): pass\n"},
        output=f"Created {scratch} (21 bytes)",
    )
    assert not vm.should_gate()

    vm.mark_write(
        "apply_patch",
        {"changes": [{"path": scratch, "op": "delete", "old_text": "test"}]},
    )
    assert not vm.should_gate()
    assert vm.status()["verification_pipeline"]["changed_files"] == [
        "pkg/module.py",
    ]


def test_editing_existing_nested_scratch_file_reopens_verification():
    """An existing scratch-named file is a normal mutation when edited."""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    scratch = "tests/pkg/test_module_scratch.py"
    vm.mark_write("edit_file", {"path": scratch})

    assert vm.should_gate()
    assert vm.status()["verification_pipeline"]["changed_files"] == [
        "pkg/module.py",
        scratch,
    ]


def test_replacing_existing_nested_scratch_file_reopens_verification():
    """A replace patch cannot claim the temporary create/delete exemption."""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    scratch = "tests/pkg/test_module_scratch.py"
    vm.mark_write(
        "apply_patch",
        {"changes": [{"path": scratch, "op": "replace", "old_text": "old"}]},
    )

    assert vm.should_gate()
    assert scratch in vm.status()["verification_pipeline"]["changed_files"]


def test_contradictory_scratch_write_output_reopens_verification():
    """A contradictory write result cannot prove that a scratch file was new."""
    vm = make_vm()
    scratch = "scratch/probe_scratch.py"

    vm.mark_write(
        "write_file",
        {"path": scratch},
        output=(
            f"Updated {scratch} (21 bytes)\n"
            f"Created {scratch} (21 bytes)"
        ),
    )

    assert vm.should_gate()


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


def _bash_fail(
    vm: VerificationManager,
    command: str,
    output: str = "1 failed",
    *,
    exit_code: int = 1,
) -> None:
    """模拟 bash 验证命令执行失败（非零退出）。"""
    prefix = f"Command exited with code {exit_code}"
    wrapped_output = output if output.startswith(prefix) else f"{prefix}\n{output}"
    vm.observe_bash(
        {"command": command},
        wrapped_output,
        False,
        True,
        exit_code=exit_code,
    )


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


def test_strict_missing_checkout_dependency_blocks_environment():
    """Offline SWE verification cannot repair a dependency absent from the host."""
    command = "python3 -m pytest tests/lint/unittest_lint.py -q"
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pylint/lint/pylinter.py",),
            targeted=(command,),
        ),
        require_targeted=True,
    )
    vm.mark_write("edit_file", {"path": "pylint/lint/pylinter.py"})
    _bash_pass(vm, "python -m py_compile pylint/lint/pylinter.py")

    _bash_fail(
        vm,
        command,
        (
            "ImportError while loading conftest 'tests/conftest.py'.\n"
            "tests/conftest.py:14: in <module>\n"
            "    from pylint import checkers\n"
            "pylint/checkers/base_checker.py:15: in <module>\n"
            "    from astroid import nodes\n"
            "E   ModuleNotFoundError: No module named 'astroid'\n"
        ),
        exit_code=4,
    )

    status = vm.status()
    assert status["verification_state"] == "blocked_environment"
    assert status["last_verification"]["status"] == "blocked_environment"
    assert status["environment_blocker"]["command"] == command
    assert status["environment_blocker"]["stage"] == "targeted"


def test_strict_runner_startup_traceback_exit_one_blocks_environment():
    """A missing dependency before test startup is infrastructure even at exit 1."""
    command = (
        "python3 -m pytest "
        "testing/test_terminal.py::TestCollectOnly::test_collectonly_basic -q"
    )
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile src/_pytest/main.py",),
            targeted=(command,),
        ),
        require_targeted=True,
    )
    vm.mark_write("edit_file", {"path": "src/_pytest/main.py"})
    _bash_pass(vm, "python -m py_compile src/_pytest/main.py")

    _bash_fail(
        vm,
        command,
        (
            "Traceback (most recent call last):\n"
            "  File \"src/pytest.py\", line 6, in <module>\n"
            "    from _pytest.assertion import register_assert_rewrite\n"
            "  File \"src/_pytest/assertion/rewrite.py\", line 23, in <module>\n"
            "    import atomicwrites\n"
            "ModuleNotFoundError: No module named 'atomicwrites'"
        ),
        exit_code=1,
    )

    status = vm.status()
    assert status["verification_state"] == "blocked_environment"
    assert status["last_verification"]["status"] == "blocked_environment"
    assert status["environment_blocker"]["command"] == command
    assert status["environment_blocker"]["stage"] == "targeted"


def test_strict_runner_wrapped_missing_module_exit_one_blocks_environment():
    """A runner wrapper must not hide its chained missing host dependency."""
    command = "python3 tests/runtests.py auth_tests.test_migrations -v1"
    changed_file = "django/contrib/auth/migrations/0011_update_proxy_permissions.py"
    vm = make_vm(
        staged_plan(targeted=(command,)),
        require_targeted=True,
    )
    vm.mark_write("edit_file", {"path": changed_file})

    _bash_fail(
        vm,
        command,
        (
            "Traceback (most recent call last):\n"
            "  File \"tests/runtests.py\", line 14, in <module>\n"
            "    import django\n"
            "ModuleNotFoundError: No module named 'django'\n\n"
            "The above exception was the direct cause of the following exception:\n\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/runtests.py\", line 16, in <module>\n"
            "    raise RuntimeError(\n"
            "        'Django module not found, reference tests/README.rst for instructions.'\n"
            "    ) from e\n"
            "RuntimeError: Django module not found, reference tests/README.rst for "
            "instructions."
        ),
        exit_code=1,
    )

    status = vm.status()
    assert status["verification_state"] == "blocked_environment"
    assert status["last_verification"]["status"] == "blocked_environment"
    assert status["environment_blocker"]["stage"] == "targeted"


@pytest.mark.parametrize("changed_file", [
    "tests/runtests.py",
    "./tests/runtests.py",
])
def test_strict_wrapped_missing_module_from_changed_runner_is_repairable(
    changed_file,
):
    """A wrapper edited by the patch remains code-owned failure evidence."""
    command = "python3 tests/runtests.py auth_tests.test_migrations -v1"
    vm = make_vm(staged_plan(targeted=(command,)), require_targeted=True)
    vm.mark_write("edit_file", {"path": changed_file})

    _bash_fail(
        vm,
        command,
        (
            "Traceback (most recent call last):\n"
            "  File \"tests/runtests.py\", line 14, in <module>\n"
            "    import django\n"
            "ModuleNotFoundError: No module named 'django'\n\n"
            "The above exception was the direct cause of the following exception:\n\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/runtests.py\", line 16, in <module>\n"
            "    raise RuntimeError('Django module not found') from e\n"
            "RuntimeError: Django module not found"
        ),
        exit_code=1,
    )

    status = vm.status()
    assert status["verification_state"] == "failed_repairable"
    assert status["environment_blocker"] is None


def test_strict_missing_module_from_changed_file_remains_repairable():
    """A bad import introduced by the patch is code failure, not infrastructure."""
    command = "python3 -m pytest tests/lint/unittest_lint.py -q"
    vm = make_vm(
        staged_plan(targeted=(command,)),
        require_targeted=True,
    )
    vm.mark_write("edit_file", {"path": "pylint/lint/pylinter.py"})

    _bash_fail(
        vm,
        command,
        (
            "pylint/lint/pylinter.py:20: in <module>\n"
            "    import does_not_exist\n"
            "E   ModuleNotFoundError: No module named 'does_not_exist'\n"
        ),
        exit_code=4,
    )

    status = vm.status()
    assert status["verification_state"] == "failed_repairable"
    assert status["environment_blocker"] is None


def test_strict_incompatible_host_pytest_config_blocks_environment():
    """A checkout warning category removed from host pytest is infrastructure."""
    command = "python3 -m pytest -q testing/test_assertion.py"
    vm = make_vm(staged_plan(targeted=(command,)), require_targeted=True)
    vm.mark_write("edit_file", {"path": "src/_pytest/assertion/util.py"})

    _bash_fail(
        vm,
        command,
        (
            "Command exited with code 4\n"
            "ERROR: while parsing the following warning configuration:\n"
            "  ignore:deprecated:pytest.RemovedInPytest4Warning\n"
            "Traceback (most recent call last):\n"
            "  File \"/venv/site-packages/_pytest/config/__init__.py\", line 1\n"
            "AttributeError: module 'pytest' has no attribute "
            "'RemovedInPytest4Warning'\n"
        ),
        exit_code=4,
    )

    assert vm.status()["verification_state"] == "blocked_environment"


def test_strict_changed_pytest_config_failure_remains_repairable():
    """A warning-filter error caused by an edited config still belongs to code."""
    command = "python3 -m pytest -q tests/test_parser.py"
    vm = make_vm(staged_plan(targeted=(command,)), require_targeted=True)
    vm.mark_write("edit_file", {"path": "tox.ini"})

    _bash_fail(
        vm,
        command,
        (
            "ERROR: while parsing the following warning configuration:\n"
            "AttributeError: module 'pytest' has no attribute 'BadWarning'\n"
        ),
        exit_code=4,
    )

    assert vm.status()["verification_state"] == "failed_repairable"


def test_unrelated_stdlib_import_break_during_collection_blocks_environment():
    """An old dependency incompatible with the host Python is not patch failure."""
    command = "python3 -m pytest tests/informal/test_leaked_connections.py -q"
    vm = make_vm(staged_plan(
        static=("python -m py_compile requests/models.py",),
        targeted=(command,),
    ))
    vm.mark_write("edit_file", {"path": "requests/models.py"})
    _bash_pass(vm, "python -m py_compile requests/models.py")

    _bash_fail(
        vm,
        command,
        (
            "ERROR collecting tests/informal/test_leaked_connections.py\n"
            "requests/packages/urllib3/_collections.py:7: in <module>\n"
            "    from collections import MutableMapping\n"
            "E   ImportError: cannot import name 'MutableMapping' from "
            "'collections' (/usr/lib/python3.13/collections/__init__.py)\n"
        ),
        exit_code=2,
    )

    status = vm.status()
    assert status["verification_state"] == "blocked_environment"
    assert status["last_verification"]["status"] == "blocked_environment"
    assert status["environment_blocker"]["command"] == command


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


def test_user_acceptance_contract_settles_inferred_required_stages():
    """A passed explicit contract outranks planner-only pending commands."""
    vm = make_vm(staged_plan(
        static=("python -m py_compile pkg/module.py",),
        regression=("pytest",),
    ))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    command = "python -m pytest -q tests"

    _bash_pass(vm, command)
    assert vm.should_gate()

    vm.observe_acceptance_contract(command, "3 passed", passed=True)

    status = vm.status()
    assert status["verification_needed"] is False
    assert status["verification_state"] == "passed"
    assert status["verification_pipeline"]["next_required_stage"] is None
    static = status["verification_pipeline"]["stages"][0]
    assert static["commands"][0]["satisfied_by"] == "user_acceptance_contract"


def test_strict_acceptance_contract_does_not_replace_static_evidence():
    """A targeted user contract cannot aggregate-pass pending strict static checks."""
    command = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            targeted=(command,),
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    vm.observe_bash({"command": command}, "1 passed", False, False)
    vm.observe_acceptance_contract(command, "1 passed", passed=True)

    assert vm.should_gate()
    static = vm.status()["verification_pipeline"]["stages"][0]
    assert static["status"] == "pending"

    _bash_pass(vm, "python -m py_compile pkg/module.py")
    assert not vm.should_gate()


@pytest.mark.parametrize(
    ("command", "output"),
    [
        (
            "pytest --collect-only tests/test_module.py::test_fix",
            "1 test collected in 0.01s",
        ),
        (
            "pytest tests/test_module.py::test_fix",
            "collected 0 items\n\nno tests ran in 0.01s",
        ),
    ],
)
def test_strict_empty_acceptance_contract_cannot_clear_targeted_gate(
    command,
    output,
):
    """Non-executing or empty acceptance commands are not behavior evidence."""
    target = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            targeted=(target,),
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    vm.observe_bash({"command": command}, output, False, False)
    vm.observe_acceptance_contract(command, output, passed=True)

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "pending"


@pytest.mark.parametrize(
    "output",
    [
        "AssertionError: expected 'command not found' in diagnostic",
        "AssertionError: expected 'missing dependency' diagnostic",
        "AssertionError: expected 'could not find module' diagnostic",
        "AssertionError: expected 'no module named pytest' diagnostic",
        "AssertionError: expected 'error importing pytest plugin' diagnostic",
        (
            "AssertionError: expected \"ModuleNotFoundError: "
            "No module named 'missing_pkg'\""
        ),
        (
            "AssertionError: expected \"ERROR: while parsing the following "
            "warning configuration: AttributeError: module 'pytest' has no "
            "attribute 'OldWarning'\""
        ),
        (
            "AssertionError: expected \"ERROR collecting tests/test_old.py "
            "ImportError: cannot import name 'MutableMapping' from "
            "'collections'\""
        ),
    ],
)
def test_assertion_text_environment_phrase_is_repairable_failure(output):
    """Test assertion prose must not masquerade as missing infrastructure."""
    command = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(targeted=(command,)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_fail(
        vm,
        command,
        output,
    )

    status = vm.status()
    assert status["verification_state"] == "failed_repairable"
    assert status["environment_blocker"] is None


@pytest.mark.parametrize(
    "output",
    [
        "Command exited with code 127\npytest: command not found",
        "ModuleNotFoundError: No module named 'missing_pkg'",
        (
            "ERROR: while parsing the following warning configuration:\n"
            "AttributeError: module 'pytest' has no attribute 'OldWarning'"
        ),
        (
            "ERROR collecting tests/test_old.py\n"
            "E   ImportError: cannot import name 'MutableMapping' from 'collections'"
        ),
        "ERROR: Error importing plugin 'pytest_cov': No module named 'pytest_cov'",
    ],
)
def test_failed_test_exit_one_cannot_claim_environment_blocker(output):
    """Child output cannot override the trusted failed-test exit category."""
    command = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(targeted=(command,)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_fail(vm, command, output)

    status = vm.status()
    assert status["verification_state"] == "failed_repairable"
    assert status["environment_blocker"] is None


@pytest.mark.parametrize(
    ("output", "exit_code"),
    [
        ("/usr/bin/python: No module named pytest", 1),
        (
            "ERROR: Error importing plugin 'pytest_cov': No module named 'pytest_cov'",
            4,
        ),
    ],
)
def test_structured_test_infrastructure_failure_is_environment_blocker(
    output,
    exit_code,
):
    """Structured runner startup failures remain unavailable infrastructure."""
    command = "pytest tests/test_module.py::test_fix"
    vm = make_vm(staged_plan(targeted=(command,)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_fail(vm, command, output, exit_code=exit_code)

    assert vm.status()["verification_state"] == "blocked_environment"


def test_command_not_found_exit_127_is_environment_blocker():
    """A real shell missing-command exit remains an infrastructure blocker."""
    command = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(targeted=(command,)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_fail(
        vm,
        command,
        "Command exited with code 127\npytest: command not found",
        exit_code=127,
    )

    status = vm.status()
    assert status["verification_state"] == "blocked_environment"
    assert status["environment_blocker"]["command"] == command


def test_mismatched_exit_metadata_cannot_claim_environment_blocker():
    """Metadata/output disagreement is untrusted and remains repairable."""
    command = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(targeted=(command,)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    vm.observe_bash(
        {"command": command},
        "Command exited with code 1\n1 failed",
        False,
        True,
        exit_code=127,
    )

    status = vm.status()
    assert status["verification_state"] == "failed_repairable"
    assert status["environment_blocker"] is None


def test_static_environment_blocker_records_static_stage():
    """Stop-hook policy needs the blocker stage to reject static-only evidence."""
    command = "ruff check pkg/module.py"
    vm = make_vm(
        staged_plan(static=(command,)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_fail(
        vm,
        command,
        "Command exited with code 127\nruff: command not found",
        exit_code=127,
    )

    blocker = vm.status()["environment_blocker"]
    assert blocker["stage"] == "static"


def test_short_circuit_static_blocker_is_not_promoted_to_targeted():
    """A failed static prefix cannot claim an unexecuted targeted stage."""
    static = "ruff check pkg/module.py"
    targeted = "pytest tests/test_module.py::test_fix"
    vm = make_vm(
        staged_plan(static=(static,), targeted=(targeted,)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    _bash_fail(
        vm,
        f"{static} && {targeted}",
        "ruff: command not found",
        exit_code=127,
    )

    status = vm.status()
    assert status["verification_state"] == "blocked_environment"
    assert status["environment_blocker"]["stage"] == "static"


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


def test_native_runner_serial_retry_replaces_same_target_failure():
    """Changing Django runner parallelism must not create a second test target."""
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        "PYTHONPATH=. python3 tests/runtests.py auth_tests.test_migrations -v1",
        "RuntimeWarning: TestResult has no addDuration method",
    )
    assert vm.should_gate()

    vm.observe_bash(
        {
            "command": (
                "PYTHONPATH=. python3 tests/runtests.py "
                "auth_tests.test_migrations --parallel 1 -v1"
            ),
        },
        "Ran 8 tests in 0.120s\n\nOK",
        False,
        False,
    )

    assert not vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "passed"
    assert [item["status"] for item in targeted["observed"]] == ["passed"]


def test_django_native_runner_replaces_equivalent_pytest_path_failure():
    """A native Django selector supersedes the same pytest file scope."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
            native_runner_kind="django",
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        "python3 -m pytest tests/auth_tests/test_migrations.py -q",
        "ERROR collecting tests/auth_tests/test_migrations.py",
    )
    assert vm.should_gate()

    vm.observe_bash(
        {
            "command": (
                "PYTHONPATH=. python3 tests/runtests.py "
                "auth_tests.test_migrations -v1 --parallel 1"
            ),
        },
        "Ran 8 tests in 0.120s\n\nOK",
        False,
        False,
    )

    assert not vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "passed"
    assert [item["status"] for item in targeted["observed"]] == ["passed"]
    assert targeted["observed"][0]["last_command"].endswith(
        "auth_tests.test_migrations -v1 --parallel 1"
    )


def test_django_native_runner_keeps_different_pytest_scope_failure():
    """A passing native selector cannot hide a different pytest target failure."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
            native_runner_kind="django",
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        "python3 -m pytest tests/auth_tests/test_migrations.py -q",
        "ERROR collecting tests/auth_tests/test_migrations.py",
    )
    vm.observe_bash(
        {
            "command": (
                "PYTHONPATH=. python3 tests/runtests.py "
                "auth_tests.test_models -v1 --parallel 1"
            ),
        },
        "Ran 4 tests in 0.080s\n\nOK",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "failed"
    assert [item["status"] for item in targeted["observed"]] == [
        "failed",
        "passed",
    ]


def test_django_native_runner_does_not_alias_pytest_option_value():
    """A pytest option value ending in .py is not a behavioral test scope."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
            native_runner_kind="django",
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        (
            "python3 -m pytest -c "
            "tests/auth_tests/test_migrations.py -q"
        ),
        "ERROR: usage error while loading pytest configuration",
    )
    vm.observe_bash(
        {
            "command": (
                "PYTHONPATH=. python3 tests/runtests.py "
                "auth_tests.test_migrations -v1 --parallel 1"
            ),
        },
        "Ran 8 tests in 0.120s\n\nOK",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "failed"
    assert [item["status"] for item in targeted["observed"]] == [
        "failed",
        "passed",
    ]


def test_django_native_runner_does_not_drop_unmapped_pytest_scope():
    """Every pytest target must be represented before native coverage applies."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
            native_runner_kind="django",
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        (
            "python3 -m pytest tests/auth_tests/test_migrations.py "
            "integration/test_other.py -q"
        ),
        "ERROR collecting integration/test_other.py",
    )
    vm.observe_bash(
        {
            "command": (
                "PYTHONPATH=. python3 tests/runtests.py "
                "auth_tests.test_migrations -v1 --parallel 1"
            ),
        },
        "Ran 8 tests in 0.120s\n\nOK",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "failed"
    assert [item["status"] for item in targeted["observed"]] == [
        "failed",
        "passed",
    ]


def test_django_native_runner_filtered_success_does_not_cover_full_file():
    """A scope-limiting native runner option cannot settle a full-file failure."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
            native_runner_kind="django",
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        "python3 -m pytest tests/auth_tests/test_migrations.py -q",
        "ERROR collecting tests/auth_tests/test_migrations.py",
    )
    vm.observe_bash(
        {
            "command": (
                "PYTHONPATH=. python3 tests/runtests.py "
                "auth_tests.test_migrations --tag smoke"
            ),
        },
        "Ran 2 tests in 0.040s\n\nOK",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "failed"


def test_untrusted_native_runner_does_not_alias_pytest_path():
    """A generic project runtests.py is not proof of Django selector identity."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        "python3 -m pytest tests/auth_tests/test_migrations.py -q",
        "ERROR collecting tests/auth_tests/test_migrations.py",
    )
    vm.observe_bash(
        {
            "command": (
                "python3 tests/runtests.py "
                "auth_tests.test_migrations -v1 --parallel 1"
            ),
        },
        "Ran 8 tests in 0.120s\n\nOK",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "failed"


def test_django_alias_rejects_different_runtests_path():
    """Only the exact workspace runner identified by the plan may supersede."""
    vm = make_vm(
        staged_plan(
            static=("python -m py_compile pkg/module.py",),
            optional_targeted=(
                "python tests/runtests.py auth_tests.test_migrations",
            ),
            regression=("python tests/runtests.py",),
            native_runner_kind="django",
        ),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    _bash_fail(
        vm,
        "python3 -m pytest tests/auth_tests/test_migrations.py -q",
        "ERROR collecting tests/auth_tests/test_migrations.py",
    )
    vm.observe_bash(
        {
            "command": (
                "python3 vendor/tests/runtests.py "
                "auth_tests.test_migrations -v1 --parallel 1"
            ),
        },
        "Ran 8 tests in 0.120s\n\nOK",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "failed"


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
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["commands"][0]["automation_provenance"] == "failure_evidence"

    _bash_pass(vm, "pytest tests/test_module.py::test_fix")
    assert not vm.should_gate()


def test_model_executed_target_is_automation_safe_after_followup_write():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    target = "pytest tests/test_module.py::test_fix"
    vm = make_vm(staged_plan(targeted=(target,)))
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, target)

    vm.mark_write("write_file", {"path": "pkg/module.py"})
    status = vm.status()
    action = VerificationScheduler().action(
        "orange",
        verification_status=status,
        unresolved_requirements=("R1",),
        has_exact_contract=True,
    )

    targeted = next(
        stage
        for stage in status["verification_pipeline"]["stages"]
        if stage["name"] == "targeted"
    )
    assert targeted["commands"][0]["automation_provenance"] == "model_execution"
    assert action.command == target


def test_many_failures_keep_only_one_bounded_target_per_test_file():
    """One widespread regression must not inflate context with dozens of nodes."""
    vm = make_vm(staged_plan(static=("python -m py_compile pkg/module.py",)))
    failures = "\n".join(
        [
            *(f"FAILED tests/test_parser.py::test_case_{i} - AssertionError" for i in range(8)),
            *(f"FAILED tests/test_scheduler.py::test_case_{i} - AssertionError" for i in range(8)),
            *(f"FAILED tests/test_cli.py::test_case_{i} - AssertionError" for i in range(8)),
            "FAILED tests/test_extra.py::test_case - AssertionError",
        ]
    )

    _bash_fail(vm, "pytest tests", failures)
    vm.mark_write("write_file", {"path": "pkg/module.py"})

    targeted = vm.status()["verification_pipeline"]["stages"][1]
    required = [item["command"] for item in targeted["commands"] if item["required"]]
    assert len(required) == 3
    assert required == [
        "pytest tests/test_parser.py::test_case_0",
        "pytest tests/test_scheduler.py::test_case_0",
        "pytest tests/test_cli.py::test_case_0",
    ]


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


def test_strict_manager_keeps_inferred_related_target_advisory_while_gating(tmp_path):
    """Strict mode needs behavior evidence without promoting a planner guess."""
    from nz_coder.runtime.process.workdir import scoped_workdir

    source = tmp_path / "lib" / "package" / "widget.py"
    test = tmp_path / "lib" / "package" / "tests" / "test_widget.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("def widget(): return 1\n", encoding="utf-8")
    test.write_text("def test_widget(): assert True\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    recovery = MagicMock()
    recovery.verification_gate_message.return_value = "Please verify your changes."

    with scoped_workdir(tmp_path):
        vm = VerificationManager(
            recovery,
            MagicMock(),
            require_targeted=True,
        )
        vm.mark_write("edit_file", {"path": "lib/package/widget.py"})
        vm.observe_verify_changed_files("OK: py_compile changed files")
        pipeline = vm.status()["verification_pipeline"]

    targeted = next(stage for stage in pipeline["stages"] if stage["name"] == "targeted")
    assert vm.should_gate() is True
    assert targeted["required"] is True
    assert targeted["evidence_required"] is True
    assert targeted["status"] == "pending"
    assert pipeline["next_required_stage"] == "targeted"
    assert targeted["commands"][0]["command"] == (
        "pytest lib/package/tests/test_widget.py"
    )
    assert targeted["commands"][0]["required"] is False


def test_strict_model_selected_target_pass_clears_gate():
    """One real model-selected targeted test satisfies strict evidence."""
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    vm.observe_bash(
        {"command": "pytest tests/test_module.py::test_fix"},
        "1 passed in 0.01s",
        False,
        False,
    )

    assert not vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "passed"


def test_strict_zero_test_target_does_not_clear_gate():
    """A zero-exit targeted command that ran no tests is not behavior evidence."""
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    vm.observe_bash(
        {"command": "pytest tests/test_module.py::test_missing"},
        "collected 0 items\n\nno tests ran in 0.01s",
        False,
        False,
    )

    assert vm.should_gate()
    targeted = vm.status()["verification_pipeline"]["stages"][1]
    assert targeted["status"] == "pending"
    assert targeted["observed"][0]["status"] == "skipped"


def test_strict_gate_message_requests_non_empty_targeted_behavior_check():
    """Strict guidance must ask for a real behavior test, not another static check."""
    vm = make_vm(
        staged_plan(static=("python -m py_compile pkg/module.py",)),
        require_targeted=True,
    )
    vm.mark_write("write_file", {"path": "pkg/module.py"})
    _bash_pass(vm, "python -m py_compile pkg/module.py")

    message = vm.make_gate_message()

    assert "direct narrow behavioral test" in message
    assert "at least one test" in message


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
