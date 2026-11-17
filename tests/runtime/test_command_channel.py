"""Bounded command framing and real nonblocking socket/parallel call behavior."""
from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time

import pytest

from tests.fixtures.process_lifecycle import ControlledBackend, make_command_scope, wait_until, make_command_authorizer


def _pair(tmp_path, *, limits=None, command_authorizer=None):
    from nz_coder.runtime.process.command_channel import (
        ChildCommandClient, ChildCommandEndpoint, CommandChannelLimits, ParentCommandPump,
    )

    scope = make_command_scope(tmp_path, ready=False, command_authorizer=command_authorizer)
    limits = limits or CommandChannelLimits.from_settings(scope.authority.run_settings)
    parent, child = socket.socketpair()
    pump = ParentCommandPump(parent, handler=scope, limits=limits)
    pump.start()
    client = ChildCommandClient(ChildCommandEndpoint(child, scope.authority.settlement_id), limits=limits, deadline=time.monotonic() + 5)
    client.set_fact_receiver(lambda _fact: None)
    assert client.policy_ready(scope.authority.execution_policy, deadline=time.monotonic() + 2)
    return scope, pump, client


def _close(scope, pump, client):
    scope.request_stop()
    assert client.close(deadline=time.monotonic() + 2)
    assert pump.close(deadline=time.monotonic() + 2)
    assert scope.close(deadline=time.monotonic() + 2)


def _frame(peer, limits, kind, request_id, payload):
    from nz_coder.runtime.process.command_channel import CommandFrame, encode_frame

    peer.sendall(encode_frame(CommandFrame(kind, request_id, payload), limits=limits))


def _chunk(peer, limits, request_id, seq, data):
    _frame(peer, limits, 'request_chunk', request_id, {'seq': seq, 'data': base64.b64encode(data).decode('ascii')})


def test_process_terminal_facts_arrive_after_start_rpc_retired(tmp_path, monkeypatch):
    import nz_coder.runtime.process.process_service as processes

    backend = ControlledBackend()
    backend.exit_code = 7
    monkeypatch.setattr(processes, 'create_process_backend', lambda *_a, **_k: backend)
    scope, pump, client = _pair(tmp_path)
    facts, arrived = [], threading.Event()

    def receive(fact):
        facts.append(fact)
        arrived.set()

    try:
        client.set_fact_receiver(receive)
        output = client.call('process', {'operation': 'start', 'command': 'python check.py', 'tty': False}, cancel_event=None)
        assert not output.startswith('Error:'), output
        assert not client._calls
        backend.recover()
        assert arrived.wait(2), 'terminal delivery cannot depend on a live start RPC or read'
        assert len(facts) == 1
        assert facts[0].receipt.exit_code == 7
        assert facts[0].settlement_id == scope.authority.settlement_id
        assert facts[0].launch_request_id == 1
        assert client.facts_barrier(deadline=time.monotonic() + 2) >= 1
        assert scope._service._record_released(next(iter(scope._service._records.values())))
    finally:
        backend.recover()
        _close(scope, pump, client)


def test_broker_roundtrip_keeps_private_receipt_outside_public_metadata(tmp_path):
    import shlex
    import sys

    scope, pump, client = _pair(tmp_path)
    try:
        output = client.call('bash', {'command': shlex.join([sys.executable, '-c', 'raise SystemExit(7)'])}, cancel_event=None)
        receipts = getattr(output, 'execution_facts', ())
        assert len(receipts) == 1
        assert receipts[0].exit_code == 7 and receipts[0].outcome == 'failed'
        assert 'execution_facts' not in output.metadata
    finally:
        _close(scope, pump, client)


def test_pump_start_failure_closes_wakeup_descriptors(tmp_path, monkeypatch):
    from nz_coder.runtime.process.command_channel import CommandChannelLimits, ParentCommandPump

    scope = make_command_scope(tmp_path)
    parent, peer = socket.socketpair()
    pump = ParentCommandPump(parent, handler=scope, limits=CommandChannelLimits())
    actual_start = threading.Thread.start

    def start(worker):
        if worker.name == 'nz-command-parent-pump':
            raise RuntimeError('controlled pump start failure')
        return actual_start(worker)

    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        with pytest.raises(RuntimeError, match='controlled'):
            pump.start()
        assert pump._wakeup_read.fileno() == -1
        assert pump._wakeup_write.fileno() == -1
    finally:
        pump._wakeup_read.close()
        pump._wakeup_write.close()
        peer.close()
        assert scope.close(deadline=time.monotonic() + 2)


def test_barrier_wakes_after_terminal_callback_finishes(tmp_path, monkeypatch):
    import nz_coder.runtime.process.process_service as processes

    backend = ControlledBackend()
    monkeypatch.setattr(processes, 'create_process_backend', lambda *_a, **_k: backend)
    scope, pump, client = _pair(tmp_path)
    delivered, release_callback, barrier_blocked = threading.Event(), threading.Event(), threading.Event()
    original_callback = scope._terminal_callback

    def delayed_callback(fact):
        result = original_callback(fact)
        assert release_callback.wait(3)
        return result

    scope._terminal_callback = delayed_callback
    actual_watermark = scope.fact_barrier_watermark

    def watermark():
        value = actual_watermark()
        if value is None:
            barrier_blocked.set()
        return value

    monkeypatch.setattr(scope, 'fact_barrier_watermark', watermark)
    result, errors = [], []

    def barrier():
        try:
            result.append(client.facts_barrier(deadline=time.monotonic() + 2))
        except Exception as exc:
            errors.append(exc)

    waiter = threading.Thread(target=barrier)
    try:
        client.set_fact_receiver(lambda _fact: delivered.set())
        client.call('process', {'operation': 'start', 'command': 'python check.py', 'tty': False}, cancel_event=None)
        backend.recover()
        assert delivered.wait(2)
        waiter.start()
        assert barrier_blocked.wait(2)
        release_callback.set()
        waiter.join(3)
        assert not waiter.is_alive()
        assert not errors and result == [1], errors
    finally:
        release_callback.set()
        backend.recover()
        if waiter.ident is not None:
            waiter.join(3)
        _close(scope, pump, client)


def test_retirement_start_does_not_hold_scope_close_lock(tmp_path, monkeypatch):
    from nz_coder.runtime.process.command_channel import CommandChannelLimits, ParentCommandPump

    actual_start = threading.Thread.start
    entered, release, close_returned = threading.Event(), threading.Event(), threading.Event()

    def start(worker):
        if worker.name == 'nz-command-retirement-notifier':
            entered.set()
            assert release.wait(3)
        return actual_start(worker)

    monkeypatch.setattr(threading.Thread, 'start', start)
    scope = make_command_scope(tmp_path)
    parent, peer = socket.socketpair()
    constructed, closed = [], []
    creator = threading.Thread(target=lambda: constructed.append(ParentCommandPump(
        parent, handler=scope, limits=CommandChannelLimits())))

    def close():
        closed.append(scope.close(deadline=time.monotonic()))
        close_returned.set()

    closer = threading.Thread(target=close)
    try:
        creator.start()
        assert entered.wait(2)
        closer.start()
        assert close_returned.wait(.5), 'blocked thread.start must not hold the scope state lock'
        assert closed == [False], 'startup ownership remains pending until the real start returns'
    finally:
        release.set()
        creator.join(3)
        if closer.ident is not None:
            closer.join(3)
        peer.close()
        assert scope.close(deadline=time.monotonic() + 2)


def test_late_cancel_after_response_retirement_preserves_other_requests(tmp_path, monkeypatch):
    import nz_coder.tools.bash as bash

    entered = {name: threading.Event() for name in ('one', 'two')}
    release = {name: threading.Event() for name in entered}
    calls, errors, results = [], [], {}
    def execute(command, **kwargs):
        calls.append(command)
        entered[command].set()
        assert release[command].wait(3)
        return command
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    scope, pump, client = _pair(tmp_path)
    monkeypatch.setattr(scope, '_on_channel_error', errors.append)
    cancel_selected, send_cancel = threading.Event(), threading.Event()
    original_next = client._next_frame
    def next_frame():
        frame = original_next()
        if frame is not None and frame.kind == 'cancel':
            cancel_selected.set()
            assert send_cancel.wait(3)
        return frame
    monkeypatch.setattr(client, '_next_frame', next_frame)
    cancelled = threading.Event()
    threads = [threading.Thread(target=lambda name=name: results.update({name: client.call('bash', {'command': name}, cancel_event=cancelled if name == 'one' else None)})) for name in entered]
    try:
        threads[0].start()
        assert entered['one'].wait(2)
        threads[1].start()
        assert entered['two'].wait(2)
        cancelled.set()
        assert cancel_selected.wait(2)
        release['one'].set()
        wait_until(lambda: 1 not in scope._requests)
        send_cancel.set()
        release['two'].set()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()

        assert errors == []
        assert results == {'one': 'Error: Command cancelled', 'two': 'two'}
        assert calls.count('one') == calls.count('two') == 1
        assert not scope._draining
        with pytest.raises(ValueError, match='unknown'):
            scope.cancel_request(scope._high_water + 1)
    finally:
        send_cancel.set()
        for event in release.values():
            event.set()
        _close(scope, pump, client)
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()


def test_repeated_identical_policy_does_not_reset_tightened_parent_policy(tmp_path):
    from dataclasses import replace
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    (tmp_path / 'policy-probe').write_text('yes')
    scope, pump, client = _pair(tmp_path)
    try:
        with scoped_runtime_overrides(strict_local_tools=True):
            assert client.call('bash', {'command': 'cat policy-probe'}, cancel_event=None) == 'yes'
        assert scope._policy.strict_local_tools
        policy = scope.authority.execution_policy
        assert client.policy_ready(policy, deadline=time.monotonic() + 1)
        assert scope._policy.strict_local_tools
        assert not client.policy_ready(replace(policy, fingerprint='different-activation'), deadline=time.monotonic() + 1)
        assert 'not allowed' in client.call('bash', {'command': 'find .'}, cancel_event=None).lower()
    finally:
        _close(scope, pump, client)


@pytest.mark.parametrize('body', [
    b'{"schema":"nz.command.v2","schema":"nz.command.v2","kind":"finish","request_id":0,"payload":{}}',
    b'{"schema":"nz.command.v2","kind":"cancel","request_id":true,"payload":{}}',
    b'{"schema":"nz.command.v0","kind":"finish","request_id":0,"payload":{}}',
    b'{"schema":"nz.command.v2","kind":"unknown","request_id":0,"payload":{}}',
    b'{"schema":"nz.command.v2","kind":"finish","request_id":0,"payload":{},"owner":"parent"}',
    b'{"schema":"nz.command.v2","kind":"cancel","request_id":NaN,"payload":{}}',
    b'{"schema":"nz.command.v2","kind":"cancel","request_id":Infinity,"payload":{}}',
    b'{"schema":"nz.command.v2","kind":"cancel","request_id":1e10000,"payload":{}}',
    b'\xff',
])
def test_frames_reject_duplicate_keys_nan_wrong_types_unknown_schema_and_partial_eof(body):
    from nz_coder.runtime.process.command_channel import CommandChannelLimits, CommandFrameDecoder

    limits = CommandChannelLimits()
    decoder = CommandFrameDecoder(limits)
    with pytest.raises(ValueError):
        decoder.feed(struct.pack('!I', len(body)) + body)
    partial = CommandFrameDecoder(limits)
    assert partial.feed(b'\x00\x00') == []
    with pytest.raises(ValueError):
        partial.finish()


def test_oversized_header_does_not_allocate_declared_payload():
    import tracemalloc
    from nz_coder.runtime.process.command_channel import CommandChannelLimits, CommandFrameDecoder

    decoder = CommandFrameDecoder(CommandChannelLimits())
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            decoder.feed(struct.pack('!I', 2**31))
        assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
    finally:
        tracemalloc.stop()


def test_begin_order_allows_interleaved_large_messages(tmp_path, monkeypatch):
    from nz_coder.runtime.process.command_channel import CommandChannelLimits, ParentCommandPump
    import nz_coder.tools.bash as bash

    completed = []
    monkeypatch.setattr(bash, 'execute_bash_local', lambda command, **kwargs: completed.append(command[:3]) or command[:3])
    errors = []
    scope = make_command_scope(tmp_path, ready=True, errors=errors)
    limits = CommandChannelLimits()
    parent, peer = socket.socketpair()
    peer.settimeout(2)
    pump = ParentCommandPump(parent, handler=scope, limits=limits)
    pump.start()
    one = json.dumps({'tool': 'bash', 'arguments': {'command': 'one' + 'x' * 100000}, 'restriction': {}}).encode()
    two = json.dumps({'tool': 'bash', 'arguments': {'command': 'two' + 'y' * 100000}, 'restriction': {}}).encode()
    try:
        _frame(peer, limits, 'request_begin', 1, {'length': len(one)})
        _frame(peer, limits, 'request_begin', 2, {'length': len(two)})
        for seq, offset in enumerate(range(0, max(len(one), len(two)), 10000)):
            _chunk(peer, limits, 1, seq, one[offset:offset + 10000])
            _chunk(peer, limits, 2, seq, two[offset:offset + 10000])
        _frame(peer, limits, 'request_end', 2, {})
        wait_until(lambda: completed == ['two'])
        _frame(peer, limits, 'request_end', 1, {})
        wait_until(lambda: completed == ['two', 'one'])
        _frame(peer, limits, 'request_begin', 1, {'length': len(one)})
        wait_until(lambda: bool(errors))
        assert len(errors) == 1
        assert completed == ['two', 'one']
    finally:
        scope.request_stop()
        peer.close()
        assert pump.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


def test_cancelled_unretired_records_keep_quota_and_control_capacity(tmp_path, monkeypatch):
    from nz_coder.runtime.process import process_service
    from nz_coder.runtime.process.command_channel import CommandChannelLimits

    backend = ControlledBackend()
    backend.io_release.clear()
    monkeypatch.setattr(process_service, 'create_process_backend', lambda *a, **k: backend)
    scope, pump, client = _pair(tmp_path, limits=CommandChannelLimits(max_requests=1), command_authorizer=make_command_authorizer(tmp_path))
    started = json.loads(client.call('process', {'operation': 'start', 'command': 'controlled'}, cancel_event=None))
    cancel = threading.Event()
    results = []
    caller = threading.Thread(target=lambda: results.append(client.call('process', {'operation': 'write', 'process_id': started['process']['process_id'], 'data': 'x'}, cancel_event=cancel)))
    caller.start()
    try:
        assert backend.io_entered.wait(2)
        cancel.set()
        caller.join(2)
        assert not caller.is_alive() and results[0].startswith('Error:')
        assert 'limit' in client.call('process', {'operation': 'list'}, cancel_event=None).lower()
        assert client.finish(deadline=time.monotonic() + .1) is False
        scope.request_stop()
        assert backend.stop_seen.wait(2)
        assert scope.close(deadline=time.monotonic()) is False
    finally:
        cancel.set()
        backend.recover()
        caller.join(2)
        _close(scope, pump, client)


@pytest.mark.parametrize('slow_peer', [False, True])
def test_slow_peer_and_half_frame_do_not_block_out_of_band_stop(tmp_path, monkeypatch, slow_peer):
    from nz_coder.runtime.process import command_channel as channel
    from nz_coder.tools import ToolOutput
    import nz_coder.tools.bash as bash

    timeouts = []
    select_entered = threading.Event()
    actual_selector = channel.selectors.DefaultSelector
    class Selector(actual_selector):
        def select(self, timeout=None):
            timeouts.append(timeout)
            select_entered.set()
            return super().select(timeout)
    monkeypatch.setattr(channel.selectors, 'DefaultSelector', Selector)
    returned = threading.Event()
    def output(command, **kwargs):
        returned.set()
        return ToolOutput('x' * 200000)
    monkeypatch.setattr(bash, 'execute_bash_local', output)
    scope = make_command_scope(tmp_path)
    limits = channel.CommandChannelLimits()
    parent, peer = socket.socketpair()
    parent.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    pump = channel.ParentCommandPump(parent, handler=scope, limits=limits)
    pump.start()
    try:
        if slow_peer:
            data = json.dumps({'tool': 'bash', 'arguments': {'command': 'echo big'}, 'restriction': {}}).encode()
            _frame(peer, limits, 'request_begin', 1, {'length': len(data)})
            _chunk(peer, limits, 1, 0, data)
            _frame(peer, limits, 'request_end', 1, {})
            assert returned.wait(2)
        else:
            peer.sendall(b'\x00\x00')
        assert select_entered.wait(2)
        scope.request_stop()
        assert pump.close(deadline=time.monotonic() + .5)
        # 阻塞 select 由独立唤醒 fd 中断；停机期限仍由上面的真实 close 断言覆盖。
        assert timeouts and all(value is None for value in timeouts)
    finally:
        peer.close()
        assert pump.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize('field', ['title', 'metadata', 'process_list'])
def test_message_budget_counts_titles_metadata_and_process_list(tmp_path, monkeypatch, field):
    from nz_coder.runtime.process.command_channel import CommandChannelLimits
    from nz_coder.tools import ToolOutput
    import nz_coder.tools.bash as bash

    value = ToolOutput('body', title='x' * 5000 if field == 'title' else '', metadata={'payload': 'x' * 5000} if field == 'metadata' else {'processes': [{'command': 'x' * 1000}] * 5} if field == 'process_list' else {})
    monkeypatch.setattr(bash, 'execute_bash_local', lambda *a, **k: value)
    scope, pump, client = _pair(tmp_path, limits=CommandChannelLimits(max_message_bytes=4096))
    try:
        result = client.call('bash', {'command': 'echo body'}, cancel_event=None)
        assert result.startswith('Error:') and 'limit' in result.lower()
    finally:
        _close(scope, pump, client)


def test_parallel_long_call_does_not_block_short_call(tmp_path):
    import shlex
    import sys

    scope, pump, client = _pair(tmp_path)
    release = tmp_path / 'release'
    entered = tmp_path / 'entered'
    script = "from pathlib import Path; import time; Path('entered').touch()\nwhile not Path('release').exists(): time.sleep(.01)\nprint('long')"
    result = []
    caller = threading.Thread(target=lambda: result.append(client.call('bash', {'command': shlex.join((sys.executable, '-u', '-c', script))}, cancel_event=None)))
    caller.start()
    try:
        wait_until(entered.exists)
        short = client.call('bash', {'command': 'echo short'}, cancel_event=None)
        assert str(short) == 'short'
        assert caller.is_alive()
        release.touch()
        caller.join(2)
        assert not caller.is_alive() and str(result[0]) == 'long'
        assert client.finish(deadline=time.monotonic() + 2)
    finally:
        release.touch()
        caller.join(2)
        _close(scope, pump, client)


def test_control_flood_does_not_accumulate_finish_acks(tmp_path):
    from nz_coder.runtime.process.command_channel import CommandChannelLimits, CommandFrame, ParentCommandPump

    scope = make_command_scope(tmp_path)
    parent, peer = socket.socketpair()
    pump = ParentCommandPump(parent, handler=scope, limits=CommandChannelLimits())
    try:
        for _ in range(100):
            pump._received(CommandFrame('finish', 0, {}))
        frames = []
        for _ in range(101):
            frame = pump._next_frame()
            if frame is None:
                break
            frames.append(frame.kind)
        assert frames == ['finish_ack']
    finally:
        parent.close()
        peer.close()
        assert scope.close(deadline=time.monotonic() + 2)


def _remote_pair(tmp_path, **kwargs):
    from nz_coder.runtime.process.command_channel import ChildCommandClient, ChildCommandEndpoint, CommandChannelLimits

    scope = make_command_scope(tmp_path, ready=False, **kwargs)
    endpoint = scope.start()
    # 测试内模拟spawn后的独立子端，父副本仍由原endpoint退役。
    child = ChildCommandEndpoint(endpoint.socket.dup(), endpoint.settlement_id)
    endpoint.close()
    client = ChildCommandClient(child, limits=CommandChannelLimits.from_settings(scope.authority.run_settings), deadline=time.monotonic() + 5)
    client.set_fact_receiver(lambda _fact: None)
    assert client.policy_ready(scope.authority.execution_policy, deadline=time.monotonic() + 2)
    return scope, client


def test_read_only_allowlist_workdir_private_path_and_package_policy_match_local(tmp_path):
    from nz_coder.runtime.agent.subagent import _run_allowed_tool
    from nz_coder.runtime.process.command_channel import scoped_command_client
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.bash import run_bash

    (tmp_path / 'nested').mkdir()
    scope, client = _remote_pair(tmp_path, read_only=True)
    try:
        with scoped_workdir(tmp_path):
            local = run_bash('pwd', read_only=True, workdir='nested')
            with scoped_command_client(client):
                remote = _run_allowed_tool('bash', {'command': 'pwd', 'workdir': 'nested'}, {'bash'}, 'explore')
                blocked = run_bash('echo forbidden > forbidden.txt', read_only=False)
                package = run_bash('python -m pip install invalid-package')
                escaped = run_bash('pwd', workdir='../')
        assert str(local) == str(remote) == str(tmp_path / 'nested')
        assert blocked.startswith('Error:') and not (tmp_path / 'forbidden.txt').exists()
        assert package.startswith('Error:') and escaped.startswith('Error:')
    finally:
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


def test_command_restriction_tightens_atomically_before_admission(tmp_path, monkeypatch):
    import nz_coder.tools.bash as bash
    from nz_coder.runtime.core.execution_context import strict_local_tools, scoped_runtime_overrides

    entered, release = threading.Event(), threading.Event()
    seen = {}
    def execute(command, **kwargs):
        before = strict_local_tools()
        if command == 'first':
            entered.set()
            release.wait()
        seen[command] = before, strict_local_tools()
        return command
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    scope, client = _remote_pair(tmp_path)
    def first():
        with scoped_runtime_overrides(strict_local_tools=False):
            client.call('bash', {'command': 'first'}, cancel_event=None)
    owner = threading.Thread(target=first)
    owner.start()
    try:
        assert entered.wait(2)
        with scoped_runtime_overrides(strict_local_tools=True):
            assert client.call('bash', {'command': 'narrow'}, cancel_event=None) == 'narrow'
        with scoped_runtime_overrides(strict_local_tools=False):
            assert client.call('bash', {'command': 'late'}, cancel_event=None) == 'late'
        release.set()
        owner.join(2)
        assert seen['first'] == (False, False)
        assert seen['narrow'] == seen['late'] == (True, True)
    finally:
        release.set()
        owner.join(2)
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


def test_parallel_progress_keeps_original_tool_context(tmp_path, monkeypatch):
    import nz_coder.tools.bash as bash
    from nz_coder.protocol.attachments import make_image_attachment
    from nz_coder.tools import ToolOutput, current_tool_call_id, report_tool_metadata, scoped_tool_call, scoped_tool_metadata_reporter

    both = threading.Barrier(2)
    attachment = make_image_attachment(b'\x89PNG\r\n\x1a\n', 'image/png')
    def execute(command, **kwargs):
        both.wait(timeout=2)
        report_tool_metadata(title=command, metadata={'output': command})
        return ToolOutput(command, title=command, metadata={'key': command}, attachments=[attachment])
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    scope, client = _remote_pair(tmp_path)
    previews, results = {}, {}
    def call(name):
        def report(title, metadata):
            previews.setdefault(name, []).append((current_tool_call_id(), title, metadata['output']))
        with scoped_tool_call(name), scoped_tool_metadata_reporter(report):
            results[name] = client.call('bash', {'command': name}, cancel_event=None)
    threads = [threading.Thread(target=call, args=(x,)) for x in ['one', 'two']]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
        assert previews == {'one': [('one', 'one', 'one')], 'two': [('two', 'two', 'two')]}
        for name in ['one', 'two']:
            assert isinstance(results[name], ToolOutput)
            assert str(results[name]) == results[name].title == name
            assert results[name].metadata == {'key': name}
            assert results[name].attachments == [attachment]
    finally:
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


def test_proxy_disconnect_has_no_local_fallback(tmp_path, monkeypatch):
    from nz_coder.runtime.process.command_channel import scoped_command_client
    import nz_coder.tools.bash as bash
    import nz_coder.tools.process as process

    scope, client = _remote_pair(tmp_path)
    assert client.close(deadline=time.monotonic() + 2)
    def local(*a, **k):
        raise AssertionError('local fallback is forbidden')
    monkeypatch.setattr(bash, 'execute_bash_local', local)
    monkeypatch.setattr(process, 'workspace_process_service', local)
    try:
        with scoped_command_client(client):
            assert bash.run_bash('echo unreachable').startswith('Error:')
            assert process.run_process('start', command='echo unreachable').startswith('Error:')
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_scope_does_not_retain_parent_file_transaction(tmp_path):
    import gc
    import weakref
    from nz_coder.tools.files import bind_tool_state

    class ParentTracker:
        pass
    tracker = ParentTracker()
    reference = weakref.ref(tracker)
    with bind_tool_state(change_tracker=tracker):
        scope, client = _remote_pair(tmp_path)
    del tracker
    gc.collect()
    try:
        assert reference() is None
        result = client.call('bash', {'command': 'printf new > sample.txt'}, cancel_event=None)
        assert not result.startswith('Error:')
        assert (tmp_path / 'sample.txt').read_text() == 'new'
    finally:
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


def test_child_publisher_is_retained_after_parent_switch(tmp_path):
    import gc
    import shlex
    import sys
    import weakref
    from nz_coder.protocol.session_events import SessionEventBus, scoped_session_event_bus

    bus = SessionEventBus(session_id='parent')
    original = bus.for_interaction('origin', agent_invocation_id='main').for_child('child')
    reference = weakref.ref(original)
    scope, client = _remote_pair(tmp_path, publisher=original)
    del original
    gc.collect()
    try:
        assert reference() is not None
        with scoped_session_event_bus(bus.for_interaction('later', agent_invocation_id='other')):
            started = json.loads(client.call('process', {'operation': 'start', 'tty': False, 'command': shlex.join((sys.executable, '-u', '-c', "print('secret-body', flush=True); import time; time.sleep(30)"))}, cancel_event=None))
            assert client.call('process', {'operation': 'kill', 'process_id': started['process']['process_id']}, cancel_event=None)
        events = [event.to_dict() for event in bus.recent(30)]
        types = [event['type'] for event in events]
        assert types.count('process.exited') == types.count('process.killed') == 1
        for event in events:
            if event['type'].startswith('process.'):
                assert event['meta']['run_id'] == 'origin' and event['meta']['agent_id'] == 'child'
        assert 'secret-body' not in json.dumps([e for e in events if e['type'] != 'process.started'])
    finally:
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)
        bus.close()


def test_blocked_reporter_does_not_hold_request_state_lock(tmp_path, monkeypatch):
    import nz_coder.tools.bash as bash
    from nz_coder.tools import report_tool_metadata, scoped_tool_metadata_reporter

    entered, release, command_release, cancel = (threading.Event() for _ in range(4))
    def execute(command, **kwargs):
        report_tool_metadata(title=command, metadata={'output': command})
        command_release.wait()
        return command
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    scope, client = _remote_pair(tmp_path)
    results = []
    def reporter(title, metadata):
        entered.set()
        release.wait()
    def call():
        with scoped_tool_metadata_reporter(reporter):
            results.append(client.call('bash', {'command': 'blocked'}, cancel_event=cancel))
    owner = threading.Thread(target=call)
    owner.start()
    try:
        assert entered.wait(2)
        cancel.set()
        owner.join(.3)
        assert not owner.is_alive()
        assert results[0].startswith('Error: Command cancelled')
        assert client.close(deadline=time.monotonic()) is False
    finally:
        release.set()
        command_release.set()
        owner.join(2)
        assert client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)


def test_selector_close_failure_retains_original_pump_owner(tmp_path, monkeypatch):
    from nz_coder.runtime.process import command_channel as channel

    entered, recover, selecting = (threading.Event() for _ in range(3))
    selectors = []
    original = channel.selectors.DefaultSelector
    class Selector(original):
        def __init__(self):
            super().__init__()
            selectors.append(self)
        def select(self, timeout=None):
            selecting.set()
            return super().select(timeout)
        def close(self):
            entered.set()
            if not recover.is_set():
                raise OSError('controlled selector close')
            super().close()
    monkeypatch.setattr(channel.selectors, 'DefaultSelector', Selector)
    scope = make_command_scope(tmp_path)
    parent, peer = socket.socketpair()
    pump = channel.ParentCommandPump(parent, handler=scope, limits=channel.CommandChannelLimits())
    pump.start()
    try:
        assert selecting.wait(2)
        scope.request_stop()
        pump.request_stop()
        assert entered.wait(2)
        assert pump.close(deadline=time.monotonic()) is False
        recover.set()
        assert pump.close(deadline=time.monotonic() + 2)
        assert parent.fileno() == -1
    finally:
        recover.set()
        pump.close(deadline=time.monotonic() + 2)
        peer.close()
        if pump._thread is not None and not pump._thread.is_alive():
            parent.close()
            for selector in selectors:
                selector.close()
        assert scope.close(deadline=time.monotonic() + 2)


def test_client_partial_thread_start_failure_cleans_original_owner(tmp_path, monkeypatch):
    from nz_coder.runtime.process.command_channel import ChildCommandClient, ChildCommandEndpoint, CommandChannelLimits

    scope = make_command_scope(tmp_path, ready=False)
    endpoint = scope.start()
    child = ChildCommandEndpoint(endpoint.socket.dup(), endpoint.settlement_id)
    endpoint.close()
    started = []
    original = threading.Thread.start
    def start(thread):
        original(thread)
        if thread.name == 'nz-command-child-pump':
            started.append(thread)
            raise RuntimeError('controlled partially started child pump')
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        with pytest.raises(RuntimeError, match='partially started'):
            ChildCommandClient(child, limits=CommandChannelLimits(), deadline=time.monotonic() + 2)
        assert started
        started[0].join(.3)
        assert not started[0].is_alive()
        assert child.socket.fileno() == -1
    finally:
        assert scope.close(deadline=time.monotonic() + 2)
        for thread in started:
            thread.join(2)
            assert not thread.is_alive()


def test_sealed_activation_keeps_old_facts_historical_and_delivers_new_launch(tmp_path, monkeypatch):
    import nz_coder.runtime.process.process_service as processes
    old, new = ControlledBackend(), ControlledBackend()
    new.exit_code = 7
    backends = iter((old, new))
    monkeypatch.setattr(processes, 'create_process_backend', lambda *_args, **_kwargs: next(backends))
    scope, pump, client = _pair(tmp_path)
    first_facts, second_facts, arrived = [], [], threading.Event()
    try:
        client.set_fact_receiver(lambda fact: first_facts.append(fact))
        assert not client.call('process', {'operation': 'start', 'command': 'python old.py', 'tty': False}, cancel_event=None).startswith('Error:')
        watermark = client.facts_barrier(deadline=time.monotonic() + 2)
        client.facts_sealed(watermark)
        def receive(fact):
            second_facts.append(fact)
            arrived.set()
        client.set_fact_receiver(receive)
        assert not client.call('process', {'operation': 'start', 'command': 'python new.py', 'tty': False}, cancel_event=None).startswith('Error:')
        old.recover()
        new.recover()
        assert arrived.wait(2), 'new activation must receive its process terminal without read'
        assert first_facts == []
        assert len(second_facts) == 1 and second_facts[0].receipt.exit_code == 7
        assert client.facts_barrier(deadline=time.monotonic() + 2) > watermark
        assert len(scope._historical_facts) == 1
    finally:
        old.recover()
        new.recover()
        _close(scope, pump, client)
