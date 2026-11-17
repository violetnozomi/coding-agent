"""Parent command policy follows actual activation and only becomes stricter."""
from __future__ import annotations

import asyncio
import copy

import pytest

from nz_coder.runtime.core import execution_context as context


@pytest.mark.parametrize(
    'restored,pure,initial,want',
    [(True, True, 'pytest tests/unit', ('tests/unit',)),
     (False, True, 'pytest tests/unit', ('tests/current',)),
     (True, False, 'pytest tests/unit', ('tests/current',)),
     (True, True, '', ('tests/current',))],
)
def test_policy_uses_original_task_only_after_restored_pure_continuation(
    restored, pure, initial, want,
):
    activation = context.CommandActivation('pytest tests/current', True, pure, None)
    policy = context.derive_command_execution_policy(
        activation, restored=restored, initial_task_text=initial,
        strict_local_tools=False,
    )
    changed = context.derive_command_execution_policy(
        activation, restored=restored, initial_task_text="different task",
        strict_local_tools=False,
    )
    assert (policy.fingerprint != changed.fingerprint) is (restored and pure)
    assert policy.strict_local_tools is False


@pytest.mark.parametrize('message,want', [
    (None, False), ({'role': 'assistant', 'content': 'continue'}, False),
    ({'role': 'user', 'content': 'pytest tests/next'}, True),
    ({'role': 'user', 'content': [], '_nz_synthetic': True}, False),
    ({'role': 'user', 'content': 'summary', '_nz_synthetic': True}, False),
])
def test_restore_eligibility_matches_lifecycle_adapter(message, want):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    genuine = context.is_genuine_runtime_followup(message)
    assert genuine is want
    state = RuntimeState()
    state.reset()
    data = state.to_dict(active=False)
    assert RuntimeState().restore(data, allow_inactive=genuine) is want


def test_late_parallel_snapshot_cannot_relax_policy():
    policy = context.CommandExecutionPolicy(False, 'initial')
    narrow = context.tighten_command_policy(
        policy, context.CommandPolicyRestriction(True),
    )
    late = context.tighten_command_policy(
        narrow, context.CommandPolicyRestriction(False),
    )
    assert late.strict_local_tools is True
    assert late.fingerprint == 'initial'
    assert policy.strict_local_tools is False


def test_activation_and_fingerprint_ignore_ephemeral_message_identity():
    from nz_coder.runtime.core.execution_context import command_activation

    messages = [
        {'role': 'user', 'content': 'pytest tests/unit'},
        {'role': 'assistant', 'content': 'working', '_nz_continuation': {'version': 1, 'status': 'max_turns', 'summary': 'unfinished'}},
        {'role': 'user', 'content': 'go on'},
    ]
    first = command_activation(messages)
    assert first.resume_activation and first.pure_continuation
    assert first.current_user_message is None
    before = copy.deepcopy(messages)
    a = context.derive_command_execution_policy(first, restored=True, initial_task_text='pytest tests/unit', strict_local_tools=False)
    messages[-1]['_nz_message_id'] = 'random-new-id'
    b = context.derive_command_execution_policy(command_activation(messages), restored=True, initial_task_text='pytest tests/unit', strict_local_tools=False)
    assert a.fingerprint == b.fingerprint
    assert before[0] == messages[0]
    c = context.derive_command_execution_policy(first, restored=False, initial_task_text='pytest tests/unit', strict_local_tools=False)
    assert a.fingerprint != c.fingerprint


@pytest.mark.parametrize('native', [False, True])
def test_native_activation_preview_matches_open_without_writing(tmp_path, native):
    from nz_coder.runtime.core.profiles import READ_CHILD_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest
    from nz_coder.runtime.session.model import Session
    from nz_coder.runtime.session.runtime import SessionRuntime, preview_activation_transcript
    from nz_coder.runtime.session.store import LegacyJsonSessionStore

    store = LegacyJsonSessionStore()
    session = Session.create('child', [
        {'role': 'user', 'content': 'pytest tests/unit'},
        {'role': 'assistant', 'content': 'unfinished'},
    ], workspace=tmp_path, parent_session_id='parent') if native else None
    if session:
        session.begin_run()
        asyncio.run(store.save(session))
    before = copy.deepcopy(session)
    request = [{'role': 'user', 'content': 'go on'}]
    preview = preview_activation_transcript(session, request)
    assert session == before
    request_run = RunRequest(
        agent=AgentDefinition(name='explore', instructions='inspect'),
        profile=READ_CHILD_PROFILE, messages=tuple(request), workspace=tmp_path,
        session_id='child', metadata={'parent_session_id': 'parent'},
    )
    actual = asyncio.run(SessionRuntime(store).open(request_run))
    assert [(x['role'], x.get('content')) for x in actual.session.transcript] == [(x['role'], x.get('content')) for x in preview]


def test_conflicting_preview_does_not_mutate_original_session(tmp_path):
    from nz_coder.runtime.session.model import Session
    from nz_coder.runtime.session.runtime import preview_activation_transcript

    session = Session.create('child', [{'role': 'user', 'content': 'original', '_nz_message_id': 'one'}], workspace=tmp_path)
    before = copy.deepcopy(session.transcript)
    with pytest.raises(ValueError, match='conflicts'):
        preview_activation_transcript(session, [{'role': 'user', 'content': 'conflict', '_nz_message_id': 'two'}])
    assert session.transcript == before


def _initialization_context(persisted):
    from types import SimpleNamespace
    from nz_coder.runtime.core.lifecycle_context import LifecycleRunState

    return SimpleNamespace(
        run_state=LifecycleRunState(), clear_reverter=lambda: None,
        verification_runtime=SimpleNamespace(initialize=lambda **_kwargs: None),
        recovery=SimpleNamespace(start_tool_call_run=lambda: None),
        clear_read_cache=lambda: None, admission_handle=None,
        reset_hooks=lambda: None, clear_reasoning_escalation=lambda: None,
        commit=lambda: None, restore_agent_role=lambda: None, session_id='child',
        bind_user_messages=lambda messages: None,
        prepare_runtime_state=lambda *a, **k: False,
        runtime_state=SimpleNamespace(turn_count=0, initial_task_text=''),
        start_run_evidence=lambda: None,
        persist_runtime_state=lambda **kwargs: persisted.set(),
        publish_started=lambda *args: None,
    )


def test_commands_and_model_do_not_advance_before_policy_ack(tmp_path):
    import socket
    import threading
    import time
    from nz_coder.runtime.core.execution_context import command_activation
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    from nz_coder.runtime.process.command_channel import ChildCommandClient, ChildCommandEndpoint, CommandChannelLimits, ParentCommandPump, scoped_command_client
    from tests.fixtures.process_lifecycle import make_command_scope

    messages = [{'role': 'user', 'content': 'pytest tests/unit'}]
    policy = context.derive_command_execution_policy(command_activation(messages), restored=False, initial_task_text='', strict_local_tools=False)
    scope = make_command_scope(tmp_path, ready=False, policy=policy)
    parent, child = socket.socketpair()
    limits = CommandChannelLimits()
    client = ChildCommandClient(ChildCommandEndpoint(child, scope.authority.settlement_id), limits=limits, deadline=time.monotonic() + 5)
    client.set_fact_receiver(lambda _fact: None)
    persisted, finished = threading.Event(), threading.Event()
    model_steps, errors = [], []
    def run():
        try:
            with scoped_command_client(client), context.scoped_runtime_overrides(strict_local_tools=False):
                ProductionRunLifecycle().initialize(_initialization_context(persisted), messages, False)
                model_steps.append('advance')
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    owner = threading.Thread(target=run)
    owner.start()
    pump = None
    try:
        assert persisted.wait(2)
        assert not finished.wait(.05)
        assert model_steps == []
        assert client.call('bash', {'command': 'echo not-initialized'}, cancel_event=None).startswith('Error:')
        pump = ParentCommandPump(parent, handler=scope, limits=limits)
        pump.start()
        owner.join(2)
        assert not owner.is_alive() and errors == [] and model_steps == ['advance']
        assert client.finish(deadline=time.monotonic() + 2)
    finally:
        scope.request_stop()
        assert client.close(deadline=time.monotonic() + 2)
        owner.join(2)
        if pump is not None:
            assert pump.close(deadline=time.monotonic() + 2)
        else:
            parent.close()
        assert scope.close(deadline=time.monotonic() + 2)


def test_policy_mismatch_stops_without_default_fallback(tmp_path, monkeypatch):
    import threading
    import time
    from nz_coder.protocol.public_error import PublicRuntimeError
    from nz_coder.runtime.core.execution_context import command_activation
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    from nz_coder.runtime.process.command_channel import ChildCommandClient, ChildCommandEndpoint, CommandChannelLimits, scoped_command_client
    from tests.fixtures.process_lifecycle import make_command_scope

    policy = context.derive_command_execution_policy(command_activation([{'role': 'user', 'content': 'pytest tests/original'}]), restored=False, initial_task_text='', strict_local_tools=False)
    errors = []
    scope = make_command_scope(tmp_path, ready=False, policy=policy, errors=errors)
    endpoint = scope.start()
    client = ChildCommandClient(ChildCommandEndpoint(endpoint.socket.dup(), endpoint.settlement_id), limits=CommandChannelLimits(), deadline=time.monotonic() + 5)
    client.set_fact_receiver(lambda _fact: None)
    endpoint.close()
    try:
        with scoped_command_client(client), context.scoped_runtime_overrides(strict_local_tools=False):
            with pytest.raises(PublicRuntimeError) as failed:
                ProductionRunLifecycle().initialize(_initialization_context(threading.Event()), [{'role': 'user', 'content': 'pytest tests/changed'}], False)
        assert failed.value.public_error.code == 'child_command_channel_error'
        assert len(errors) == 1
        assert errors[0].code == 'child_command_channel_error'
    finally:
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)
