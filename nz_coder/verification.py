"""Verification state manager for the agent loop.

追踪文件编辑后是否通过了验证命令（测试/编译检查），
并在模型尝试结束时驱动 verification gate。

职责:
  - 记录哪些工具写了文件 (mark_write)
  - 解析 bash / python_symbol_check 结果 (observe_bash / observe_symbol_check)
  - 判断是否需要继续验证 (should_gate)
  - 生成 gate 提示消息 (make_gate_message)
"""
from __future__ import annotations

import os.path as _osp
from typing import TYPE_CHECKING

from nz_coder.recovery import _extract_failure_excerpt

if TYPE_CHECKING:
    from nz_coder.recovery import RecoveryState
    from nz_coder.trace import TraceRecorder


class VerificationManager:
    """Tracks whether edits need to be validated before the agent can finish.

    典型生命周期:
        reset()             — 每次 run() 开始时调用
        mark_write()        — 写工具成功执行后调用
        observe_bash()      — bash 工具结果出来后调用
        observe_symbol_check() — python_symbol_check 结果出来后调用
        should_gate()       — True 表示模型不应结束
        increment_gate_prompt() / gate_prompts — 跟踪已发送的 gate 提示数
        make_gate_message() — 生成注入到对话的 gate 提示
        status()            — 返回用于 run() 状态报告的快照
    """

    def __init__(self, recovery: RecoveryState, tracer: TraceRecorder):
        self._recovery = recovery
        self._tracer = tracer
        self._needed: bool = False
        self._last: dict | None = None
        self._gate_prompts: int = 0

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """每次 run() 开始时重置状态。"""
        self._needed = False
        self._last = None
        self._gate_prompts = 0

    # ── 写入通知 ─────────────────────────────────────────────────────────────

    def mark_write(self, tool_name: str, tool_input: dict) -> None:
        """写工具成功执行后调用。

        scratch 文件（根目录 test_*.py、*.md、*.txt 等）不重置 gate，
        因为它们不是被修复的包代码。
        """
        if self._is_scratch_file_write(tool_name, tool_input):
            return
        self._needed = True
        self._gate_prompts = 0
        self._tracer.log("verification_needed", reason=f"{tool_name} changed files")

    # ── 结果观察 ─────────────────────────────────────────────────────────────

    def observe_bash(
        self,
        tool_input: dict,
        output: str,
        dispatch_failed: bool,
        command_failed: bool,
    ) -> None:
        """根据 bash 工具结果更新验证状态。"""
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if not self._is_verification_command(command):
            return

        status = "failed" if (command_failed or dispatch_failed) else "passed"
        if status == "passed" and self._looks_like_failed_output(output):
            status = "failed"

        # 纯环境问题（缺少外部包、agent 脚本编码错误等）不应覆盖
        # 之前通过的验证（如 py_compile）。
        if status == "failed" and self._is_env_import_error(output):
            self._tracer.log("verification_env_error_skipped", command=command)
            return

        self._last = {"command": command, "status": status, "output": _extract_failure_excerpt(output)}
        if status == "passed":
            self._needed = False
        else:
            self._needed = True
            self._gate_prompts = 0
        self._tracer.log("verification_result", **self._last)

    def observe_symbol_check(self, output: str) -> None:
        """根据 python_symbol_check 结果更新验证状态。"""
        status = "passed" if output.startswith("OK:") else ("skipped" if output.startswith("WARN:") else "failed")
        self._last = {
            "command": "python_symbol_check",
            "status": status,
            "output": output[-4000:],
        }
        self._needed = status not in {"passed", "skipped"}
        self._tracer.log("verification_result", **self._last)

    def observe_verify_changed_files(self, output: str) -> None:
        """根据 verify_changed_files 结果更新验证状态。

        OK 表示已通过可执行检查；WARN 表示没有可用的语言检查器或只做了
        部分检查，允许结束但把状态记录为 skipped，避免通用项目卡死在 gate。
        """
        status = "passed" if output.startswith("OK:") else ("skipped" if output.startswith("WARN:") else "failed")
        self._last = {
            "command": "verify_changed_files",
            "status": status,
            "output": output[-4000:],
        }
        self._needed = status not in {"passed", "skipped"}
        if status == "failed":
            self._gate_prompts = 0
        self._tracer.log("verification_result", **self._last)

    # ── Gate 控制 ────────────────────────────────────────────────────────────

    def should_gate(self) -> bool:
        """True 表示模型尝试结束但验证尚未通过。"""
        return self._needed

    def increment_gate_prompt(self) -> int:
        """递增并返回已发送的 gate 提示次数。"""
        self._gate_prompts += 1
        return self._gate_prompts

    @property
    def gate_prompts(self) -> int:
        return self._gate_prompts

    def make_gate_message(self) -> str:
        """返回注入到对话的 verification gate 提示。"""
        message = self._recovery.verification_gate_message(self._last)
        try:
            from nz_coder.verification_planner import plan_verification
            plan = plan_verification(include_broad=False)
        except Exception:
            plan = ""
        if plan and not plan.startswith("Error:"):
            message += "\n\n<verification-plan>\n" + plan + "\n</verification-plan>"
        return message

    def status(self) -> dict:
        """返回适合放入 run() 返回值的快照。"""
        return {
            "verification_needed": self._needed,
            "last_verification": self._last,
        }

    # ── 内部工具方法 ──────────────────────────────────────────────────────────

    def _is_verification_command(self, command: str) -> bool:
        """启发式判断：该命令是否用于验证编辑后的行为？"""
        cmd = " ".join((command or "").strip().lower().split())
        if not cmd:
            return False
        verification_markers = (
            "pytest",
            "unittest",
            "tox",
            "nox",
            "manage.py test",
            "npm test",
            "npm run test",
            "npm run typecheck",
            "pnpm run typecheck",
            "yarn typecheck",
            "pnpm test",
            "yarn test",
            "cargo test",
            "cargo check",
            "go test",
            "go vet",
            "mvn test",
            "gradle test",
            "./gradlew test",
            "make test",
        )
        if any(marker in cmd for marker in verification_markers):
            return True
        if "python" in cmd and ("py_compile" in cmd or "compileall" in cmd):
            return True
        if "tsc" in cmd and "noemit" in cmd.replace("-", ""):
            return True
        if "python" in cmd and " -c " in cmd:
            return (
                "assert" in cmd
                or "import " in cmd
                or "print('ok')" in cmd
                or 'print("ok")' in cmd
            )
        return False

    def _looks_like_failed_output(self, output: str) -> bool:
        """True 表示命令退出码为 0，但输出明显包含失败信号。"""
        if "Traceback (most recent call last)" in output:
            return True
        lowered = output.lower()
        if "no module named" in lowered or "setup failed" in lowered:
            return True
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(("FAILED ", "FAIL:", "AssertionError")):
                return True
            if line == "FAIL" or line.startswith("FAIL "):
                return True
            if line.startswith("Test ") and " FAIL" in line:
                return True
        return False

    def _is_env_import_error(self, output: str) -> bool:
        """True 表示验证失败属于环境/依赖问题，而非代码本身的缺陷。

        保守策略：只识别明确的外部包缺失和 agent 脚本编码错误。
        不把项目内部 import 失败（can't import name 'foo' from 'mymodule'）
        也视为环境问题，以免掩盖代码缺陷。
        """
        env_indicators = (
            "ModuleNotFoundError: No module named",
            "ImportError: No module named",
            "No module named",
        )
        if any(ind in output for ind in env_indicators):
            return True
        # agent 在 python3 -c "..." 中使用了字面 \n，导致 shell 语法错误
        if 'File "<string>", line 1' in output and "SyntaxError" in output:
            return True
        # pytest exit code 4：配置/警告解析错误，不是代码质量问题
        if "ERROR: while parsing the following warning configuration" in output:
            return True
        if output.strip().startswith("ERROR: ") and "pytest" in output:
            if any(k in output for k in ("no tests ran", "error during collection", "INTERNALERROR")):
                return True
        return False

    def _is_scratch_file_write(self, fn_name: str, tool_input) -> bool:
        """True 表示写的是根目录临时/文档文件，不应重置 verification gate。

        仅对 write_file 工具生效；edit_file / apply_patch 等始终视为实质修改。
        """
        if fn_name != "write_file":
            return False
        if not isinstance(tool_input, dict):
            return False
        path = tool_input.get("path", "") or ""
        fname = _osp.basename(path)
        # 只有根目录文件（无子目录层级）才视为 scratch
        if _osp.dirname(path):
            return False
        lower = fname.lower()
        # 根目录测试占位文件
        if lower.startswith("test_") or lower.endswith("_test.py"):
            return True
        # 根目录文档文件（agent 偶尔会写 CHANGES_SUMMARY.md 等说明）
        if lower.endswith((".md", ".txt", ".rst")):
            return True
        return False
