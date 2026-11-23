"""Read-only terminal projections for conversation turns and saved Sessions."""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re

from rich.table import Table
from rich.text import Text

from nz_coder.interface.presentation_tokens import clip_terminal_text
from nz_coder.protocol.message_schema import (
    ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY,
    INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY,
    MODEL_NOTE_KEY,
    PARTS_KEY,
    SUMMARY_KEY,
    USER_TIME_KEY,
    is_user_request_message,
    legacy_messages,
    public_message_content,
    rebind_fork_history as rebind_fork_history,
)
from nz_coder.runtime.session.history import HistoryUnavailable, is_history_marker, restore_session_history
from nz_coder.state.input_expansion import INPUT_EXPANSIONS_KEY, USER_TEXT_KEY, user_instruction_text
from nz_coder.state.sessions import active_session_id, list_sessions, load_session, SessionReadError
from nz_coder.tool_platform.permissioning.modes import MODES


@dataclass(frozen=True)
class ConversationTurn:
    """One visible user turn and its following Agent messages."""

    number: int
    message_id: str | None
    start: int
    end: int
    assistant_indices: tuple[int, ...]
    user_text: str
    assistant_text: str
    tools: tuple[str, ...]
    changed_files: tuple[str, ...]
    additions: int
    deletions: int


@dataclass(frozen=True)
class TranscriptBlock:
    """One independently renderable message block in the terminal transcript."""

    message_id: str
    role: str
    markdown: str
    turn_number: int | None = None
    navigable: bool = True
    part_id: str | None = None
    compact_markdown: str | None = None


@dataclass(frozen=True)
class TranscriptDocument:
    """Structured transcript projection with durable message identity."""

    header: str
    blocks: tuple[TranscriptBlock, ...]
    open_detail: Callable[[str], tuple[str, str] | None] | None = None

    def markdown(self) -> str:
        """Return the legacy whole-document Markdown representation."""
        sections = [self.header, *(block.markdown for block in self.blocks)]
        return "\n".join(section.rstrip() for section in sections if section).rstrip() + "\n"


@dataclass(frozen=True)
class SessionOption:
    """Metadata used by both the Session table and keyboard picker."""

    session_id: str
    title: str
    active: bool
    timestamp: str
    message_count: int
    model: str
    mode: str
    unavailable: str = ""


def conversation_turns(messages: list[dict], *, complete: bool = True) -> list[ConversationTurn]:
    """Group history at real user prompts while hiding injected diagnostics."""
    boundaries = [
        index for index, message in enumerate(messages)
        if isinstance(message, dict)
        and _is_turn_user(message)
    ]
    starts = [index for index in boundaries if _visible_message(messages[index])]
    positions = dict.fromkeys(boundaries, -1)
    positions.update({index: number for number, index in enumerate(starts)})
    ends = dict(zip(boundaries, [*boundaries[1:], len(messages)]))
    owners = {messages[index][MESSAGE_ID_KEY]: number for index, number in positions.items()
              if isinstance(messages[index].get(MESSAGE_ID_KEY), str)}
    grouped: list[list[int]] = [[] for _ in starts]
    position = -1
    # 旧记录靠排列顺序归属；已有父身份的迟到回复始终归到原问题。
    for index, message in enumerate(messages):
        if index in positions:
            position = positions[index]
        elif isinstance(message, dict) and message.get("role") == "assistant" and _visible_message(message):
            parent = message.get(ASSISTANT_PARENT_KEY)
            # 旧头缺失时，明指未知父消息的答复不能降级分配给当前尾轮。
            owner = owners.get(parent, position if complete else -1) if isinstance(parent, str) else position
            if owner >= 0:
                grouped[owner].append(index)
    turns: list[ConversationTurn] = []
    for position, start in enumerate(starts):
        end = ends[start]
        user_text = _message_text(messages[start])
        assistant_text = ""
        tools: dict[str, None] = {}
        changed_files, additions, deletions = _turn_diff(messages[start])
        user_id = messages[start].get(MESSAGE_ID_KEY)
        for index in grouped[position]:
            message = messages[index]
            assistant_text = _message_text(message) or assistant_text
            for call in message.get("tool_calls", []) or []:
                if isinstance(call, dict):
                    name = str(call.get("function", {}).get("name") or "").strip()
                    if name:
                        tools[name] = None
            for part in message.get(PARTS_KEY, []) or []:
                if isinstance(part, dict) and part.get("type") == "tool":
                    name = str(part.get("tool") or "").strip()
                    if name:
                        tools[name] = None
        turns.append(ConversationTurn(
            number=position + 1,
            message_id=user_id if isinstance(user_id, str) else None,
            start=start,
            end=end,
            assistant_indices=tuple(grouped[position]),
            user_text=user_text,
            assistant_text=assistant_text,
            tools=tuple(tools),
            changed_files=changed_files,
            additions=additions,
            deletions=deletions,
        ))
    return turns


@dataclass
class TurnHistory:
    """历史命令共享一次还原，以及旧记录不能确认时仍可查看的尾部。"""

    messages: list[dict]
    turns: list[ConversationTurn]
    unavailable: HistoryUnavailable | None = None

    @classmethod
    def load(cls, messages: list[dict], *, session_id: str, workspace: Path) -> TurnHistory:
        try:
            restored = restore_session_history(messages, session_id=session_id, workspace=workspace)
        except HistoryUnavailable as exc:
            return cls(exc.messages, conversation_turns(exc.messages, complete=False), exc)
        return cls(list(restored), conversation_turns(restored))

    @property
    def readable(self) -> list[ConversationTurn]:
        start = self.unavailable.complete_from if self.unavailable else 0
        return [turn for turn in self.turns if turn.start >= start]

    def label(self, turn: ConversationTurn) -> str:
        if self.unavailable and not self.unavailable.numbers_known:
            return "Latest user turn" if turn == self.turns[-1] else "User turn"
        return f"Turn {turn.number}"

    def select(self, number: int | None) -> ConversationTurn | None:
        if self.unavailable and (not self.turns or number is not None and not self.unavailable.numbers_known):
            raise self.unavailable
        selected = len(self.turns) if number is None else number
        if not 1 <= selected <= len(self.turns):
            return None
        turn = self.turns[selected - 1]
        if self.unavailable and turn.start < self.unavailable.complete_from:
            raise self.unavailable
        return turn

    def detail(self, session_id: str, turn: ConversationTurn, *, remote: bool = False) -> str:
        return format_turn_detail(session_id, turn, self.messages, remote=remote, title=self.label(turn))

    def export(self, session_id: str, *, title: str, tool_details: bool = True, remote: bool = False) -> dict[str, str]:
        """以归档原序导出可公开的正文；明确标注只能还原部分记录的情况。"""
        sources = self._export_messages()
        display = _remote_transcript_messages(sources) if remote else sources
        document = build_transcript_document(session_id, display, title=title, tool_details=tool_details)
        payload = {"session_id": session_id}
        if self.unavailable:
            notice = f"Partial transcript. {self.unavailable}"
            document = TranscriptDocument(f"{document.header}\n> {notice}\n", document.blocks)
            payload["notice"] = notice
        payload["markdown"] = document.markdown()
        return payload

    def _export_messages(self) -> list[dict]:
        users = {turn.start for turn in self.turns}
        hidden_user = any(_is_turn_user(message) and not _visible_message(message) for message in self.messages)
        # 可确认的用户副本也有原文。回复和工具回执须先在原始图中确认归属，再移除身份字段。
        if self.unavailable or hidden_user:
            assistants = {index for turn in self.readable for index in turn.assistant_indices}
        else:
            assistants = {i for i, m in enumerate(self.messages) if m.get("role") == "assistant" and _visible_message(m)}
        owners = _tool_result_owners(self.messages)
        hidden_calls = {}
        restricted = bool(self.unavailable) or any(not _visible_message(m) for m in self.messages)
        result = []
        for index, message in enumerate(self.messages):
            if not _visible_message(message):
                continue
            role = message.get("role")
            if role == "user" and index in users:
                if USER_TEXT_KEY in message or message.get(INPUT_EXPANSIONS_KEY):
                    message = {**message, "content": user_instruction_text(message) or ""}
            elif role == "assistant" and index in assistants:
                message, masked = _export_assistant(message)
                if masked:
                    hidden_calls[index] = masked
                    restricted = True
            elif role == "tool":
                matches = owners.get(index, [])
                if matches or restricted:
                    if len(matches) != 1 or matches[0] not in assistants:
                        continue
                    if message.get("tool_call_id") in hidden_calls.get(matches[0], ()):
                        continue
            else:
                continue
            result.append(message)
        return result

    def fork_turn(self, number: int | None) -> ConversationTurn:
        if self.unavailable:
            raise self.unavailable
        if not self.turns:
            raise ValueError("No user turns are available to fork")
        selected = self.select(number)
        if selected is None:
            raise ValueError(f"Turn must be between 1 and {len(self.turns)}")
        return selected

    def fork_prefix(self, turn: ConversationTurn) -> list[dict]:
        if self.unavailable:
            raise self.unavailable
        prefix = self.messages[:turn.end]
        if turn.end == len(self.messages):
            return prefix
        # 工具可能原地回写较早的助手消息。只有晚到的独立记录可以直接截去；
        # 已被覆盖、无法证明当时内容的记录不能作为历史分支发布。
        cutoff = _recorded_time(self.messages[turn.end], USER_TIME_KEY, "created")
        undated_tools = {}
        for index, message in enumerate(prefix):
            if message.get("role") != "assistant":
                continue
            completed = _recorded_time(message, ASSISTANT_TIME_KEY, "completed")
            if cutoff is not None and completed is not None and completed > cutoff:
                raise HistoryUnavailable(self.messages, turn.end, True)
            for part in message.get(PARTS_KEY, []) or []:
                if not isinstance(part, dict):
                    continue
                state = part.get("state") if part.get("type") == "tool" else part
                if not isinstance(state, dict):
                    continue
                end = _recorded_time(state, "time", "end")
                if cutoff is not None and end is not None:
                    if end > cutoff:
                        raise HistoryUnavailable(self.messages, turn.end, True)
                elif (part.get("type") == "tool" and state.get("status") in {"completed", "error"}
                      and (cutoff is None or completed is None)):
                    call_id = part.get("call_id")
                    if not isinstance(call_id, str) or not call_id:
                        raise HistoryUnavailable(self.messages, turn.end, True)
                    error = state.get("error")
                    output = state.get("output", error.get("message") if isinstance(error, dict) else error)
                    undated_tools[index, call_id] = output
        if undated_tools:
            # 没有可比时间时，仅边界之前同一调用的独立回执能证明此输出已经存在。
            for index, owners in _tool_result_owners(prefix).items():
                if len(owners) == 1:
                    receipt = prefix[index]
                    key = (owners[0], receipt["tool_call_id"])
                    if key in undated_tools and isinstance(receipt.get("content"), str) and receipt["content"] == undated_tools[key]:
                        del undated_tools[key]
            if undated_tools:
                raise HistoryUnavailable(self.messages, turn.end, True)
        return prefix

    def timeline(self, limit: int) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("Timeline limit must be between 1 and 100")
        known = self.unavailable is None or self.unavailable.numbers_known
        rows = []
        for turn in self.readable[-limit:]:
            rows.append({
                "number": turn.number if known else None,
                "user": _preview(turn.user_text, 240), "assistant": _preview(turn.assistant_text, 240),
                "tools": _preview(", ".join(turn.tools), 240),
                "changes": (f"{len(turn.changed_files)} files +{turn.additions}/-{turn.deletions}"
                            if turn.changed_files else ""),
            })
        return {"turns": rows, "notice": str(self.unavailable) if self.unavailable else ""}


def _complete_turn_history(messages: list[dict]) -> TurnHistory:
    if any(isinstance(message, dict) and is_history_marker(message) for message in messages):
        raise ValueError("Compacted history must be restored from the original Session first.")
    return TurnHistory(messages, conversation_turns(messages))


def fork_history(messages: list[dict], turn_number: int) -> list[dict]:
    """Copy history through one complete visible user turn."""
    view = _complete_turn_history(messages)
    return copy.deepcopy(view.fork_prefix(view.fork_turn(turn_number)))


def forked_session_title(title: str) -> str:
    """Return InfCode's monotonically numbered title for a Session fork."""
    match = re.match(r"^(.+) \(fork #(\d+)\)$", str(title))
    if match:
        return f"{match.group(1)} (fork #{int(match.group(2)) + 1})"
    return f"{title} (fork #1)"


def render_timeline(messages: list[dict], *, limit: int = 20) -> Table:
    """Build a bounded turn table suitable for Rich and plain capture."""
    view = _complete_turn_history(messages)
    return render_timeline_rows(view.timeline(max(1, min(int(limit), 100))))


def render_timeline_rows(projection: dict, *, title: str = "Session timeline") -> Table:
    table = Table(title=title, caption=projection["notice"] or None, show_lines=True, expand=True)
    table.add_column("Turn", justify="right", style="cyan", no_wrap=True)
    table.add_column("User")
    table.add_column("Agent")
    table.add_column("Tools", style="yellow")
    table.add_column("Changes", style="green", no_wrap=True)
    for row in projection["turns"]:
        table.add_row(
            str(row["number"]) if row["number"] is not None else "—",
            Text(row["user"] or "(empty)"), Text(row["assistant"] or "(no final text)"), Text(row["tools"] or "—"),
            row["changes"] or "—",
        )
    if not projection["turns"] and not projection["notice"]:
        table.add_row("—", "No user turns.", "—", "—", "—")
    return table


def render_sessions(*, limit: int = 20) -> Table:
    """Build a metadata-only saved-Session table without loading Agent owners."""
    table = Table(title="Saved sessions", show_lines=False, expand=True)
    table.add_column("", width=1)
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Updated", no_wrap=True)
    table.add_column("Messages", justify="right")
    table.add_column("Model")
    table.add_column("Mode")
    rows = session_options(limit=limit)
    for option in rows:
        table.add_row(
            "●" if option.active else "",
            option.session_id,
            option.title or "—",
            option.timestamp,
            str(option.message_count),
            option.model,
            option.mode,
        )
    if not rows:
        table.add_row("", "No saved sessions.", "—", "—", "0", "—", "—")
    return table


def format_transcript(
    session_id: str,
    messages: list[dict],
    *,
    title: str = "",
    tool_details: bool = True,
    thinking: bool = True,
) -> str:
    """Format one Session as an InfCode-style Markdown transcript."""
    return build_transcript_document(
        session_id,
        messages,
        title=title,
        tool_details=tool_details,
        thinking=thinking,
    ).markdown()


def format_turn_detail(
    session_id: str, turn: ConversationTurn, messages: list[dict], *, remote: bool = False, title: str = "",
) -> str:
    """仅在用户打开某轮详情时附上该轮保存的模型原稿。"""
    # 继续执行时，较早请求的答复可能迟到。沿用时间线认定的归属，不靠它恰好排在哪条用户消息后面。
    selected = _turn_messages(messages, turn)
    # 远程客户端只取得原有公开消息字段，本地检查仍可展开工具详情。
    display = selected
    if remote:
        visible = [message for message in selected if message.get("role") != "user" or _is_turn_user(message)]
        display = _remote_transcript_messages(visible)
    transcript = format_transcript(session_id, display, title=title or f"Turn {turn.number}")
    for index in turn.assistant_indices:
        message = messages[index]
        note = message.get(MODEL_NOTE_KEY)
        if message.get("_nz_authoritative") is not False and isinstance(note, str) and note.strip():
            transcript += "\n### 模型说明（执行声明以本轮回执为准）\n\n" + _fenced(note) + "\n"
    return transcript


def _remote_transcript_messages(visible: list[dict]) -> list[dict]:
    display = legacy_messages(visible)
    # 服务端先识别用户亲自提交的标签示例。来源字段只供 Markdown 渲染，不进入远程协议。
    for source, public in zip(visible, display):
        if source.get("role") == "user" and USER_TEXT_KEY in source:
            public[USER_TEXT_KEY] = source[USER_TEXT_KEY]
    return display


def _export_assistant(message: dict) -> tuple[dict, set[str]]:
    message = public_message_content(message)
    parts = message.get(PARTS_KEY) or []
    hidden = [part for part in parts if isinstance(part, dict) and (
        part.get("internal") is True or part.get("visible") is False
        or part.get("type") == "text" and (part.get("ignored") or part.get("synthetic"))
    )]
    if not hidden:
        return message, set()
    visible = [part for part in parts if part not in hidden]
    masked = {part["call_id"] for part in hidden if part.get("type") == "tool" and isinstance(part.get("call_id"), str)}
    message = {**message, PARTS_KEY: visible}
    if masked:
        message["tool_calls"] = [call for call in message.get("tool_calls") or []
                                 if isinstance(call, dict) and call.get("id") not in masked]
    if any(part.get("type") == "text" for part in hidden):
        # 原始 content 可能连着多个文本块。只要其中有撤回的块，就不能回退到整段。
        message["content"] = "\n".join(part["text"] for part in visible if isinstance(part, dict)
                                       and part.get("type") == "text" and isinstance(part.get("text"), str))
    return message, masked


def _turn_messages(messages: list[dict], turn: ConversationTurn) -> list[dict]:
    assistants = set(turn.assistant_indices)
    owners = _tool_result_owners(messages)
    selected = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        belongs = turn.start <= index < turn.end
        if role == "assistant":
            belongs = index in assistants
        elif index in owners:
            matches = owners[index]
            belongs = len(matches) == 1 and matches[0] in assistants
        if belongs and message.get("_nz_visible") is not False and message.get("_nz_internal") is not True:
            selected.append(message)
    return selected


def _tool_result_owners(messages: list[dict]) -> dict[int, list[int]]:
    # 工具结果可能晚于下一次用户输入。记录实际发出调用的消息，正常结算后允许 ID 重用。
    pending: dict[str, set[int]] = {}
    owners = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            calls = [call.get("id") for call in message.get("tool_calls") or [] if isinstance(call, dict)]
            calls.extend(part.get("call_id") for part in message.get(PARTS_KEY) or []
                         if isinstance(part, dict) and part.get("type") == "tool")
            for call_id in calls:
                if isinstance(call_id, str) and call_id:
                    pending.setdefault(call_id, set()).add(index)
        elif message.get("role") == "tool" and isinstance(call_id := message.get("tool_call_id"), str) and call_id:
            candidates = pending.get(call_id, set())
            run = message.get(INTERACTION_RUN_ID_KEY)
            matches = [i for i in candidates if not run or messages[i].get(INTERACTION_RUN_ID_KEY) == run]
            owners[index] = matches
            if len(matches) == 1:
                candidates.remove(matches[0])
                if not candidates:
                    del pending[call_id]
    return owners


def build_transcript_document(
    session_id: str,
    messages: list[dict],
    *,
    title: str = "",
    tool_details: bool = True,
    thinking: bool = True,
    compact_tools: bool = False,
    open_detail: Callable[[str], tuple[str, str] | None] | None = None,
) -> TranscriptDocument:
    """Build the message-addressable projection used by the terminal surface."""
    heading = title.strip() or f"NZ-Coder session {session_id}"
    header = "\n".join((f"# {heading}", "", f"**Session ID:** {session_id}", "", "---", ""))
    blocks: list[TranscriptBlock] = []
    number_turns = not any(isinstance(item, dict) and is_history_marker(item) for item in messages)
    visible_assistants = None
    tool_owners = {}
    if any(isinstance(item, dict) and _is_turn_user(item) and not _visible_message(item) for item in messages):
        visible_assistants = {i for turn in conversation_turns(messages) for i in turn.assistant_indices}
        tool_owners = _tool_result_owners(messages)
    turn_number = 0
    last_message_id = ""
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        message = public_message_content(message)
        role = str(message.get("role") or "")
        if not _visible_message(message):
            continue
        if visible_assistants is not None:
            if role == "assistant" and index not in visible_assistants:
                continue
            if role == "tool" and not (len(owners := tool_owners.get(index, [])) == 1 and owners[0] in visible_assistants):
                continue
        if role == "user" and not _is_turn_user(message):
            continue
        message_id = str(message.get(MESSAGE_ID_KEY) or f"transcript-{role}-{index}")
        lines: list[str] = []
        navigable = False
        block_turn: int | None = None
        if role == "user":
            turn_number += 1
            block_turn = turn_number if number_turns else None
            navigable = bool(_message_text(message))
            lines.extend(("## User", "", _message_text(message), ""))
        elif role == "assistant":
            navigable = bool(_message_text(message))
            lines.extend(("## Assistant", ""))
            reasoning = "\n".join(
                str(part.get("text") or "").strip()
                for part in message.get(PARTS_KEY, []) or []
                if isinstance(part, dict)
                and part.get("type") == "reasoning"
                and part.get("internal") is not True
                and part.get("visible") is not False
                and str(part.get("text") or "").strip()
            )
            if thinking and reasoning:
                lines.extend(("_Thinking:_", "", reasoning, ""))
            text = _message_text(message)
            if text:
                lines.extend((text, ""))
        elif role == "tool":
            if not tool_details:
                continue
            output = _message_text(message)
            if output:
                lines.extend(("**Output:**", _fenced(output), ""))
            message_id = str(message.get(MESSAGE_ID_KEY) or last_message_id or message_id)
        else:
            continue
        lines.extend(("---", ""))
        blocks.append(TranscriptBlock(
            message_id=message_id,
            role=role,
            markdown="\n".join(lines).rstrip() + "\n",
            turn_number=block_turn,
            navigable=navigable,
        ))
        if role == "assistant":
            blocks.extend(_assistant_tool_blocks(
                message,
                message_id,
                tool_details=tool_details,
                compact_tools=compact_tools,
            ))
        if role in {"user", "assistant"}:
            last_message_id = message_id
    return TranscriptDocument(header=header, blocks=tuple(blocks), open_detail=open_detail)


def _assistant_tool_blocks(
    message: dict, message_id: str, *, tool_details: bool, compact_tools: bool
) -> list[TranscriptBlock]:
    """Project durable ToolParts, falling back to provider tool-call payloads."""
    if not tool_details:
        return []
    parts = {
        str(part.get("call_id")): part
        for part in message.get(PARTS_KEY, []) or []
        if isinstance(part, dict) and part.get("type") == "tool" and part.get("call_id")
    }
    calls = [item for item in message.get("tool_calls", []) or [] if isinstance(item, dict)]
    call_ids: set[str] = set()
    result: list[TranscriptBlock] = []
    for index, call in enumerate(calls):
        call_id = str(call.get("id") or call.get("tool_call_id") or f"index-{index}")
        call_ids.add(call_id)
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        part = parts.get(call_id)
        state = (part or {}).get("state")
        if not isinstance(state, dict):
            state = {}
        result.append(_tool_block(
            message_id,
            part_id=str((part or {}).get("id") or call_id),
            name=str((part or {}).get("tool") or function.get("name") or "tool"),
            arguments=state.get("input", function.get("arguments", {})),
            state=state,
            tool_details=tool_details,
            compact_tools=compact_tools,
        ))
    for call_id, part in parts.items():
        if call_id in call_ids:
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        result.append(_tool_block(
            message_id,
            part_id=str(part.get("id") or call_id),
            name=str(part.get("tool") or "tool"),
            arguments=state.get("input", {}),
            state=state,
            tool_details=tool_details,
            compact_tools=compact_tools,
        ))
    return result


def _tool_block(
    message_id: str,
    *,
    part_id: str,
    name: str,
    arguments,
    state: dict,
    tool_details: bool,
    compact_tools: bool,
) -> TranscriptBlock:
    """Build compact and expanded Markdown for one durable ToolPart."""
    status = str(state.get("status") or "completed")
    interrupted = status == "error" and state.get("interrupted") is True
    display_status = "interrupted" if interrupted else status
    compact = f"**▸ ⚙ {name}** · {display_status}\n\n---\n"
    lines = [f"**Tool: {name}**", ""]
    if tool_details:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        rendered = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        lines.extend(("**Input:**", _fenced(rendered, "json"), ""))
        if status == "completed":
            output = state.get("output")
        elif interrupted:
            output = "Tool execution interrupted."
        else:
            from nz_coder.protocol.public_error import public_error_message

            output = public_error_message(state.get("error"))
        if output:
            lines.extend(("**Output:**", _fenced(str(output)), ""))
    lines.extend(("---", ""))
    return TranscriptBlock(
        message_id=message_id,
        role="toolpart",
        markdown="\n".join(lines).rstrip() + "\n",
        navigable=False,
        part_id=part_id,
        compact_markdown=compact if compact_tools else None,
    )


def session_options(*, limit: int = 20) -> list[SessionOption]:
    """Return bounded Session metadata without constructing Agent owners."""
    try:
        active = active_session_id()
    except SessionReadError:
        active = None
    paths = list_sessions(limit=max(1, min(int(limit), 100)))
    result = []

    def option(session_id, payload, is_active):
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise SessionReadError("corrupt", session_id)
        raw_title = payload.get("title")
        title = raw_title[:160] if isinstance(raw_title, str) else ""
        raw_model = payload.get("model")
        model = raw_model[:160] if isinstance(raw_model, str) and raw_model else "—"
        raw_time = payload.get("timestamp")
        timestamp = raw_time if isinstance(raw_time, str) and len(raw_time) == 19 and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", raw_time) else "—"
        raw_mode = payload.get("mode")
        mode = raw_mode if isinstance(raw_mode, str) and raw_mode in MODES else "—"
        return SessionOption(session_id, title, is_active, timestamp, len(messages), model, mode)

    for path in paths:
        try:
            result.append(option(path.stem, load_session(path.stem), path.stem == active))
        except SessionReadError as exc:
            result.append(SessionOption(path.stem, f"Unavailable: {exc.kind}", False, "—", 0, "—", "—", exc.kind))
    if active and active not in {item.session_id for item in result}:
        try:
            payload = load_session("active")
            if payload:
                result.insert(0, option(active, payload, True))
        except SessionReadError as exc:
            result.insert(0, SessionOption(active, f"Unavailable: {exc.kind}", False, "—", 0, "—", "—", exc.kind))
    return result


def latest_assistant_text(messages: list[dict]) -> str:
    """Return text Parts from the latest Assistant, with legacy content fallback."""
    message = next(
        (
            item for item in reversed(messages)
            if isinstance(item, dict) and item.get("role") == "assistant"
            and item.get("_nz_internal") is not True
            and item.get("_nz_visible") is not False
        ),
        None,
    )
    if message is None:
        return ""
    message = public_message_content(message)
    values = []
    for part in message.get(PARTS_KEY, []) or []:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        if part.get("synthetic") or part.get("ignored"):
            continue
        value = part.get("text")
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return "\n".join(values).strip() or _message_text(message)


def _is_turn_user(message: dict) -> bool:
    return is_user_request_message(message) and not is_history_marker(message)


def _visible_message(message: dict) -> bool:
    return message.get("_nz_visible") is not False and message.get("_nz_internal") is not True


def _recorded_time(owner: dict, key: str, moment: str) -> float | None:
    timing = owner.get(key)
    value = timing.get(moment) if isinstance(timing, dict) else None
    return value if type(value) in (int, float) else None


def _message_text(message: dict) -> str:
    content = public_message_content(message).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(parts).strip()
    if content is None:
        return ""
    # Provider-native structured payloads are durable protocol state, not
    # terminal prose. Serializing them here leaked orphan JSON tails after a
    # resumed Session when the viewport started in the middle of the block.
    return ""


def _turn_diff(message: dict) -> tuple[tuple[str, ...], int, int]:
    summary = message.get(SUMMARY_KEY)
    diffs = summary.get("diffs") if isinstance(summary, dict) else None
    if not isinstance(diffs, list):
        return (), 0, 0
    files = []
    additions = 0
    deletions = 0
    for item in diffs:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        files.append(item["file"])
        additions += max(0, int(item.get("additions") or 0))
        deletions += max(0, int(item.get("deletions") or 0))
    return tuple(files), additions, deletions


def _preview(value: str, limit: int) -> str:
    # 工具或粘贴输入可有许多兆字节。终端列数无法限制组合字符，先限制预览的原文字数。
    value = str(value)
    cap = max(1, limit) * 8
    compact = " ".join(value[:cap].split())
    if len(value) > cap:
        compact += "…"
    return clip_terminal_text(compact, limit)


def _fenced(value: str, language: str = "") -> str:
    longest = max((len(item) for item in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{value}\n{fence}"
