"""Read-only terminal projections for conversation turns and saved Sessions."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import re

from rich.table import Table

from nz_coder.interface.presentation_tokens import clip_terminal_text
from nz_coder.protocol.message_schema import (
    ASSISTANT_PARENT_KEY,
    MESSAGE_ID_KEY,
    PARTS_KEY,
    SUMMARY_KEY,
    is_synthetic_user_message,
    rebind_fork_history as rebind_fork_history,
)
from nz_coder.state.sessions import active_session_id, list_sessions, load_session


@dataclass(frozen=True)
class ConversationTurn:
    """One visible user turn and its following Agent messages."""

    number: int
    start: int
    end: int
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


def conversation_turns(messages: list[dict]) -> list[ConversationTurn]:
    """Group history at real user prompts while hiding injected diagnostics."""
    starts = [
        index for index, message in enumerate(messages)
        if isinstance(message, dict)
        and message.get("role") == "user"
        and not _is_synthetic_user(message)
    ]
    real_user_ids = {
        messages[index].get(MESSAGE_ID_KEY)
        for index in starts
        if isinstance(messages[index].get(MESSAGE_ID_KEY), str)
    }
    turns: list[ConversationTurn] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(messages)
        user_text = _message_text(messages[start])
        assistant_text = ""
        tools: list[str] = []
        changed_files, additions, deletions = _turn_diff(messages[start])
        user_id = messages[start].get(MESSAGE_ID_KEY)
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                if (
                    message.get("_nz_internal") is True
                    or message.get("_nz_visible") is False
                ):
                    continue
                parent_id = message.get(ASSISTANT_PARENT_KEY)
                graph_owned = (
                    isinstance(user_id, str)
                    and isinstance(parent_id, str)
                    and parent_id == user_id
                )
                positional = start < index < end and parent_id not in real_user_ids
                if not graph_owned and not positional:
                    continue
                text = _message_text(message)
                if text:
                    assistant_text = text
                for call in message.get("tool_calls", []) or []:
                    if not isinstance(call, dict):
                        continue
                    name = str(call.get("function", {}).get("name") or "").strip()
                    if name and name not in tools:
                        tools.append(name)
                for part in message.get(PARTS_KEY, []) or []:
                    if not isinstance(part, dict) or part.get("type") != "tool":
                        continue
                    name = str(part.get("tool") or "").strip()
                    if name and name not in tools:
                        tools.append(name)
        turns.append(ConversationTurn(
            number=position + 1,
            start=start,
            end=end,
            user_text=user_text,
            assistant_text=assistant_text,
            tools=tuple(tools),
            changed_files=changed_files,
            additions=additions,
            deletions=deletions,
        ))
    return turns


def fork_history(messages: list[dict], turn_number: int) -> list[dict]:
    """Copy history through one complete visible user turn."""
    turns = conversation_turns(messages)
    if not turns:
        raise ValueError("No user turns are available to fork")
    if turn_number < 1 or turn_number > len(turns):
        raise ValueError(f"Turn must be between 1 and {len(turns)}")
    return copy.deepcopy(messages[:turns[turn_number - 1].end])


def forked_session_title(title: str) -> str:
    """Return InfCode's monotonically numbered title for a Session fork."""
    match = re.match(r"^(.+) \(fork #(\d+)\)$", str(title))
    if match:
        return f"{match.group(1)} (fork #{int(match.group(2)) + 1})"
    return f"{title} (fork #1)"


def render_timeline(messages: list[dict], *, limit: int = 20) -> Table:
    """Build a bounded turn table suitable for Rich and plain capture."""
    turns = conversation_turns(messages)[-max(1, min(int(limit), 100)):]
    table = Table(title="Session timeline", show_lines=True, expand=True)
    table.add_column("Turn", justify="right", style="cyan", no_wrap=True)
    table.add_column("User")
    table.add_column("Agent")
    table.add_column("Tools", style="yellow")
    table.add_column("Changes", style="green", no_wrap=True)
    for turn in turns:
        table.add_row(
            str(turn.number),
            _preview(turn.user_text, 240) or "(empty)",
            _preview(turn.assistant_text, 240) or "(no final text)",
            ", ".join(turn.tools) or "—",
            (
                f"{len(turn.changed_files)} files +{turn.additions}/-{turn.deletions}"
                if turn.changed_files else "—"
            ),
        )
    if not turns:
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


def build_transcript_document(
    session_id: str,
    messages: list[dict],
    *,
    title: str = "",
    tool_details: bool = True,
    thinking: bool = True,
    compact_tools: bool = False,
) -> TranscriptDocument:
    """Build the message-addressable projection used by the terminal surface."""
    heading = title.strip() or f"NZ-Coder session {session_id}"
    header = "\n".join((f"# {heading}", "", f"**Session ID:** {session_id}", "", "---", ""))
    blocks: list[TranscriptBlock] = []
    turn_number = 0
    last_message_id = ""
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "assistant" and (
            message.get("_nz_internal") is True
            or message.get("_nz_visible") is False
        ):
            continue
        if role == "user" and is_synthetic_user_message(message):
            continue
        message_id = str(message.get(MESSAGE_ID_KEY) or f"transcript-{role}-{index}")
        lines: list[str] = []
        navigable = False
        block_turn: int | None = None
        if role == "user":
            turn_number += 1
            block_turn = turn_number
            navigable = bool(_message_text(message))
            lines.extend(("## User", "", _message_text(message), ""))
        elif role == "assistant":
            navigable = bool(_message_text(message))
            lines.extend(("## Assistant", ""))
            reasoning = str(message.get("reasoning_content") or "").strip()
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
    return TranscriptDocument(header=header, blocks=tuple(blocks))


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
    compact = f"**▸ ⚙ {name}** · {status}\n\n---\n"
    lines = [f"**Tool: {name}**", ""]
    if tool_details:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        rendered = json.dumps(arguments, ensure_ascii=False, indent=2, default=str)
        lines.extend(("**Input:**", _fenced(rendered, "json"), ""))
        output = state.get("output") if status == "completed" else state.get("error")
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
    active = active_session_id()
    paths = list_sessions(limit=max(1, min(int(limit), 100)))
    payloads = [(path.stem, load_session(path.stem)) for path in paths]
    if active and active not in {session_id for session_id, _payload in payloads}:
        payload = load_session("active")
        if payload:
            payloads.insert(0, (active, payload))
    return [
        SessionOption(
            session_id=session_id,
            title=str(payload.get("title") or ""),
            active=session_id == active,
            timestamp=str(payload.get("timestamp") or "—"),
            message_count=len(payload.get("messages", [])),
            model=str(payload.get("model") or "—"),
            mode=str(payload.get("mode") or "—"),
        )
        for session_id, payload in payloads
    ]


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


def _is_synthetic_user(message: dict) -> bool:
    return is_synthetic_user_message(message)


def _message_text(message: dict) -> str:
    content = message.get("content", "")
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
    compact = " ".join(str(value).split())
    return clip_terminal_text(compact, limit)


def _fenced(value: str, language: str = "") -> str:
    longest = max((len(item) for item in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{value}\n{fence}"
