"""RuntimeState — Claude Code 风格的 state-as-message 机制。

在每个 agent loop 迭代中跟踪客观事实状态（turn、diff、验证、空转），
并在每轮 LLM 调用前注入 <system-reminder> 状态块，让模型基于当前状态
做出更合理的探索/收敛决策。

与 scratchpad 的区分：
  - scratchpad = agent 主观推理笔记（agent 自己写）
  - RuntimeState = 系统自动跟踪的客观事实（工具调用后自动更新）
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from nz_coder.task_policy import (
    detect_task_mode,
    is_broad_test_command,
    is_exact_test_command,
    task_wants_tests,
)


STRICT_INVESTIGATION_SOFT_LIMIT = 12
STRICT_INVESTIGATION_HARD_LIMIT = 20
_INVESTIGATION_TOOLS = frozenset({
    "grep_search",
    "glob_search",
    "read_file",
    "read_symbol",
    "list_directory",
    "repo_map",
    "code_references",
    "find_symbol_callers",
    "analyze_impact",
})
_BASH_INVESTIGATION_COMMANDS = frozenset({
    "cat", "grep", "head", "rg", "sed", "tail", "tree", "find",
})


# ── Broad test runner 检测 — 识别"跑全套测试"的行为 ──────────────────────────

def _is_broad_test_command(command: str) -> bool:
    """向后兼容包装：实际规则在 task_policy 中维护。"""
    return is_broad_test_command(command)


def _is_exact_test(command: str) -> bool:
    """向后兼容包装：实际规则在 task_policy 中维护。"""
    return is_exact_test_command(command)


# ═══════════════════════════════════════════════════════════════════════════════
# RuntimeState
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RuntimeState:
    """Agent 运行时客观状态，由系统在每轮工具调用后自动更新。

    所有字段都是客观事实（工具调用记录），不包含 agent 的主观判断。
    """

    # ── Turn / Time ──────────────────────────────────────────────────────────
    turn_count: int = 0
    max_turns: int = 80
    started_at: float = 0.0
    timeout_seconds: int = 0

    # ── Edit tracking ────────────────────────────────────────────────────────
    last_edit_turn: int = 0           # 最后一轮做了源码编辑的 turn 号
    edits_this_run: int = 0           # 本次 run 共做了多少次编辑

    # ── Diff status（由 diff_status 工具调用更新）─────────────────────────────
    has_diff: bool = False
    diff_chars: int = 0
    changed_files: list[str] = field(default_factory=list)
    tests_modified: bool = False
    source_only: bool = False

    # ── L1 acceptance criteria ───────────────────────────────────────────────
    acceptance_criteria: list[str] = field(default_factory=list)
    requested_paths: list[str] = field(default_factory=list)
    task_mode: str = "unknown"
    wants_tests: bool = False

    # ── Planning tracking ───────────────────────────────────────────────────
    plan_generated: bool = False
    plan_text: str = ""
    replan_count: int = 0
    initial_task_text: str = ""
    initial_plan_complexity: str = ""
    patch_risk: dict = field(default_factory=dict)
    risk_feedback_fingerprint: str = ""
    risk_replan_fingerprint: str = ""

    # ── Verification tracking ────────────────────────────────────────────────
    verification_attempts: int = 0    # 总验证尝试次数
    py_compile_ok: bool = False      # 兼容旧字段：最近一次 verify_changed_files 是否可接受
    changed_files_verified: bool = False
    broad_test_attempts: int = 0     # 跑了多少次 broad test runner
    exact_test_attempts: int = 0     # 跑了多少次精确测试

    # ── Search / Read tracking（检测空转）─────────────────────────────────────
    searched_patterns: list[str] = field(default_factory=list)
    read_files: list[str] = field(default_factory=list)
    investigation_calls_since_edit: int = 0
    mutation_generation: int = 0
    diff_generation: int = -1
    verification_generation: int = -1
    strict_progress_nudges: int = 0
    strict_progress_blocks: int = 0

    # ── Transition ────────────────────────────────────────────────────────────
    # 上轮做了什么，用于判断当前应该收敛还是继续探索
    # "edited_source" | "ran_broad_test" | "ran_exact_test" | "searched" | "read" | ""
    transition: str = ""

    # ── 标志 ──────────────────────────────────────────────────────────────────
    _state_block_emitted: bool = False
    _diff_seen_from_tool: bool = False   # diff_status 被调用过

    # ═══════════════════════════════════════════════════════════════════════════

    def reset(self, max_turns: int = 80, timeout_seconds: int = 0):
        """在每次 run() 开始时调用。"""
        self.turn_count = 0
        self.max_turns = max_turns
        self.started_at = time.time()
        self.timeout_seconds = timeout_seconds

        self.last_edit_turn = 0
        self.edits_this_run = 0

        self.has_diff = False
        self.diff_chars = 0
        self.changed_files = []
        self.tests_modified = False
        self.source_only = False
        self.acceptance_criteria = []
        self.requested_paths = []
        self.task_mode = "unknown"
        self.wants_tests = False

        self.plan_generated = False
        self.plan_text = ""
        self.replan_count = 0
        self.initial_task_text = ""
        self.initial_plan_complexity = ""
        self.patch_risk = {}
        self.risk_feedback_fingerprint = ""
        self.risk_replan_fingerprint = ""

        self.verification_attempts = 0
        self.py_compile_ok = False
        self.changed_files_verified = False
        self.broad_test_attempts = 0
        self.exact_test_attempts = 0

        self.searched_patterns = []
        self.read_files = []
        self.investigation_calls_since_edit = 0
        self.mutation_generation = 0
        self.diff_generation = -1
        self.verification_generation = -1
        self.strict_progress_nudges = 0
        self.strict_progress_blocks = 0

        self.transition = ""
        self._state_block_emitted = False
        self._diff_seen_from_tool = False

    def set_acceptance_criteria_from_text(self, text: str, limit: int = 5) -> None:
        """从用户任务文本中提取轻量 L1 验收标准。"""
        self.acceptance_criteria = extract_acceptance_criteria(text, limit=limit)
        self.requested_paths = extract_explicit_paths(text, limit=limit)
        self.task_mode = detect_task_mode(text)
        self.wants_tests = task_wants_tests(text)

    def task_complexity(self) -> str:
        """按当前 diff/edit 规模给任务分级：L0/L1/L2/L3。"""
        changed_count = len(self.changed_files)
        if self.edits_this_run == 0 and self.diff_chars == 0:
            return "L0"
        if self.edits_this_run <= 1 and self.diff_chars <= 1200 and changed_count <= 1:
            return "L1"
        if self.edits_this_run <= 3 and self.diff_chars <= 6000 and changed_count <= 4:
            return "L2"
        return "L3"

    def to_dict(self, active: bool = True) -> dict:
        """序列化 RuntimeState，用于中断恢复。"""
        data = asdict(self)
        data["active"] = active
        data["saved_at"] = time.time()
        return data

    def restore(self, data: dict) -> bool:
        """从 dict 恢复字段；返回是否恢复成功。"""
        if not isinstance(data, dict) or not data.get("active"):
            return False
        for key, value in data.items():
            if key in {"active", "saved_at"}:
                continue
            if hasattr(self, key):
                setattr(self, key, value)
        return True

    def save(self, path: Path, active: bool = True) -> None:
        """把 RuntimeState 写入磁盘。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(active=active), ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, path: Path) -> bool:
        """从磁盘恢复 active 状态。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return self.restore(data)

    # ── Observation ───────────────────────────────────────────────────────────

    def observe_tool(self, name: str, tool_input: dict | None, output: str):
        """根据工具调用更新状态。

        由 AgentLoop 在每次工具执行后调用。
        """
        tool_input = tool_input or {}
        output = output or ""

        if self.is_investigation_call(name, tool_input):
            self.investigation_calls_since_edit += 1

        # ── grep_search ──────────────────────────────────────────────────────
        if name == "grep_search":
            pattern = tool_input.get("pattern", "")
            if pattern and pattern not in self.searched_patterns:
                self.searched_patterns.append(pattern)
                if len(self.searched_patterns) > 30:
                    self.searched_patterns = self.searched_patterns[-20:]
            self.transition = "searched"

        # ── read_file / read_symbol / repo_map ────────────────────────────────
        elif name in ("read_file", "read_symbol", "repo_map", "code_references"):
            path = tool_input.get("path", "")
            if path and path not in self.read_files:
                self.read_files.append(path)
                if len(self.read_files) > 40:
                    self.read_files = self.read_files[-25:]
            self.transition = "read"

        # ── 源码编辑（write_file, edit_file, apply_patch, replace_lines,
        #     python_structural_edit）──────────────────────────────────────────
        elif name in ("write_file", "edit_file", "apply_patch", "replace_lines",
                      "python_structural_edit", "scaffold_project", "write_files_batch"):
            self.last_edit_turn = self.turn_count
            self.edits_this_run += 1
            self.mutation_generation += 1
            self.investigation_calls_since_edit = 0
            self.strict_progress_nudges = 0
            self.strict_progress_blocks = 0
            self.transition = "edited_source"

        # ── diff_status ──────────────────────────────────────────────────────
        elif name == "diff_status":
            self._diff_seen_from_tool = True
            # 解析 diff_status 输出中的关键字段
            if "has_non_empty_diff: true" in output:
                self.has_diff = True
            elif "has_non_empty_diff: false" in output:
                self.has_diff = False

            # 提取 diff_chars
            import re
            m = re.search(r"diff_chars:\s*(\d+)", output)
            if m:
                self.diff_chars = int(m.group(1))

            # 提取 changed_files
            in_files = False
            self.changed_files = []
            for line in output.splitlines():
                stripped = line.strip()
                if stripped == "Changed files:":
                    in_files = True
                    continue
                if in_files:
                    if stripped.startswith("- ") or stripped.startswith("  - "):
                        continue  # still in changed files section
                    if stripped == "(none)":
                        in_files = False
                    elif stripped and not stripped.startswith("R"):
                        in_files = False
                # Actually read the changed files from the "  path" format
            self.changed_files = self._parse_changed_files(output)

            if "tests_modified: true" in output:
                self.tests_modified = True
            elif "tests_modified: false" in output:
                self.tests_modified = False
            if "source_only: true" in output:
                self.source_only = True
            elif "source_only: false" in output:
                self.source_only = False

            if self.has_diff and self._strict_diff_scope_allowed():
                self.diff_generation = self.mutation_generation
            else:
                self.diff_generation = -1

            self.transition = "checked_diff"

        # ── verify_changed_files ──────────────────────────────────────────────
        elif name in ("verify_changed_files", "verify_project_build"):
            self.verification_attempts += 1
            if output.startswith(("OK:", "WARN:")):
                self.py_compile_ok = True
                self.changed_files_verified = True
                if output.startswith("OK:"):
                    self.verification_generation = self.mutation_generation
            elif output.startswith("FAIL:"):
                self.py_compile_ok = False
                self.changed_files_verified = False
                self.verification_generation = -1
            self.transition = "verified"

        # ── bash ──────────────────────────────────────────────────────────────
        elif name == "bash":
            command = tool_input.get("command", "")
            if _is_broad_test_command(command):
                self.verification_attempts += 1
                self.broad_test_attempts += 1
                self.transition = "ran_broad_test"
            elif _is_exact_test(command):
                self.verification_attempts += 1
                self.exact_test_attempts += 1
                self.transition = "ran_exact_test"


    def _parse_changed_files(self, output: str) -> list[str]:
        """从 diff_status 输出中解析 changed files 列表。

        diff_status 输出格式:
          Changed files:
            path/to/file.py
            ...
          空行或关键词结束文件列表。
        """
        files = []
        in_section = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped in ("Changed files:", "Changed files"):
                in_section = True
                continue
            if not in_section:
                continue
            # 空行结束文件列表
            if not stripped:
                break
            # 元数据行结束文件列表
            if ":" in stripped and not stripped.startswith(("  ", "- ")):
                if any(kw in stripped.lower() for kw in ("has_non_empty", "diff_char",
                        "changed_files", "python_files", "tests_mod", "source_only",
                        "recommendation", "git status")):
                    break
            # 跳过 "(none)"
            if stripped == "(none)":
                continue
            # 文件路径：以空格开头且不含冒号（避免匹配 "has_non_empty_diff: true"）
            if line.startswith("  ") and ":" not in stripped:
                fname = stripped
                if fname and not fname.startswith("has_") and not fname.startswith("diff_"):
                    files.append(fname)
        return files

    # ── State block generation ────────────────────────────────────────────────

    def strict_progress_action(
        self,
        tool_name: str,
        pending: int = 0,
        tool_input: dict | None = None,
    ) -> str:
        """Return whether strict mode may execute another investigation call."""
        if (
            self.is_investigation_call(tool_name, tool_input)
            and self.investigation_calls_since_edit + max(0, int(pending))
            >= STRICT_INVESTIGATION_HARD_LIMIT
        ):
            return "block"
        return "allow"

    def strict_generation_terminal_ready(self) -> bool:
        """Return whether the current mutation generation has final evidence.

        Diff and verification observations are intentionally order-independent,
        matching InfCodeX's terminal-signal contract: the consumer evaluates the
        settled state rather than the tool that happened to run last.
        """
        generation = self.mutation_generation
        return (
            generation > 0
            and self.diff_generation == generation
            and self.verification_generation == generation
            and self.has_diff
            and self._strict_diff_scope_allowed()
        )

    def _strict_diff_scope_allowed(self) -> bool:
        """Accept test changes only when the task explicitly calls for them."""
        return bool(
            (self.source_only and not self.tests_modified)
            or (self.tests_modified and self.wants_tests)
        )

    @staticmethod
    def is_investigation_tool(tool_name: str) -> bool:
        """Return whether a tool consumes the strict investigation budget."""
        return str(tool_name or "") in _INVESTIGATION_TOOLS

    @staticmethod
    def is_investigation_call(
        tool_name: str,
        tool_input: dict | None = None,
    ) -> bool:
        """Classify structured and strict read-only Bash investigations."""
        name = str(tool_name or "")
        if name in _INVESTIGATION_TOOLS:
            return True
        if name != "bash":
            return False
        command = str((tool_input or {}).get("command") or "").strip()
        tokens = command.split()
        if not tokens:
            return False
        if tokens[0] in _BASH_INVESTIGATION_COMMANDS:
            return True
        return len(tokens) > 1 and tokens[0] == "git" and tokens[1] == "grep"

    def build_prompt_block(self, strict: bool = False) -> str:
        """返回 <system-reminder> 状态块，注入到 system prompt 末尾。

        仅在状态有意义时才返回非空字符串（有 diff、接近限制、在空转等）。
        空字符串表示当前不需要注入状态提醒。
        """
        reminders: list[str] = []

        t_now = time.time()
        elapsed = int(t_now - self.started_at) if self.started_at else 0
        turns_remaining = max(0, self.max_turns - self.turn_count)
        time_remaining = max(0, self.timeout_seconds - elapsed) if self.timeout_seconds else 0
        complexity = self.task_complexity()

        if (
            strict
            and self.investigation_calls_since_edit >= STRICT_INVESTIGATION_SOFT_LIMIT
            and self.strict_progress_nudges == 0
        ):
            reminders.append(
                "STRICT CONVERGENCE: "
                f"{self.investigation_calls_since_edit} investigation calls have completed "
                "since the last source edit. Synthesize the evidence now. Make the smallest "
                "plausible source edit, call diff_status, or finish with a concrete blocker; "
                "do not broaden the search."
            )
            self.strict_progress_nudges += 1

        # ── 1. Turn / Time budget ────────────────────────────────────────────
        if complexity != "L0" or turns_remaining <= 10 or (time_remaining and time_remaining <= 120):
            budget_lines = [
                f"Turn {self.turn_count}/{self.max_turns}",
                f"task_complexity={complexity}",
                f"task_mode={self.task_mode}",
            ]
            if self.timeout_seconds:
                budget_lines.append(f"Time {elapsed}s/{self.timeout_seconds}s")
            budget_lines.append(f"{turns_remaining} turns remaining" +
                               (f", {time_remaining}s" if self.timeout_seconds else ""))
            reminders.append(" | ".join(budget_lines))

        # ── 2. Acceptance criteria（L1 定义层）───────────────────────────────
        if self.acceptance_criteria and (self.has_diff or self.verification_attempts == 0):
            criteria = "; ".join(self.acceptance_criteria[:5])
            reminders.append(f"Acceptance criteria ({len(self.acceptance_criteria)}): {criteria}")
        if self.requested_paths and (self.has_diff or self.verification_attempts == 0):
            targets = ", ".join(self.requested_paths[:5])
            reminders.append(
                f"User named target files ({len(self.requested_paths)}): {targets}. Prefer editing those exact paths or the closest existing match; do not create same-basename files elsewhere without confirming."
            )

        if self.patch_risk.get("requires_replan"):
            categories = ", ".join(
                str(item.get("category") or "unknown")
                for item in self.patch_risk.get("risk_signals", [])[:5]
                if isinstance(item, dict)
            ) or "unspecified"
            reminders.append(
                "PATCH RISK REVIEW REQUIRED: " + categories + ". Re-read the affected declarations, "
                "preserve public APIs and user-named scope unless explicitly requested, and revise the approach before finalizing."
            )

        if self.task_mode == "project_creation":
            reminders.append(
                "PROJECT CREATION MODE: start with analyze_project_requirements -> create_project_blueprint -> "
                "scaffold_project. If the scaffold still misses requested business logic, use write_files_batch. "
                "Then run inspect_generated_project -> check_project_completeness -> plan_project_acceptance -> "
                "verify_project_build. Do not start with grep_search unless you are intentionally reusing local code."
            )
            if not self.has_diff and self.turn_count >= 3 and self.edits_this_run == 0:
                reminders.append(
                    "No project files created yet. Move from planning to scaffold_project or write_files_batch now."
                )

        # ── 3. Diff status（如果有 diff，这是最重要的信息）───────────────────
        if self.has_diff:
            nc = self.diff_chars
            nf = len(self.changed_files)
            if self.tests_modified and (self.wants_tests or self.task_mode in {"test", "project_creation"}):
                test_note = " Includes test updates requested by the task; ensure they cover the change."
            elif self.tests_modified:
                test_note = " Includes test files; confirm this matches the user request."
            else:
                test_note = " Task explicitly asks for tests, but no test files have been changed yet." if self.wants_tests else ""
            verify_hint = (
                "Run plan_project_acceptance or verify_project_build before finalizing."
                if self.task_mode == "project_creation"
                else "Run verify_changed_files or the narrowest relevant project check before finalizing."
            )
            reminders.append(
                f"DIFF EXISTS: {nc} chars across {nf} file(s).{test_note} {verify_hint}"
            )

        # ── 4. Diminishing returns: 连续多轮没有编辑 ─────────────────────────
        no_edit_turns = (self.turn_count - self.last_edit_turn) if self.last_edit_turn else self.turn_count
        if self.task_mode != "discuss":
            if self.task_mode == "project_creation":
                edit_hint = "Move from requirements and blueprint work to concrete scaffold creation or batch file writing."
            elif self.task_mode in {"feature", "refactor", "test"}:
                edit_hint = "Move from exploration to the next concrete implementation or test update."
            else:
                edit_hint = "Stop broad exploration and make the smallest relevant code change."
            if self.turn_count >= 10 and no_edit_turns >= 8:
                reminders.append(
                    f"WARNING: No source edit in the last {no_edit_turns} turns. {edit_hint}"
                )
            elif self.turn_count >= 5 and no_edit_turns >= 5:
                reminders.append(f"No source edit in {no_edit_turns} turns. {edit_hint}")

        # ── 5. Verification budget ────────────────────────────────────────────
        if self.broad_test_attempts >= 3:
            reminders.append(
                f"STOP: {self.broad_test_attempts} broad test runs attempted. "
                "Do NOT run broad test runners again. Use verify_changed_files or a narrow targeted check."
            )
        # ── 6. Low budget warning ─────────────────────────────────────────────
        if turns_remaining <= 5 and self.has_diff:
            reminders.append(
                f"CRITICAL: Only {turns_remaining} turns remaining. "
                "Finalize your patch NOW. Do not start new explorations."
            )
        elif turns_remaining <= 10 and self.has_diff:
            reminders.append(
                f"Low budget: {turns_remaining} turns remaining. "
                "Verify and finalize your patch."
            )
        elif (time_remaining and time_remaining <= 60 and self.has_diff):
            reminders.append(
                "CRITICAL: Less than 60s remaining. Finalize your patch NOW."
            )

        # ── 7. Verification passed but still going ────────────────────────────
        if self.py_compile_ok and self.has_diff and self.verification_attempts >= 1:
            if no_edit_turns <= 2:
                reminders.append(
                    "verify_changed_files passed. Diff exists. You should FINALIZE now."
                )

        if not reminders:
            return ""

        block = "<system-reminder>\n" + "\n".join(f"- {r}" for r in reminders) + "\n</system-reminder>"
        return block


def extract_explicit_paths(text: str, limit: int = 5) -> list[str]:
    """Extract explicit file paths or basenames mentioned in the task text."""
    if not isinstance(text, str) or not text.strip():
        return []
    paths: list[str] = []
    pattern = r"(?<![\w/.-])([\w./-]+\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|go|rs|java|rb|php|c|cc|cpp|h|hpp|md|json|ya?ml))"
    for match in re.findall(pattern, text, flags=re.IGNORECASE):
        _append_unique(paths, match.strip(), limit)
        if len(paths) >= limit:
            break
    return paths


def extract_acceptance_criteria(text: str, limit: int = 5) -> list[str]:
    """从 issue/user 文本中提取轻量验收标准。

    只做保守启发式，不调用模型：优先保留精确测试、FAILED 行、编号/项目符号
    任务项，以及 should/must/中文“应/必须”句子。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    criteria: list[str] = []
    patterns = (
        r"[\w./-]+\.py(?:::[\w.\-]+)+",
        r"FAILED\s+[^\n]+",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text):
            _append_unique(criteria, match.strip(), limit)

    bullet_re = re.compile(r"^(?:\d+[.)]|[-*])\s+(.+)$")
    action_markers = ("新增", "修改", "增加", "更新", "支持", "返回", "抛出", "覆盖", "生成", "编写", "校验", "验证", "add ", "update ", "return ", "raise ", "write ", "create ", "test ")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        match = bullet_re.match(stripped)
        if not match:
            continue
        item = match.group(1).strip()
        lowered = item.lower()
        if any(marker in lowered for marker in ("should", "must", "expected", "failing")):
            _append_unique(criteria, item[:180], limit)
        elif any(marker in item for marker in ("应该", "必须", "期望", "失败", "报错")):
            _append_unique(criteria, item[:180], limit)
        elif any(marker in lowered for marker in action_markers):
            _append_unique(criteria, item[:180], limit)
        elif re.search(r"[\w./-]+\.(?:py|js|ts|go|rs|rb)\b", item, flags=re.IGNORECASE):
            _append_unique(criteria, item[:180], limit)
        if len(criteria) >= limit:
            break

    for sentence in re.split(r"(?<=[.!?。！？])\s+|\n+", text):
        stripped = sentence.strip(" -\t")
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in (" should ", " must ", " expected ", " fails ", " failing ")):
            _append_unique(criteria, stripped[:180], limit)
        elif any(marker in stripped for marker in ("应该", "必须", "期望", "失败", "报错")):
            _append_unique(criteria, stripped[:180], limit)
        if len(criteria) >= limit:
            break
    return criteria


def _append_unique(items: list[str], value: str, limit: int) -> None:
    if value and value not in items and len(items) < limit:
        items.append(value)
