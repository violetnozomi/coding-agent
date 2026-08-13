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

import copy
import os.path as _osp
import re
import shlex
from collections import Counter
from typing import TYPE_CHECKING, Callable

from nz_coder.recovery import _extract_failure_excerpt
from nz_coder.verification_evidence import (
    VerificationState,
    is_environment_verification_failure,
)

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

    def __init__(
        self,
        recovery: RecoveryState,
        tracer: TraceRecorder,
        plan_builder: Callable[[list[str]], dict] | None = None,
    ):
        self._recovery = recovery
        self._tracer = tracer
        self._plan_builder = plan_builder
        self._needed: bool = False
        self._last: dict | None = None
        self._gate_prompts: int = 0
        self._has_write: bool = False
        self._changed_files: list[str] = []
        self._failed_tests: list[str] = []
        self._required_target_commands: list[str] = []
        self._plan: dict | None = None
        self._pipeline: dict[str, dict] = {}
        self._state = VerificationState.UNVERIFIED
        self._environment_blocker: dict | None = None

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        """每次 run() 开始时重置状态。"""
        self._needed = False
        self._last = None
        self._gate_prompts = 0
        self._has_write = False
        self._changed_files = []
        self._failed_tests = []
        self._required_target_commands = []
        self._plan = None
        self._pipeline = {}
        self._state = VerificationState.UNVERIFIED
        self._environment_blocker = None

    # ── 写入通知 ─────────────────────────────────────────────────────────────

    def mark_write(self, tool_name: str, tool_input: dict) -> None:
        """写工具成功执行后调用。

        scratch 文件（根目录 test_*.py、*.md、*.txt 等）不重置 gate，
        因为它们不是被修复的包代码。
        """
        if self._is_scratch_file_write(tool_name, tool_input):
            return
        if isinstance(tool_input, dict) and bool(tool_input.get("dry_run")):
            return
        for path in self._extract_changed_files(tool_input):
            if path not in self._changed_files:
                self._changed_files.append(path)
        self._needed = True
        self._gate_prompts = 0
        self._has_write = True
        self._plan = None
        self._pipeline = {}
        self._state = VerificationState.UNVERIFIED
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
        if dispatch_failed:
            return
        self._ensure_pipeline()
        from nz_coder.verification_planner import (
            classify_verification_segments,
            is_python_probe_command,
            verification_output_failed,
            verification_success_is_reliable,
        )
        segments = classify_verification_segments(command, self._plan)
        if not segments:
            # A free-form Python probe is deliberately not allowed to satisfy
            # verification merely by exiting zero.  It can still provide
            # trustworthy negative evidence when its output contains an
            # explicit failure marker (for example ``No module named``).
            if not (
                (command_failed or verification_output_failed(output))
                and is_python_probe_command(command)
            ):
                return
            segments = [("static", command)]

        status = "failed" if command_failed else "passed"
        if status == "passed" and verification_output_failed(output):
            status = "failed"
        if status == "failed" and is_environment_verification_failure(command, output):
            status = "blocked_environment"
            self._environment_blocker = {
                "command": command,
                "output": _extract_failure_excerpt(output),
            }
        elif status == "passed" and self._environment_blocker is not None:
            if self._commands_equivalent(self._environment_blocker.get("command", ""), command):
                self._environment_blocker = None
        if status == "passed" and not verification_success_is_reliable(command):
            self._tracer.log(
                "verification_result_ignored",
                command=command,
                reason="shell operator can hide a failed verification segment",
            )
            return

        ranks = {"static": 1, "targeted": 2, "regression": 3}
        stage = max((item[0] for item in segments), key=ranks.__getitem__)
        for segment_stage, segment_command in segments:
            if status == "failed":
                self._remember_failed_targets(segment_stage, segment_command, output)
            self._record_verification_result(segment_stage, status, segment_command)
        self._last = {
            "command": command,
            "stage": stage,
            "status": status,
            "output": _extract_failure_excerpt(output),
        }
        self._tracer.log("verification_result", **self._last)

    def observe_symbol_check(self, output: str, tool_input: dict | None = None) -> None:
        """根据 python_symbol_check 结果更新验证状态。"""
        self._ensure_pipeline()
        status = "passed" if output.startswith("OK:") else ("skipped" if output.startswith("WARN:") else "failed")
        path = str((tool_input or {}).get("path") or "").strip()
        command = "python_symbol_check" + (f" {shlex.quote(path)}" if path else "")
        self._last = {
            "command": command,
            "stage": "static",
            "status": status,
            "output": output[-4000:],
        }
        self._record_verification_result(
            "static",
            status,
            command,
            aggregate=True,
            aggregate_paths=[path] if path else None,
        )
        self._tracer.log("verification_result", **self._last)

    def observe_verify_changed_files(self, output: str) -> None:
        """根据 verify_changed_files 结果更新验证状态。

        OK 表示已通过可执行检查；WARN 表示没有可用的语言检查器或只做了
        部分检查，允许结束但把状态记录为 skipped，避免通用项目卡死在 gate。
        """
        self._ensure_pipeline()
        status = "passed" if output.startswith("OK:") else ("skipped" if output.startswith("WARN:") else "failed")
        self._last = {
            "command": "verify_changed_files",
            "stage": "static",
            "status": status,
            "output": output[-4000:],
        }
        self._record_verification_result("static", status, "verify_changed_files", aggregate=True)
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
        self._ensure_pipeline()
        message = self._recovery.verification_gate_message(self._last)
        pipeline = self._format_pipeline_status()
        if pipeline:
            message += "\n\n<verification-pipeline>\n" + pipeline + "\n</verification-pipeline>"
        return message

    def status(self) -> dict:
        """返回适合放入 run() 返回值的快照。"""
        self._ensure_pipeline()
        return {
            "verification_needed": self._needed,
            "verification_state": self._state.value,
            "last_verification": self._last,
            "environment_blocker": dict(self._environment_blocker) if self._environment_blocker else None,
            "verification_pipeline": self._pipeline_snapshot(),
        }

    # ── 内部工具方法 ──────────────────────────────────────────────────────────

    def _is_verification_command(self, command: str) -> bool:
        """启发式判断：该命令是否用于验证编辑后的行为？"""
        return self._verification_stage(command) is not None

    def _verification_stage(self, command: str) -> str | None:
        from nz_coder.verification_planner import classify_verification_command
        return classify_verification_command(command, self._plan)

    def _ensure_pipeline(self) -> None:
        """Build a staged verification plan lazily after a material write."""
        if self._plan is not None or not self._has_write:
            return
        try:
            if self._plan_builder is not None:
                plan = self._plan_builder(list(self._changed_files))
            else:
                from nz_coder.verification_planner import plan_verification_commands
                changed_files = self._plannable_changed_files()
                plan = plan_verification_commands(
                    changed_files=changed_files,
                    failing_tests=list(self._failed_tests),
                    include_broad=False,
                )
            self._plan = copy.deepcopy(plan) if isinstance(plan, dict) else {}
        except Exception as exc:
            self._plan = {}
            self._tracer.log("verification_plan_failed", error=str(exc))

        self._add_required_target_commands()
        self._pipeline = {}
        for stage in self._plan.get("stages", []):
            if not isinstance(stage, dict):
                continue
            name = str(stage.get("name") or "")
            if name not in {"static", "targeted", "regression"}:
                continue
            stage_required = bool(stage.get("required"))
            commands = []
            for item in stage.get("commands", []):
                if not isinstance(item, dict) or not str(item.get("command") or "").strip():
                    continue
                command = dict(item)
                command["required"] = bool(command.get("required", stage_required))
                command["status"] = "pending" if command["required"] else "not_run"
                commands.append(command)
            self._pipeline[name] = {
                "name": name,
                "commands": commands,
                "observed": [],
            }
        self._tracer.log(
            "verification_pipeline_planned",
            changed_files=list(self._changed_files),
            stages=self._pipeline_snapshot()["stages"],
        )

    def _plannable_changed_files(self) -> list[str] | None:
        """Exclude known deleted paths without falling back to the git-wide scan."""
        if not self._changed_files:
            return None
        try:
            from nz_coder.runtime.workdir import current_workdir
            from nz_coder.tools.files import _safe_path

            root = current_workdir().resolve()
            existing: list[str] = []
            for path in self._changed_files:
                safe = _safe_path(path)
                if safe.exists():
                    existing.append(safe.relative_to(root).as_posix())
            return existing
        except (OSError, TypeError, ValueError):
            return []

    def _add_required_target_commands(self) -> None:
        """Merge previously failing targeted commands into the current plan."""
        if not self._required_target_commands or self._plan is None:
            return
        stages = self._plan.setdefault("stages", [])
        targeted = next(
            (item for item in stages if isinstance(item, dict) and item.get("name") == "targeted"),
            None,
        )
        if targeted is None:
            targeted = {"name": "targeted", "required": True, "commands": []}
            stages.append(targeted)
        targeted["required"] = True
        commands = targeted.setdefault("commands", [])
        for command in self._required_target_commands:
            existing = next(
                (
                    item for item in commands
                    if isinstance(item, dict)
                    and self._commands_equivalent(item.get("command", ""), command)
                ),
                None,
            )
            if existing is not None:
                existing["required"] = True
                existing["reason"] = "previously failing targeted verification"
                continue
            commands.append({
                "command": command,
                "reason": "previously failing targeted verification",
                "required": True,
            })

    def _record_verification_result(
        self,
        stage: str,
        status: str,
        command: str,
        *,
        aggregate: bool = False,
        aggregate_paths: list[str] | None = None,
    ) -> None:
        """Update command-level stage state and recompute the completion gate."""
        state = self._pipeline.get(stage)
        if state is None:
            state = {"name": stage, "commands": [], "observed": []}
            self._pipeline[stage] = state

        required_commands = [item for item in state["commands"] if item.get("required")]
        matched = []
        if aggregate and status in {"passed", "failed"}:
            matched = required_commands
            if aggregate_paths:
                matched = [
                    item for item in matched
                    if any(
                        self._command_covers_path(item.get("command", ""), path)
                        for path in aggregate_paths
                    )
                ]
        elif not aggregate:
            matched = [
                item for item in state["commands"]
                if (
                    self._commands_equivalent(item.get("command", ""), command)
                    if item.get("required")
                    else self._same_command(item.get("command", ""), command)
                )
            ]

        for item in matched:
            item["status"] = status
            item["last_command"] = command

        should_record_observed = not matched
        if aggregate and status == "skipped" and required_commands:
            should_record_observed = True
        if should_record_observed:
            self._upsert_observed(state["observed"], command, status)

        failed = any(
            item.get("status") == "failed"
            for pipeline_stage in self._pipeline.values()
            for item in pipeline_stage["commands"] + pipeline_stage["observed"]
        )
        all_required = [
            item
            for pipeline_stage in self._pipeline.values()
            for item in pipeline_stage["commands"]
            if item.get("required")
        ]
        if all_required:
            complete = all(item.get("status") == "passed" for item in all_required)
        else:
            complete = any(
                item.get("status") in {"passed", "skipped"}
                for pipeline_stage in self._pipeline.values()
                for item in pipeline_stage["commands"] + pipeline_stage["observed"]
            )
        blocked_environment = self._environment_blocker is not None or any(
            item.get("status") == "blocked_environment"
            for pipeline_stage in self._pipeline.values()
            for item in pipeline_stage["commands"] + pipeline_stage["observed"]
        )
        degraded = any(
            item.get("status") == "skipped"
            for pipeline_stage in self._pipeline.values()
            for item in pipeline_stage["commands"] + pipeline_stage["observed"]
        )
        self._needed = failed or blocked_environment or not complete
        if failed:
            self._state = VerificationState.FAILED_REPAIRABLE
        elif blocked_environment:
            self._state = VerificationState.BLOCKED_ENVIRONMENT
        elif complete and degraded:
            self._state = VerificationState.DEGRADED
        elif complete:
            self._state = VerificationState.PASSED
        else:
            self._state = VerificationState.VERIFYING
        if status == "failed" or (status == "passed" and matched):
            self._gate_prompts = 0
        self._tracer.log(
            "verification_stage_result",
            stage=stage,
            status=status,
            command=command,
            verification_needed=self._needed,
        )

    def _upsert_observed(self, items: list[dict], command: str, status: str) -> None:
        for item in items:
            if self._commands_equivalent(item.get("command", ""), command):
                item["status"] = status
                item["last_command"] = command
                return
        items.append({
            "command": command,
            "required": False,
            "status": status,
            "last_command": command,
        })

    def _remember_failed_targets(self, stage: str, command: str, output: str) -> None:
        """Carry concrete failing tests across the edit that is meant to fix them."""
        from nz_coder.recovery import _extract_failed_tests
        failed_tests = _extract_failed_tests(output)
        for test in failed_tests:
            if test not in self._failed_tests:
                self._failed_tests.append(test)
            if "pytest" in command.lower():
                targeted = f"pytest {shlex.quote(test)}"
                if targeted not in self._required_target_commands:
                    self._required_target_commands.append(targeted)
        if stage == "targeted" and not failed_tests:
            normalized = " ".join((command or "").split())
            if normalized and normalized not in self._required_target_commands:
                self._required_target_commands.append(normalized)

    def _commands_equivalent(self, planned: str, observed: str) -> bool:
        """Return True when observed covers the same planned command target."""
        from nz_coder.verification_planner import canonical_verification_segments
        planned_segments = canonical_verification_segments(planned, self._plan)
        observed_segments = canonical_verification_segments(observed, self._plan)
        for planned_stage, planned_tokens in planned_segments:
            planned_counts = Counter(planned_tokens)
            for observed_stage, observed_tokens in observed_segments:
                if planned_stage != observed_stage:
                    continue
                observed_counts = Counter(observed_tokens)
                if observed_counts == planned_counts:
                    return True
        return False

    def _same_command(self, first: str, second: str) -> bool:
        from nz_coder.verification_planner import verification_command_key
        return verification_command_key(first, self._plan) == verification_command_key(second, self._plan)

    def _command_covers_path(self, command: str, path: str) -> bool:
        from nz_coder.verification_planner import canonical_verification_segments
        normalized_path = str(path or "").replace("\\", "/").lstrip("./")
        if not normalized_path:
            return False
        return any(
            str(token).replace("\\", "/").lstrip("./") == normalized_path
            for _stage, tokens in canonical_verification_segments(command, self._plan)
            for token in tokens
        )

    def _pipeline_snapshot(self) -> dict:
        stages = []
        for name in ("static", "targeted", "regression"):
            state = self._pipeline.get(name)
            if state is None:
                continue
            commands = [dict(item) for item in state["commands"]]
            observed = [dict(item) for item in state["observed"]]
            combined = commands + observed
            required = [item for item in commands if item.get("required")]
            if any(item.get("status") == "failed" for item in combined):
                status = "failed"
            elif required and all(item.get("status") == "passed" for item in required):
                status = "passed"
            elif required:
                status = "pending"
            elif any(item.get("status") == "passed" for item in combined):
                status = "passed"
            elif any(item.get("status") == "skipped" for item in combined):
                status = "skipped"
            elif commands:
                status = "optional"
            else:
                status = "unavailable"
            stages.append({
                "name": name,
                "required": bool(required),
                "status": status,
                "commands": commands,
                "observed": observed,
            })

        next_stage = next(
            (item["name"] for item in stages if item["status"] in {"failed", "pending"}),
            None,
        )
        return {
            "changed_files": list(self._changed_files),
            "stages": stages,
            "next_required_stage": next_stage,
        }

    def _format_pipeline_status(self) -> str:
        snapshot = self._pipeline_snapshot()
        stages = snapshot["stages"]
        if not stages:
            return ""
        lines = [
            f"- {stage['name']}: {stage['status']} ({'required' if stage['required'] else 'optional'})"
            for stage in stages
        ]
        pending = []
        recommended = []
        failed_optional = []
        for stage in stages:
            for item in stage["commands"]:
                if item.get("required") and item.get("status") != "passed":
                    pending.append(str(item.get("command") or ""))
                elif (
                    not item.get("required")
                    and item.get("status") == "not_run"
                    and "related test from" in str(item.get("reason") or "")
                ):
                    recommended.append(str(item.get("command") or ""))
                elif not item.get("required") and item.get("status") == "failed":
                    failed_optional.append(str(item.get("command") or ""))
            for item in stage["observed"]:
                if item.get("status") == "failed":
                    failed_optional.append(str(item.get("command") or ""))
        if pending:
            lines.append("Required checks still to run:")
            lines.extend(f"  - {command}" for command in pending[:12])
        if recommended:
            lines.append("Recommended high-confidence related checks:")
            lines.extend(f"  - {command}" for command in recommended[:4])
        if failed_optional:
            lines.append("Failed checks that must be rerun after fixing:")
            lines.extend(f"  - {command}" for command in failed_optional[:4])
        if not pending and not failed_optional and self._needed:
            lines.append("No focused required command was inferred; run verify_changed_files.")
        return "\n".join(lines)

    def _extract_changed_files(self, tool_input) -> list[str]:
        """Best-effort path extraction for single, batch, and patch write tools."""
        if not isinstance(tool_input, dict):
            return []
        paths: list[str] = []

        def add(value) -> None:
            if isinstance(value, str) and value.strip() and value.strip() not in paths:
                paths.append(value.strip())

        add(tool_input.get("path"))
        for key in ("files", "changes"):
            values = tool_input.get(key, [])
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict):
                    add(item.get("path"))
                else:
                    add(item)

        patch = tool_input.get("patch") or tool_input.get("patch_text") or ""
        if isinstance(patch, str):
            for match in re.finditer(
                r"^(?:\+\+\+ b/|--- a/|\*\*\* (?:Add|Update|Delete) File: )(.+)$",
                patch,
                re.MULTILINE,
            ):
                path = match.group(1).strip()
                if path != "/dev/null":
                    add(path)
        return paths

    def _looks_like_failed_output(self, output: str) -> bool:
        """True 表示命令退出码为 0，但输出明显包含失败信号。"""
        from nz_coder.verification_planner import verification_output_failed

        return verification_output_failed(output)

    def _is_python_probe(self, command: str) -> bool:
        """Return True for a real ``python -c`` command, excluding printed text."""
        from nz_coder.verification_planner import is_python_probe_command

        return is_python_probe_command(command)

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
