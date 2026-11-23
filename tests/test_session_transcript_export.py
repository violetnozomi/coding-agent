"""通过本地命令和回环服务验证公开会话导出。"""
import builtins
import copy
from io import StringIO

import pytest
from rich.console import Console

from nz_coder.http_service import NZCoderClient
from nz_coder.interface.commands import build_default_registry
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.interface.remote import attach_main
from nz_coder.interface.submission import build_user_submission
from nz_coder.protocol.message_schema import COMPACTION_KEY, DEFERRED_MODEL_TEXT_KEY, ensure_message_identities
from nz_coder.state import context, sessions
from tests.fixtures.compaction_summary import summary_json
from tests.test_context_budget import _CompactionClient
from tests.test_http_service import local_service as local_service


def _compact(rows, sid):
    with sessions.scoped_session(sid):
        ensure_message_identities(rows, sid)
        result = context.auto_compact(rows, _CompactionClient(summary_json("private continuity summary")), "offline")
        ensure_message_identities(result, sid)
        archive = sessions.session_transcript_dir(sid) / result[0][COMPACTION_KEY]["archive"].rsplit("/", 1)[-1]
    return result, archive


def _session(service):
    client = NZCoderClient(service.base_url, service.token)
    sid = client.create_session()["id"]
    return client, service.manager.get(sid)


def _turns(start, end):
    rows = []
    for number in range(start, end + 1):
        rows.extend([{"role": "user", "content": f"Question {number}."},
                     {"role": "assistant", "content": f"Receipt {number}.", "_nz_model_note": "private model draft"}])
    return rows


def _terminal_export(session, path, monkeypatch):
    stream = StringIO()
    ctx = CommandContext(session.history, {"id": session.session_id}, "", None,
                         Console(file=stream, force_terminal=False), None)
    clipboard = []
    monkeypatch.setattr("nz_coder.interface.clipboard._write_osc52", lambda data: clipboard.append(data.decode()) or True)
    monkeypatch.setattr("nz_coder.interface.clipboard._native_copy_commands", lambda: ())
    registry = build_default_registry()
    assert registry.dispatch(f"/export {path}", ctx)
    assert registry.dispatch("/copy", ctx)
    return clipboard[0], stream.getvalue()


def test_complete_export_uses_archived_replies_in_original_order(local_service, monkeypatch):
    client, session = _session(local_service)
    rows, _ = _compact(_turns(1, 5), session.session_id)
    session.history[:] = rows
    original = copy.deepcopy(rows)
    path = session.workspace / "transcript.md"
    copied, _ = _terminal_export(session, path, monkeypatch)
    remote = client.export_session(session.session_id)
    for rendered in (copied, path.read_text(), remote["markdown"]):
        assert [line for line in rendered.splitlines() if line.startswith("Receipt ")] == [
            "Receipt 1.", "Receipt 2.", "Receipt 3.", "Receipt 4.", "Receipt 5.",
        ]
        assert "private" not in rendered
    assert not remote.get("notice") and session.history == original


@pytest.mark.parametrize("damage", ["missing", "oversize"])
def test_partial_export_recovers_known_outer_archive_and_preserves_previous_file(local_service, monkeypatch, damage):
    client, session = _session(local_service)
    sid = session.session_id
    first, archive = _compact(_turns(1, 5), sid)
    rows, _ = _compact(first + _turns(6, 7), sid)
    session.history[:] = rows
    if damage == "missing":
        archive.unlink()
    else:
        with archive.open("ab") as file:
            file.truncate(64 * 1024 * 1024 + 1)
    payload = client.export_session(sid)
    assert payload.get("notice") and payload["notice"] in payload["markdown"]
    path = session.workspace / "partial.md"
    copied, local_status = _terminal_export(session, path, monkeypatch)
    assert path.read_text() == copied and payload["notice"] in copied and "partial" in local_status.lower()
    for rendered in (copied, payload["markdown"]):
        assert "Question 1." in rendered and "Receipt 1." not in rendered
        assert [line for line in rendered.splitlines() if line.startswith("Receipt ")] == [
            "Receipt 4.", "Receipt 5.", "Receipt 6.", "Receipt 7.",
        ]
        assert "private" not in rendered
    old = session.workspace / "previous.md"
    old.write_text("Previous complete export.")
    _terminal_export(session, old, monkeypatch)
    assert old.read_text() == "Previous complete export."
    attached = session.workspace / "attached.md"
    token = session.workspace / "token"
    token.write_text(local_service.token)
    commands = iter([f"/export {old}", f"/export {attached}", "/exit"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(commands))
    screen = StringIO()
    assert attach_main([sid, "--url", local_service.base_url, "--token-file", str(token)],
                       console=Console(file=screen, force_terminal=False)) == 0
    assert attached.read_text() == payload["markdown"] and "partial" in screen.getvalue().lower()
    assert old.read_text() == "Previous complete export."


def test_export_filters_user_sources_and_descendants_before_http_projection(local_service, monkeypatch):
    client, session = _session(local_service)
    sid = session.session_id
    literal = "<session-summary>Submitted example</session-summary>"
    user = build_user_submission(literal, (), workspace=session.workspace, session_id=sid, agent="build",
                                 provider_id="offline", model_id="offline")
    session.history[:] = [
        user, {"role": "user", "content": "HIDDEN expanded body", "_nz_user_text": "Typed attachment request."},
        {"role": "user", "content": "HIDDEN orphan expansion", "_nz_input_expansions": [{"kind": "file"}]},
        {"role": "user", "content": "HIDDEN runtime instruction", "_nz_synthetic": True},
        {"role": "user", "content": "HIDDEN revoked prompt", "_nz_visible": False, "_nz_message_id": "msg-hidden"},
        {"role": "assistant", "content": "HIDDEN positional reply"},
        {"role": "user", "content": "Current public prompt.", "_nz_message_id": "msg-current"},
        {"role": "assistant", "content": "HIDDEN deferred draft", DEFERRED_MODEL_TEXT_KEY: True},
        {"role": "assistant", "content": "HIDDEN late reply", "_nz_parent_id": "msg-hidden",
         "tool_calls": [{"id": "late", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "late", "content": "HIDDEN late tool receipt"},
        {"role": "assistant", "content": "Public final.", "_nz_model_note": "HIDDEN model note",
         "tool_calls": [{"id": "masked", "function": {"name": "HIDDEN masked tool", "arguments": "{}"}}],
         "_nz_parts": [{"id": "part-private", "type": "tool", "call_id": "masked", "visible": False,
                        "state": {"status": "completed", "output": "HIDDEN masked result"}}]},
        {"role": "tool", "tool_call_id": "masked", "content": "HIDDEN masked tool receipt"},
    ]
    original = copy.deepcopy(session.history)
    copied, _ = _terminal_export(session, session.workspace / "visible.md", monkeypatch)
    for rendered in (copied, client.export_session(sid)["markdown"]):
        assert literal in rendered and "Typed attachment request." in rendered and "Public final." in rendered
        assert "HIDDEN" not in rendered
    assert original == session.history
