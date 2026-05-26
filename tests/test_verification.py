"""tests/test_verification.py — VerificationManager 单元测试。

覆盖写文件触发验证需求、scratch 文件豁免、bash 验证命令清除/设置状态、
python_symbol_check 结果、环境错误不覆盖已通过的验证等场景。
"""
from unittest.mock import MagicMock

import pytest

from nz_coder.verification import VerificationManager


# ── 测试辅助 ────────────────────────────────────────────────────────────────

def make_vm() -> VerificationManager:
    """创建一个带 mock 依赖的 VerificationManager。"""
    recovery = MagicMock()
    recovery.verification_gate_message.return_value = "Please verify your changes."
    tracer = MagicMock()
    vm = VerificationManager(recovery, tracer)
    vm.reset()
    return vm


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


def test_module_not_found_no_override():
    """ModuleNotFoundError 导致的 pytest 失败不应覆盖已通过的 py_compile。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    _bash_pass(vm, "python3 -m py_compile nz_coder/loop.py")
    assert not vm.should_gate()

    env_error_output = (
        "Command exited with code 1\n"
        "ModuleNotFoundError: No module named 'requests'"
    )
    _bash_fail(vm, "pytest tests/", env_error_output)
    # 环境问题不应覆盖已通过的验证
    assert not vm.should_gate()


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


def test_no_module_named_output_with_zero_exit_does_not_clear_gate():
    """输出 No module named 但退出 0 时，不应把验证标为通过。"""
    vm = make_vm()
    vm.mark_write("write_file", {"path": "nz_coder/loop.py"})
    vm.observe_bash(
        {"command": "python3 -c \"import django; print('Django setup failed: No module named cgi')\""},
        "Django setup failed: No module named 'cgi'",
        False,
        False,
    )
    assert vm.should_gate()
    assert vm.status()["last_verification"] is None


def test_no_module_named_output_with_zero_exit_no_override():
    """输出 No module named 但退出 0 时，不应覆盖之前通过的验证。"""
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
    assert not vm.should_gate()
    assert vm.status()["last_verification"]["command"] == "python3 -m py_compile nz_coder/loop.py"


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
    vm = make_vm()
    s = vm.status()
    assert s["verification_needed"] is False
    assert s["last_verification"] is None

    vm.mark_write("write_file", {"path": "pkg/module.py"})
    s = vm.status()
    assert s["verification_needed"] is True

    _bash_pass(vm, "pytest tests/")
    s = vm.status()
    assert s["verification_needed"] is False
    assert s["last_verification"] is not None
    assert s["last_verification"]["status"] == "passed"
