"""工作笔记的完整保存、展示与新输入身份行为。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import scoped_session
from nz_coder.tools.scratchpad import Scratchpad


def test_long_note_is_saved_in_full_and_survives_reopen(tmp_path):
    with scoped_workdir(tmp_path), scoped_session("long-note"):
        note = "Investigation evidence. " * 40 + "CANCEL_AFTER_COMMIT_BEFORE_ACK"
        assert not Scratchpad().update_section('focus', [{"summary": note, "details": ""}]).startswith("Error:")
        assert note in Scratchpad().read()


def test_later_failure_notes_do_not_evict_investigation_findings(tmp_path):
    with scoped_workdir(tmp_path), scoped_session("many-notes"):
        pad = Scratchpad()
        facts = [f"Key finding {i}: checkpoint follows fsync and rename." for i in range(12)]
        for fact in facts:
            pad.update_section('focus', [{"summary": fact, "details": ""}])
        for i in range(25):
            pad.update_section('lessons', [{"summary": f"Attempt {i}: cancellation race investigated.", "details": ""}])
        restored = Scratchpad().read()
        assert all(fact in restored for fact in facts)
        assert sum(map(len, Scratchpad().memory.values())) == 37


def test_short_findings_are_all_in_prompt(tmp_path):
    with scoped_workdir(tmp_path), scoped_session("visible-notes"):
        pad = Scratchpad()
        facts = [f"Finding {i}: preserve parser ownership." for i in range(12)]
        for fact in facts:
            pad.update_section('focus', [{"summary": fact, "details": ""}])
        assert all(fact in pad.build_prompt_block() for fact in facts)


def test_summary_view_points_to_full_details_without_truncation(tmp_path):
    with scoped_workdir(tmp_path), scoped_session("bounded-view"):
        pad = Scratchpad()
        note = "Evidence and reproduction details. " * 150 + "EXACT_FINAL_CONDITION"
        pad.update_section("focus", [{"summary": "IMPORTANT_SUMMARY", "details": note}])
        view = pad.build_prompt_block()
        assert "IMPORTANT_SUMMARY" in view
        assert note not in view
        assert "read_scratchpad" in view
        assert note in pad.read()


@pytest.mark.parametrize("content", ["继续", [{"type": "text", "text": "继续"}]])
def test_identical_new_submissions_get_different_user_message_ids(tmp_path, content):
    from nz_coder.runtime.session.runtime import SessionRuntime
    from nz_coder.runtime.session.store import EphemeralSessionStore
    from test_native_product_runtime import _request

    request = replace(_request(tmp_path), messages=({"role": "user", "content": content},))

    async def scenario():
        first = await SessionRuntime(EphemeralSessionStore()).open(request)
        second = await SessionRuntime(EphemeralSessionStore()).open(request)
        assert first.transcript[0]["_nz_message_id"] != second.transcript[0]["_nz_message_id"]
        assert "_nz_message_id" not in request.messages[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("content", ["继续", [{"type": "text", "text": "继续"}]])
def test_new_submission_appends_to_existing_session(tmp_path, content):
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.session.runtime import SessionRuntime
    from nz_coder.runtime.session.store import EphemeralSessionStore
    from test_native_product_runtime import _request

    request = replace(_request(tmp_path), messages=({"role": "user", "content": content},))

    async def scenario():
        runtime = SessionRuntime(EphemeralSessionStore())
        first = await runtime.open(request)
        await runtime.finalize(first, RunStatus.COMPLETED)
        second = await runtime.open(request)
        assert len(second.transcript) == 2
        assert second.transcript[0]["_nz_message_id"] != second.transcript[1]["_nz_message_id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("persist_session", [False, True])
def test_new_user_run_starts_empty_but_keeps_todo(monkeypatch, tmp_path, persist_session):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.tools import todo
    from test_native_product_runtime import _request, _install_runtime, _completion

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0
            self.initial_notes = []
            self.updated_notes = []

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            dynamic = next((str(message.get("content", "")) for message in kwargs["messages"]
                            if str(message.get("content", "")).startswith("<context-injection>")), "")
            marker = "## Working Memory (Scratchpad)"
            notes = dynamic[dynamic.index(marker):] if marker in dynamic else "Scratchpad is empty."
            if self.calls % 2:
                self.initial_notes.append(notes)
                response = _completion(tool="update_scratchpad")
                response.choices[0].message.tool_calls[0].function.arguments = json.dumps({"section": 'focus', "entries": [{"summary": f"CURRENT_FINDING_{self.calls}", "details": ""}]})
                return response
            self.updated_notes.append(notes)
            return _completion()

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = replace(_request(tmp_path), metadata={"permission_mode": "auto", "persist_session": persist_session})
    with scoped_workdir(tmp_path), scoped_session(request.session_id):
        todo.todo_update([{"content": "KEEP_TODO", "status": "pending"}])
    options = RunOptions(permission_asker=lambda *_a: True)
    environment = native_sdk.build_product_run_environment(request, options)

    async def scenario():
        nonlocal environment
        store = environment.runtime_services.session_runtime.store
        async def run(req):
            nonlocal environment
            environment = native_sdk.build_product_run_environment(req, options)
            # 环境/模型 owner 每轮新建，会话端口及磁盘仍是原件。
            environment.runtime_services.session_runtime.store = store
            try:
                return await native_sdk.NativeSDKRunner(environment).run_result(req, options)
            finally:
                environment.close()
        environment.close()
        await run(request)
        result = await run(replace(request, messages=({"role": "user", "content": "继续"},)))
        # 没有新用户消息的内部恢复，复用上一条用户消息的笔记。
        await run(replace(request, messages=tuple(result.messages)))

    try:
        asyncio.run(scenario())
        assert provider.initial_notes[:2] == ["Scratchpad is empty.", "Scratchpad is empty."]
        assert "CURRENT_FINDING_3" in provider.initial_notes[2]
        assert "CURRENT_FINDING_1" in provider.updated_notes[0]
        assert "CURRENT_FINDING_3" in provider.updated_notes[1]
        with scoped_workdir(tmp_path), scoped_session(request.session_id):
            assert "KEEP_TODO" in todo.render()
        assert "CURRENT_FINDING_5" in environment._sp.read()
    finally:
        environment.close()


def test_parallel_updates_preserve_every_note(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    pad = Scratchpad(tmp_path / "notes.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: pad.update_section('focus', [{"summary": f"PARALLEL_FINDING_{i:03d}", "details": ""}]), range(40)))
    assert all(not result.startswith("Error:") for result in results)
    restored = Scratchpad(tmp_path / "notes.json").read()
    assert all(f"PARALLEL_FINDING_{i:03d}" in restored for i in range(40))


@pytest.mark.parametrize("messages", [(), ({"role": "system", "content": "Respond briefly."},),
    ({"role": "user", "content": [{"type": "text", "text": "inspect"}]},)])
def test_legal_request_without_plain_user_text_reaches_execution_factory(monkeypatch, tmp_path, messages):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.state.sessions import session_scratchpad_path
    from nz_coder.tools.scratchpad import current_scratchpad
    from test_native_product_runtime import _request, _install_runtime

    class ReachedFactory(Exception):
        pass

    _install_runtime(monkeypatch, object())
    request = replace(_request(tmp_path), messages=messages)
    options = RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    with scoped_workdir(tmp_path), scoped_session(request.session_id):
        unrelated = Scratchpad(session_scratchpad_path())
        unrelated.update_section('focus', [{"summary": "UNRELATED_NOTE", "details": ""}])

    def factory(_context, _services):
        assert current_scratchpad().read() == "Scratchpad is empty."
        current_scratchpad().update_section('focus', [{"summary": "THIS_INVOCATION_ONLY", "details": ""}])
        assert "THIS_INVOCATION_ONLY" in current_scratchpad().read()
        raise ReachedFactory

    monkeypatch.setattr(environment.runner, "_execution_context_factory", factory)
    try:
        with pytest.raises(ReachedFactory):
            asyncio.run(environment.runner.run_result(request, options))
        assert "THIS_INVOCATION_ONLY" not in unrelated.read()
    finally:
        environment.close()
