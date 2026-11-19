"""当前真实用户消息的模型工作记忆；主观笔记不替代执行与验证状态。"""
from __future__ import annotations

import hashlib
import json
import math
import time
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock

from nz_coder.protocol.public_error import PublicInputError, format_public_error, to_public_error
from nz_coder.tools import ToolOutput, register

SECTIONS = ("focus", "findings", "decisions", "lessons")

# 主动记录与专门整理共用内容约定；框架不据此判断笔记真假或自动补事实。
WORKING_MEMORY_GUIDANCE = """Keep notes that help this task continue after history is compressed.
focus: unresolved investigation questions, blockers and their basis or applicability conditions; move still-useful conclusions
to the other sections when a focus item is finished.
findings: costly-to-rediscover conclusions, interface/environment behavior and unresolved hypotheses;
retain observed evidence and certainty without promoting an inference to a confirmed fact.
decisions: the chosen approach, its reason, important tradeoffs and known conditions for reconsidering it.
lessons: failed approaches, observed causes and the conditions under which the lesson applies.
Every summary is shown automatically: make it self-contained, including the conclusion and any
known applicability or invalidation condition that changes the next action. Include essential reasons,
not just topic titles. Keep long reasoning, rejected alternatives, reproductions and recheck details
in details; findings.basis records available source paths, functions, commands or tool observations.
Do not invent missing evidence or conditions. Preserve still-useful costly information, conditions,
reasons and uncertainty when merging or replacing notes; age alone does not make a note obsolete.
Do not maintain another copy of the user contract, stage route, current check-status table or accepted
handoff action. A candidate investigation suggestion is advice; preserve why it matters, not a second
current-action record. Historical observations may explain a finding but do not certify current status.
Current execution and verification facts take precedence over notes; confirmed is a model label,
not current verification proof. There are no minimum counts or per-section quotas.
"""


def empty_memory() -> dict:
    return {section: [] for section in SECTIONS}


def validate_entries(section: str, entries: list, *, stored: bool = False) -> None:
    if section not in SECTIONS or not isinstance(entries, list):
        raise PublicInputError("invalid working memory section or entries")
    fields = {"summary", "details"}
    if section == "findings":
        fields |= {"certainty", "basis"}
    if stored:
        fields.add("ts")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != fields:
            raise PublicInputError(f"invalid working memory fields in {section}")
        if not isinstance(entry["summary"], str) or not entry["summary"].strip():
            raise PublicInputError("working memory summary must be a nonempty string")
        if not isinstance(entry["details"], str):
            raise PublicInputError("working memory details must be a string")
        if section == "findings" and (
            entry["certainty"] not in ("confirmed", "hypothesis")
            or not isinstance(entry["basis"], str)
        ):
            raise PublicInputError("invalid finding certainty or basis")
        if stored and (type(entry["ts"]) not in (float, int) or not math.isfinite(entry["ts"])):
            raise ValueError("invalid working memory timestamp")


def validate_memory(memory: dict, *, stored: bool = False) -> None:
    if not isinstance(memory, dict) or set(memory) != set(SECTIONS):
        raise PublicInputError("working memory requires all four sections")
    for section in SECTIONS:
        validate_entries(section, memory[section], stored=stored)


def validate_payload(payload: dict) -> None:
    """运行时只加载新格式；迁移是显式离线操作，不在读取时猜测。"""
    if (not isinstance(payload, dict) or set(payload) != {"version", "memory", "reminder"}
            or type(payload["version"]) is not int or payload["version"] != 2):
        raise ValueError("unsupported working memory format/version; migrate the stopped session offline")
    validate_memory(payload["memory"], stored=True)
    reminder = payload["reminder"]
    if (not isinstance(reminder, dict) or set(reminder) != {
        "revision", "completed_steps", "last_check_step", "pressure_notified"
    } or any(type(reminder[k]) is not int or reminder[k] < 0
             for k in ("revision", "completed_steps", "last_check_step"))
            or type(reminder["pressure_notified"]) is not bool
            or reminder["last_check_step"] > reminder["completed_steps"]):
        raise ValueError("invalid working memory reminder format")


@dataclass(frozen=True)
class _ScratchpadState:
    memory: dict
    revision: int = 0
    completed_steps: int = 0
    last_check_step: int = 0
    pressure_notified: bool = False


class Scratchpad:
    """唯一持有四区正文；外部只取得快照，成功落盘后才发布状态。"""

    def __init__(self, path: Path | None = None, *, persistent: bool = True) -> None:
        self._path = path
        self._persistent = persistent
        self._lock = RLock()
        self._states: dict[tuple[str, str], _ScratchpadState] = {}

    @classmethod
    def for_user_message(cls, session_id: str, message_id: str) -> Scratchpad:
        from nz_coder.state.sessions import session_runtime_dir
        name = hashlib.sha256(message_id.encode()).hexdigest() + ".json"
        return cls(session_runtime_dir(session_id) / "scratchpad" / name)

    def snapshot(self) -> tuple[dict, int]:
        with self._lock:
            state = self._session_state()
            return deepcopy(state.memory), state.revision

    @property
    def memory(self) -> dict:
        return self.snapshot()[0]

    def _session_key(self) -> tuple[str, str]:
        if not self._persistent:
            return "", ""
        if self._path is not None:
            return str(self._path.parent), self._path.stem
        from nz_coder.runtime.process.workdir import current_workdir
        from nz_coder.state.sessions import active_session_id
        return str(current_workdir()), active_session_id() or "default"

    def _session_state(self) -> _ScratchpadState:
        with self._lock:
            key = self._session_key()
            if key not in self._states:
                self._states[key] = self._load_state(key[1])
            return self._states[key]

    def _load_state(self, session_id: str) -> _ScratchpadState:
        if not self._persistent:
            return _ScratchpadState(empty_memory())
        from nz_coder.state.sessions import session_scratchpad_path
        path = self._path if self._path is not None else session_scratchpad_path(session_id)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _ScratchpadState(empty_memory())
        try:
            payload = json.loads(text)
            validate_payload(payload)
        except ValueError as exc:
            raise ValueError(f"Invalid working memory format at {path}: {exc}") from exc
        return _ScratchpadState(payload["memory"], **payload["reminder"])

    def _save_state(self, state: _ScratchpadState) -> str | None:
        key = self._session_key()
        error = self._persist_state(key[1], state)
        if error is None:
            self._states[key] = state
        return error

    def _persist_state(self, session_id: str, state: _ScratchpadState) -> str | None:
        if not self._persistent:
            return None
        from nz_coder.state.sessions import session_scratchpad_path, write_session_runtime_json
        path = self._path if self._path is not None else session_scratchpad_path(session_id)
        try:
            write_session_runtime_json(path, {
                "version": 2, "memory": state.memory,
                "reminder": {
                    "revision": state.revision, "completed_steps": state.completed_steps,
                    "last_check_step": state.last_check_step, "pressure_notified": state.pressure_notified,
                },
            })
        except Exception as exc:
            return to_public_error(exc).message
        return None

    @property
    def revision(self) -> int:
        return self._session_state().revision

    @property
    def completed_steps(self) -> int:
        return self._session_state().completed_steps

    def reminder_reason(self, interval: int, under_pressure: bool) -> str:
        """只查询，不因预检、重复冻结或估算而消费提醒。"""
        if interval <= 0:
            return ""
        state = self._session_state()
        if under_pressure and not state.pressure_notified:
            return "pressure"
        if state.completed_steps - state.last_check_step >= interval:
            return "periodic"
        return ""

    def complete_step(
        self, revision_before: int, *, under_pressure: bool = False,
    ) -> str | None:
        """主响应及工具批次结清后调用；一次响应中的多个工具只计一步。"""
        with self._lock:
            state = self._session_state()
            step = state.completed_steps + 1
            checked = state.revision != revision_before
            return self._save_state(replace(
                state, completed_steps=step,
                last_check_step=step if checked else state.last_check_step,
                pressure_notified=state.pressure_notified if under_pressure else False,
            ))

    def begin_maintenance(self, *, under_pressure: bool) -> str | None:
        """先保存尝试窗口，避免结果写盘失败后每一步重复请求。"""
        with self._lock:
            state = self._session_state()
            return self._save_state(replace(
                state, last_check_step=state.completed_steps,
                pressure_notified=under_pressure,
            ))

    def replace_memory(self, memory: dict, *, expected_revision: int) -> str | None:
        try:
            validate_memory(memory)
        except ValueError as exc:
            return to_public_error(exc).message
        with self._lock:
            if self._session_state().revision != expected_revision:
                return "stale working memory revision"
            return self._save_memory({section: self._stamp(entries) for section, entries in memory.items()})

    @staticmethod
    def _stamp(entries: list[dict]) -> list[dict]:
        return [{**deepcopy(entry), "ts": time.time()} for entry in entries]

    def _save_memory(self, memory: dict, *, clear: bool = False) -> str | None:
        state = self._session_state()
        return self._save_state(replace(
            state, memory=memory, revision=state.revision + 1,
            completed_steps=0 if clear else state.completed_steps,
            last_check_step=0 if clear else state.completed_steps,
            pressure_notified=False if clear else state.pressure_notified,
        ))

    def update_section(self, section: str, entries: list[dict], *, mode: str = "append") -> str:
        if mode not in {"append", "replace"}:
            return "Error: mode must be append or replace"
        try:
            validate_entries(section, entries)
        except ValueError as exc:
            return format_public_error(exc)
        with self._lock:
            memory = self.memory
            notes = self._stamp(entries)
            memory[section] = memory[section] + notes if mode == "append" else notes
            error = self._save_memory(memory)
            if error:
                return f"Error: failed to persist scratchpad: {error}"
        return f"Scratchpad updated [{section}]: {len(entries)} entries ({mode})."

    def read(self) -> str:
        memory = self.memory
        if not any(memory.values()):
            return "Scratchpad is empty."
        return "# Scratchpad (complete working memory)\n" + json.dumps(memory, ensure_ascii=False, indent=2)

    def build_prompt_block(self, *, include_read_hint: bool = True) -> str:
        memory = self.memory
        if not any(memory.values()):
            return ""
        header = ("\n## Working Memory (Scratchpad)\n"
                  "Model-authored notes may be stale. Current runtime and verification facts take precedence.\n")
        if include_read_hint:
            header += ("All summaries follow; use read_scratchpad when an action needs full evidence, "
                       "applicability conditions or recheck details. Recheck notes that conflict with current facts.\n")
        lines = []
        for section in SECTIONS:
            for entry in memory[section]:
                certainty = f" [{entry['certainty']}]" if section == "findings" else ""
                lines.append(f"- [{section}]{certainty} {entry['summary']}")
        return header + "\n".join(lines) + "\n"

    def clear(self) -> str:
        with self._lock:
            count = sum(len(entries) for entries in self._session_state().memory.values())
            error = self._save_memory(empty_memory(), clear=True)
            if error:
                return f"Error: failed to persist scratchpad: {error}"
        return f"Scratchpad cleared ({count} entries removed)."


scratchpad = Scratchpad()
_CURRENT_SCRATCHPAD: ContextVar[Scratchpad] = ContextVar("working_scratchpad", default=scratchpad)


def current_scratchpad() -> Scratchpad:
    return _CURRENT_SCRATCHPAD.get()


@contextmanager
def scoped_scratchpad(pad: Scratchpad):
    token = _CURRENT_SCRATCHPAD.set(pad)
    try:
        yield pad
    finally:
        _CURRENT_SCRATCHPAD.reset(token)


def set_agent_loop(loop) -> None:  # noqa: ARG001
    pass


def _update_scratchpad(
    category: str | None = None, content: str | None = None,
    next_action: dict | None = None, mode: str = "append", *,
    section: str | None = None, entries: list[dict] | None = None,
) -> str:
    if mode not in {"append", "replace"}:
        return "Error: mode must be append or replace"
    if next_action is not None:
        if section is not None or entries is not None:
            return "Error: memory entries and execution handoff are mutually exclusive"
        if mode == "replace":
            return "Error: replace cannot be combined with next_action; submit notes separately"
        if category != "plan" or not isinstance(content, str):
            return "Error: next_action requires category='plan' and content"
        return ToolOutput("Execution handoff proposed; awaiting runtime acceptance.", metadata={
            "execution_handoff_proposal": {"hypothesis": content, "next_action": next_action},
        })
    if category is not None or content is not None:
        return "Error: memory writes require section and entries; category/content require next_action"
    return current_scratchpad().update_section(section, entries, mode=mode)


def _read_scratchpad() -> str:
    return current_scratchpad().read()


# Provider 会展平组合 schema；声明全部字段，由处理器校验分支与分区约束。
_ENTRY_SCHEMA = {
    "type": "object", "properties": {
        "summary": {"type": "string", "minLength": 1,
                    "description": "Self-contained conclusion with known conditions that change the next action; all summaries enter future coding requests."},
        "details": {"type": "string", "description": "Full reasoning, rejected alternatives, reproductions and recheck conditions; read_scratchpad retrieves it."},
        "certainty": {"type": "string", "enum": ["confirmed", "hypothesis"],
                      "description": "Required only for findings; model label, not verification proof."},
        "basis": {"type": "string", "description": "Required only for findings; available source paths, functions, commands or observations. May be empty; do not invent evidence."},
    }, "required": ["summary", "details"], "additionalProperties": False,
}

register(
    name="update_scratchpad",
    description=(
        "Maintain current-request notes: focus=questions/blockers/basis, findings=costly conclusions/evidence, "
        "decisions=choices/reasons, lessons=failed approaches/causes. Keep conclusions, conditions and "
        "uncertainty in summary; reasoning/rechecks in details. Findings need certainty+basis. "
        "Do not copy current runtime status or accepted actions. section+entries updates notes; "
        "mode=replace replaces a section; [] clears. OR category=plan, content=hypothesis, "
        "next_action proposes a handoff without notes (no section/entries, mode=append). "
        "Acceptance does not execute or authorize."
    ),
    parameters={
        "type": "object",
        "properties": {
            "section": {"type": "string", "enum": list(SECTIONS)},
            "entries": {"type": "array", "items": _ENTRY_SCHEMA},
            "category": {"type": "string", "enum": ["plan"]},
            "content": {"type": "string", "description": "Execution handoff hypothesis only."},
            "mode": {"type": "string", "enum": ["append", "replace"], "default": "append"},
            "next_action": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["inspect", "edit", "verify", "blocked"]},
                    "evidence": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
                    "targets": {"type": "array", "maxItems": 3, "items": {
                        "type": "object", "properties": {
                            "path": {"type": "string"}, "symbol": {"type": "string"},
                            "operation": {"type": "string", "enum": ["modify", "create", "read"]},
                        }, "required": ["path", "symbol", "operation"], "additionalProperties": False,
                    }},
                    "action": {"type": "string", "description": "One concrete minimal action or missing condition; max 500 chars."},
                    "verification_command": {"type": "string", "description": "Required for verify; not executed automatically."},
                    "verification_cwd": {"type": "string"},
                },
                "required": ["kind", "evidence", "targets", "action"],
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    handler=_update_scratchpad,
)

register(
    name="read_scratchpad",
    description=("Read complete structured working memory for your current user request: "
                 "summaries, details, finding certainty/basis, and timestamps. "
                 "Only summaries are included automatically. Read when the next action needs full "
                 "evidence, applicability conditions or recheck details; resolve conflicts against current facts."),
    parameters={"type": "object", "properties": {}},
    handler=_read_scratchpad,
    execution="read",
)
