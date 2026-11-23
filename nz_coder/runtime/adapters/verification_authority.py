"""只用宿主保存的用户原文重建旧验收，摘要正文不参与授权。"""
from __future__ import annotations

from nz_coder.protocol.message_schema import (
    COMPACTION_KEY, MESSAGE_ID_KEY, SESSION_ID_KEY, is_user_request_message,
)
from nz_coder.runtime.core.execution_context import explicit_replacement_task, is_genuine_runtime_followup
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.history import HistoryUnavailable, is_history_marker, restore_session_history
from nz_coder.runtime.verification.domain.checkpoint import AuthoritySourceUnavailable
from nz_coder.state.input_expansion import (
    INPUT_EXPANSIONS_KEY, USER_TEXT_KEY, render_attachment_list, render_expanded_message, user_instruction_text,
)
from nz_coder.state.sessions import load_session


def _raw_user(message):
    """附件的展开正文可以包含命令；只有用户亲自提交的文字可恢复验收。"""
    if not is_user_request_message(message):
        return None
    text = user_instruction_text(message)
    if not isinstance(text, str) or text.startswith("<oversized-user-input>"):
        raise AuthoritySourceUnavailable()
    source = {**message, "content": text} if USER_TEXT_KEY in message else message
    return source if is_genuine_runtime_followup(source) else None


def _matches_initial(source, initial):
    replacement = explicit_replacement_task(source)
    if (replacement or source["content"].strip()) == initial:
        return True
    if USER_TEXT_KEY not in source or not isinstance(source.get(INPUT_EXPANSIONS_KEY), list):
        return False
    # 早期状态用展开后的正文标记首轮。附件只能用来辨认该条消息，不能提供任务指令。
    expanded = dict(source)
    render_expanded_message(expanded)
    stored = explicit_replacement_task(expanded) if replacement else expanded["content"].strip()
    if stored == initial:
        return True
    records = source[INPUT_EXPANSIONS_KEY]
    if not initial.startswith("<attached-files>\n") or not records:
        return False
    # 终端先提交名称及字节数，稍后才惰性展开。只核对提交当时的元数据，不读现在的文件。
    files = []
    for record in records:
        if not isinstance(record, dict) or record.get("kind") not in ("file", "image", "document"):
            return False
        name, size = record.get("source"), record.get("originalBytes")
        if not isinstance(name, str) or not name or type(size) is not int or size < 0:
            return False
        files.append((name, size))
    return render_attachment_list(source["content"], files).strip() == initial


def _same_source(left, right):
    if left.get("role") != right.get("role") or is_history_marker(left) != is_history_marker(right):
        return False
    if is_history_marker(left):
        return left.get(COMPACTION_KEY) == right.get(COMPACTION_KEY)
    a, b = _raw_user(left), _raw_user(right)
    return (a is None and b is None) or (a is not None and b is not None and a["content"] == b["content"])


def _task_start_in_tail(tail, initial):
    # 真实用户在完整尾部明确开启了本任务，更早任务的归档与本次授权无关。
    for index in range(len(tail) - 1, -1, -1):
        source = _raw_user(tail[index])
        if explicit_replacement_task(source) is not None:
            return index if _matches_initial(source, initial) else None
    return None


def _current_task_users(state, messages, session_id, workspace):
    initial = state.initial_task_text.strip()
    if messages is None:
        with scoped_workdir(workspace):
            messages = load_session(session_id).get("messages", [])
    try:
        messages = restore_session_history(
            messages, session_id=session_id, workspace=workspace,
            start_in_tail=lambda tail: _task_start_in_tail(tail, initial), same_original_source=_same_source,
        )
    except HistoryUnavailable as exc:
        raise AuthoritySourceUnavailable() from exc
    users = []
    for message in reversed(messages):
        source = _raw_user(message)
        if source is None:
            continue
        if source.get(SESSION_ID_KEY, session_id) != session_id:
            raise AuthoritySourceUnavailable()
        users.append(source)
        if explicit_replacement_task(source) is not None:
            if not _matches_initial(source, initial):
                raise AuthoritySourceUnavailable()
            break
    users.reverse()
    return users


def _user_ids(users):
    ids = [m.get(MESSAGE_ID_KEY) for m in users]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise AuthoritySourceUnavailable()
    return ids


def recover_user_updates(state, previous, messages, *, session_id, workspace):
    """把两次授权身份之间的真实追加指令交回同一任务；已开始的新任务会另建验收。"""
    users = _current_task_users(state, messages, session_id, workspace)
    ids = _user_ids(users)
    wanted = state.task_authority_message_id
    if not wanted or wanted not in ids:
        raise AuthoritySourceUnavailable()
    end = ids.index(wanted) + 1
    count = state.task_authority_epoch - previous.authority_revision
    if previous.authority_message_id:
        if previous.authority_message_id not in ids[:end - 1]:
            raise AuthoritySourceUnavailable()
        start = ids.index(previous.authority_message_id) + 1
    elif explicit_replacement_task(users[0]) is not None:
        # 即使两个任务正文相同，也不能把替换前的旧合同继承到新任务。
        raise AuthoritySourceUnavailable()
    elif count == 1:
        start = end - 1
    else:
        starts = [i for i, m in enumerate(users[:end]) if _matches_initial(m, state.initial_task_text.strip())]
        if len(starts) != 1:
            raise AuthoritySourceUnavailable()
        start = starts[0] + 1
    if end - start < count:
        raise AuthoritySourceUnavailable()
    return users[start:end]


def recover_user_contract(state, messages, *, session_id, workspace):
    from nz_coder.runtime.adapters.context import new_runtime_state
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    initial = state.initial_task_text.strip()
    users = _current_task_users(state, messages, session_id, workspace)
    starts = [i for i, m in enumerate(users) if _matches_initial(m, initial)]
    if len(starts) != 1:
        raise AuthoritySourceUnavailable()
    users = users[starts[0]:]
    identities = _user_ids(users)
    required_ids = {ref.get("source_message_id") for ref in state.task_reference_evidence if ref.get("source_message_id")}
    if ((state.task_authority_message_id and state.task_authority_message_id not in identities)
            or not required_ids.issubset(identities) or len(users) < max(1, state.task_authority_epoch)):
        raise AuthoritySourceUnavailable()
    rebuilt = new_runtime_state()
    rebuilt.initial_task_text = explicit_replacement_task(users[0]) or users[0]["content"]
    rebuilt.set_acceptance_criteria_from_text(rebuilt.initial_task_text, workspace=workspace)
    for message in users[1:]:
        rebuilt.apply_current_round_instruction(message["content"], workspace=workspace,
            source_message_id=message[MESSAGE_ID_KEY], capture_task_references=False)
    return parse_task_contract(rebuilt.task_contract, workspace=workspace)
