"""按宿主压缩记录读取会话原始转录，并保留当前会话对历史的更新。"""
from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from itertools import chain
from pathlib import Path

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.foundation.workspace_file_access import FixedFileAccess
from nz_coder.protocol.message_schema import COMPACTION_KEY, MESSAGE_ID_KEY, SESSION_ID_KEY
from nz_coder.runtime.session.model import _validate_message
from nz_coder.state.context import _is_compaction_summary, _is_human_user_turn
from nz_coder.state.input_expansion import USER_TEXT_KEY
from nz_coder.state.sessions import _exact_session_id, _valid_persisted_parts, session_dir, session_transcript_dir
from nz_coder.state.workdir import scoped_workdir


_ARCHIVE_BUDGET = 64 * 1024 * 1024
_MAX_ARCHIVES = 32


class HistoryUnavailable(ValueError):
    """历史存在缺口；完整尾轮仍可由持有它的会话对外展示。"""

    def __init__(self, messages: list[dict], complete_from: int, numbers_known: bool):
        super().__init__("Original session history is unavailable.")
        self.messages = messages
        self.complete_from = complete_from
        self.numbers_known = numbers_known


def is_history_marker(message: dict) -> bool:
    return COMPACTION_KEY in message or _is_compaction_summary(message)


def _message_ids(messages: list[dict], session_id: str) -> list[str]:
    ids = [m.get(MESSAGE_ID_KEY) for m in messages]
    if (any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids)
            or any(m.get(SESSION_ID_KEY) != session_id for m in messages)):
        raise ValueError("Invalid transcript message identities")
    return ids


def _split_compacted(messages: list[dict], session_id: str) -> tuple[dict, int]:
    marker = messages[0]
    meta = marker.get(COMPACTION_KEY)
    if marker.get("role") != "user" or not isinstance(meta, dict):
        raise ValueError("Invalid compaction marker")
    head_ids = meta.get("head_message_ids")
    if (not isinstance(head_ids, list) or not head_ids or any(not isinstance(i, str) or not i for i in head_ids)
            or len(set(head_ids)) != len(head_ids) or "tail_start_id" not in meta):
        raise ValueError("Invalid archived head identities")
    ids = _message_ids(messages, session_id)
    head_set = set(head_ids)
    boundary = meta["tail_start_id"]
    if boundary is None:
        cut = next((i for i in range(1, len(ids))
                    if ids[i] not in head_set or not _is_human_user_turn(messages[i])), len(ids))
    elif isinstance(boundary, str) and boundary in ids[1:] and boundary not in head_set:
        cut = ids.index(boundary, 1)
    else:
        raise ValueError("Missing compaction boundary")
    if any(not _is_human_user_turn(m) for m in messages[1:cut]) or not head_set.issuperset(ids[1:cut]):
        raise ValueError("Invalid retained user copies")
    # 冻结请求会把落出尾部的当前助手重新附在候选末尾；原位置和追加位置读取归档后再核对。
    overlapping = [m for m in messages[cut:] if m[MESSAGE_ID_KEY] in head_set]
    if overlapping and (len(overlapping) != 1 or overlapping[0][MESSAGE_ID_KEY] != head_ids[-1]
                        or overlapping[0].get("role") != "assistant"):
        raise ValueError("Invalid retained assistant")
    return meta, cut


def _read_archive(handle: str, session_id: str, budget: int) -> tuple[list[dict], int]:
    prefix = f"user-state://transcripts/{session_id}/"
    name = handle.removeprefix(prefix)
    if not handle.startswith(prefix) or not re.fullmatch(r"transcript_[0-9]+\.jsonl", name):
        raise ValueError("Invalid transcript archive handle")
    root = session_dir()
    relative = (session_transcript_dir(session_id) / name).relative_to(root).as_posix()
    data = FixedFileAccess(root, (relative,)).read_bytes(relative, maximum=budget)
    if not data.endswith(b"\n"):
        raise ValueError("Incomplete transcript archive")
    records = []
    for line in io.BytesIO(data):
        record = json.loads(line.decode("utf-8"), parse_constant=reject_nonstandard_json_constant)
        _validate_message(record)
        if record.get(SESSION_ID_KEY) != session_id or not _valid_persisted_parts(record):
            raise ValueError("Invalid archived message")
        records.append(record)
    return records, len(data)


def _join_archive(meta, archived, copies, live_tail, session_id, same_original_source):
    head_ids = meta["head_message_ids"]
    ids = _message_ids(archived, session_id)
    if ids[:len(head_ids)] != head_ids:
        raise ValueError("Archive does not match its recorded head")
    head, stored_tail = archived[:len(head_ids)], archived[len(head_ids):]
    boundary = stored_tail[0][MESSAGE_ID_KEY] if stored_tail else None
    if meta["tail_start_id"] != boundary:
        raise ValueError("Archive does not match its recorded tail")

    stored_by_id = {m[MESSAGE_ID_KEY]: m for m in stored_tail}
    retained_index = next((i for i, m in enumerate(live_tail) if m[MESSAGE_ID_KEY] == head_ids[-1]), None)
    if retained_index is not None:
        owner = live_tail[retained_index]
        if (head[-1].get("role") != "assistant"
                or any(m[MESSAGE_ID_KEY] not in stored_by_id for m in live_tail[:retained_index])
                or any(m[MESSAGE_ID_KEY] in stored_by_id for m in live_tail[retained_index + 1:])):
            raise ValueError("Retained assistant does not match its archived position")
        head[-1] = owner
        live_tail = live_tail[:retained_index] + live_tail[retained_index + 1:]

    # 现代格式只复制真实用户，旧格式只保留摘要和尾部。副本的附件展示或文件变更统计可随会话更新。
    copies_by_id = {m[MESSAGE_ID_KEY]: m for m in copies}
    originals = [m for m in head if m[MESSAGE_ID_KEY] in copies_by_id]
    for old, live in zip(originals, copies):
        if (old[MESSAGE_ID_KEY] != live[MESSAGE_ID_KEY] or not _is_human_user_turn(old)
                or old.get(USER_TEXT_KEY, old.get("content")) != live.get(USER_TEXT_KEY, live.get("content"))):
            raise ValueError("Retained user source disagrees with archive")

    if same_original_source is not None:
        # 调用者要复原当时的授权时，旧尾的完整前缀及来源必须一致，附件元数据取其提交时的版本。
        if len(stored_tail) > len(live_tail):
            raise ValueError("Original sources are incomplete")
        for old, live in chain(zip(originals, copies), zip(stored_tail, live_tail)):
            if old[MESSAGE_ID_KEY] != live[MESSAGE_ID_KEY] or not same_original_source(old, live):
                raise ValueError("Original sources disagree with current history")
        return archived + live_tail[len(stored_tail):]

    if boundary is None and "operation_id" in meta:
        # 原来整段都被压缩，现在又少了用户副本，无法区分撤回和脱落；此时没有原尾接点可作证。
        if any(_is_human_user_turn(m) and m[MESSAGE_ID_KEY] not in copies_by_id for m in head):
            raise ValueError("Missing retained user at an unanchored boundary")

    # 撤回后仍保留的记录应有相同的相对次序。被撤回的旧尾绝不能从压缩前的快照补回来。
    matching = [m for m in live_tail if m[MESSAGE_ID_KEY] in stored_by_id]
    kept_ids = {m[MESSAGE_ID_KEY] for m in matching}
    if [i for i in stored_by_id if i in kept_ids] != [m[MESSAGE_ID_KEY] for m in matching]:
        raise ValueError("Archive tail order disagrees with current history")
    if any(m.get("role") != stored_by_id[m[MESSAGE_ID_KEY]].get("role") for m in matching):
        raise ValueError("Archive tail roles disagree with current history")

    return [copies_by_id.get(m[MESSAGE_ID_KEY], m) for m in head] + live_tail


def restore_session_history(
    messages: list[dict], *, session_id: str, workspace: str | Path,
    start_in_tail: Callable[[list[dict]], int | None] | None = None,
    same_original_source: Callable[[dict, dict], bool] | None = None,
) -> list[dict]:
    """返回只读的原始消息序列。恢复旧授权时，调用方可指定任务边界和原文核对规则。"""
    fallback = ([], 0, False)
    remaining = _ARCHIVE_BUDGET
    seen: set[str] = set()
    try:
        while True:
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise ValueError("Invalid session transcript")
            markers = [i for i, m in enumerate(messages) if is_history_marker(m)]
            if not markers:
                return messages
            if markers != [0] or not workspace:
                raise ValueError("Invalid compaction ancestry")
            meta, cut = _split_compacted(messages, session_id)
            copies, tail = messages[1:cut], messages[cut:]
            # 尾部可从助手消息中间开始，只有接点以后的真实提问才保证拥有整轮记录。
            first = next((i for i in range(cut, len(messages)) if _is_human_user_turn(messages[i])), len(messages))
            # 纯用户消息构成的 head 可以直接数清。任一未知 ID 都可能是旧用户或更早一层摘要。
            numbers_known = meta["head_message_ids"] == [m[MESSAGE_ID_KEY] for m in copies]
            fallback = (messages, first, numbers_known)
            if start_in_tail is not None and (start := start_in_tail(tail)) is not None:
                return tail[start:]
            handle = meta.get("archive")
            if not isinstance(handle, str) or handle in seen or len(seen) >= _MAX_ARCHIVES:
                raise ValueError("Invalid or excessive archive chain")
            seen.add(handle)
            with scoped_workdir(workspace):
                _exact_session_id(session_id)
                archived, size = _read_archive(handle, session_id, remaining)
            remaining -= size
            messages = _join_archive(meta, archived, copies, tail, session_id, same_original_source)
    except (OSError, ValueError, RecursionError) as exc:
        raise HistoryUnavailable(*fallback) from exc
