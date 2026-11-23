"""在宿主真实写出的归档上检查历史查询和不完整历史的边界。"""
import copy
import json

import pytest

from nz_coder.protocol.message_schema import COMPACTION_KEY, MESSAGE_ID_KEY, MODEL_NOTE_KEY, ensure_message_identities
from nz_coder.runtime.session.history import HistoryUnavailable, is_history_marker, restore_session_history
from nz_coder.state import sessions
from tests.runtime.test_legacy_authority_recovery import SESSION, _compact, _turns, workspace as workspace


def _archive_path(messages):
    handle = messages[0][COMPACTION_KEY]["archive"]
    return sessions.session_transcript_dir(SESSION) / handle.rsplit("/", 1)[-1]


def test_nested_archive_restores_model_note_and_preserves_live_user_and_settled_reply(workspace):
    original = _turns("Same prompt", "Same prompt", "Third", "Fourth")
    original[1][MODEL_NOTE_KEY] = "First turn original note."
    first = _compact(original)
    compacted = _compact(first + _turns("Fifth", "Sixth"))
    compacted[1]["_nz_summary"] = {"diffs": [{"file": "updated.txt"}]}
    compacted[-1].update({"content": "Settled sixth answer", MODEL_NOTE_KEY: "Latest note", "_nz_visible": False})
    before = copy.deepcopy(compacted)

    restored = restore_session_history(compacted, session_id=SESSION, workspace=workspace)

    assert len(restored) == 12 and len({m[MESSAGE_ID_KEY] for m in restored}) == 12
    assert restored[:4] == [compacted[1], original[1], original[2], original[3]]
    assert restored[-1] == compacted[-1]
    assert compacted == before


def test_history_does_not_reinstate_withdrawn_tail_when_a_new_turn_is_appended(workspace):
    original = _turns("First", "Second", "Third", "Withdrawn fourth")
    compacted = _compact(original)
    del compacted[-2:]
    compacted.extend(_turns("Replacement fourth"))
    ensure_message_identities(compacted, SESSION)

    restored = restore_session_history(compacted, session_id=SESSION, workspace=workspace)
    assert restored == original[:-2] + compacted[-2:]


def test_missing_inner_archive_keeps_proven_recent_turns_after_successful_outer_restore(workspace):
    original = _turns("First", "Second", "Third", "Fourth")
    compacted = _compact(original)
    nested = _compact(compacted + _turns("Fifth", "Sixth"))
    _archive_path(compacted).unlink()
    with pytest.raises(HistoryUnavailable) as error:
        restore_session_history(nested, session_id=SESSION, workspace=workspace)
    failure = error.value
    assert failure.messages[failure.complete_from][MESSAGE_ID_KEY] == original[4][MESSAGE_ID_KEY]
    assert failure.messages[-2]["content"] == "Sixth"
    assert not failure.numbers_known


def test_incomplete_turn_split_before_its_assistant_is_outside_the_safe_suffix(workspace):
    original = _turns("First", "large input " * 13000)
    compacted = _compact(original)
    assert compacted[0][COMPACTION_KEY]["tail_start_id"] == original[-1][MESSAGE_ID_KEY]
    compacted.extend(_turns("Complete recent turn"))
    ensure_message_identities(compacted, SESSION)
    _archive_path(compacted).unlink()

    with pytest.raises(HistoryUnavailable) as error:
        restore_session_history(compacted, session_id=SESSION, workspace=workspace)
    failure = error.value
    assert failure.messages[failure.complete_from:] == compacted[-2:]
    assert not failure.numbers_known


def test_legacy_summary_without_copied_users_requires_its_original_workspace(workspace):
    original = _turns("First", "Second", "Third", "Fourth")
    compacted = _compact(original)
    # 早期压缩只保留摘要和尾部，首轮的身份与正文只能来自归档。
    marker = compacted[0]
    del marker[COMPACTION_KEY]["operation_id"]
    legacy = [marker, *original[4:]]
    assert restore_session_history(legacy, session_id=SESSION, workspace=workspace) == original

    with pytest.raises(HistoryUnavailable) as error:
        restore_session_history(legacy, session_id=SESSION, workspace=workspace.parent)
    assert not error.value.numbers_known
    assert error.value.messages[error.value.complete_from:] == original[4:]


def test_missing_user_copy_without_a_surviving_anchor_does_not_revive_a_withdrawn_turn(workspace):
    original = _turns("First")
    original[-1]["content"] = "long reply " * 13000
    compacted = _compact(original)
    assert compacted[0][COMPACTION_KEY]["tail_start_id"] is None
    current = compacted[:1] + _turns("New turn")
    ensure_message_identities(current, SESSION)
    with pytest.raises(HistoryUnavailable) as error:
        restore_session_history(current, session_id=SESSION, workspace=workspace)
    assert error.value.messages[error.value.complete_from:] == current[-2:]


def test_archive_rejects_a_part_linked_to_a_different_message(workspace):
    compacted = _compact(_turns("First", "Second", "Third"))
    path = _archive_path(compacted)
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    rows[1]["_nz_parts"][0]["message_id"] = rows[0][MESSAGE_ID_KEY]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(HistoryUnavailable):
        restore_session_history(compacted, session_id=SESSION, workspace=workspace)


def test_active_processor_reattached_after_compaction_keeps_its_settled_version_once(workspace):
    from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot

    original = _turns("First")
    owner = original[-1]
    owner["content"] = "large active request " * 10000
    compacted = _compact(original)
    assert compacted[0][COMPACTION_KEY]["tail_start_id"] is None
    snapshot = FrozenRequestSnapshot("", "", "", "", lambda messages: messages, (), retained=(owner,))
    compacted = snapshot.restore_retained(compacted)
    owner.update(content="Settled after compaction", _nz_visible=False)
    compacted.extend(_turns("Another turn"))
    ensure_message_identities(compacted, SESSION)

    restored = restore_session_history(compacted, session_id=SESSION, workspace=workspace)
    assert restored == [original[0], owner, *compacted[-2:]]


def test_submitted_literal_summary_wrapper_is_a_user_turn_before_and_after_compaction(workspace):
    from nz_coder.interface.submission import build_user_submission

    text = "<session-summary>A real submitted XML example</session-summary>"
    user = build_user_submission(text, (), workspace=workspace, session_id=SESSION, agent="build",
                                 provider_id="offline", model_id="offline")
    assert not is_history_marker(user)
    original = [user, {"role": "assistant", "content": "I can see the example."}, *_turns("Second", "Third")]
    assert restore_session_history(original, session_id=SESSION, workspace=workspace) == original
    compacted = _compact(original)
    assert compacted[1][MESSAGE_ID_KEY] == user[MESSAGE_ID_KEY]
    assert restore_session_history(compacted, session_id=SESSION, workspace=workspace) == original
    assert is_history_marker({"role": "user", "content": text})
