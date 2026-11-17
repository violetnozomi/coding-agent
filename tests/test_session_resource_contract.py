"""会话发现与持久化共享资源契约的真实文件回归。"""
import pytest

from nz_coder.http_service.manager import SessionManager
from nz_coder.state import sessions
from nz_coder.state.workdir import scoped_workdir


def test_saved_session_above_old_limit_is_listed_and_restored(tmp_path):
    body = 'a' * (17 * 1024 * 1024)
    with scoped_workdir(tmp_path):
        sessions.save_session([{'role': 'user', 'content': body}], session_id='large', activate=False, model='test')
    manager = SessionManager(workspace_roots=[tmp_path])
    try:
        result = manager.list()
        assert [item['id'] for item in result['sessions']] == ['large']
        assert result['unavailable'] == []
        assert manager.get('large').history[0]['content'] == body
    finally:
        manager.close()


def test_unavailable_session_is_not_absent(tmp_path):
    with scoped_workdir(tmp_path):
        path = sessions.session_dir() / 'broken.json'
        path.parent.mkdir(parents=True)
        path.write_text('{bad', encoding='utf-8')
    manager = SessionManager(workspace_roots=[tmp_path])
    try:
        result = manager.list()
        assert result['sessions'] == []
        assert result['unavailable'][0]['candidate_id'] == 'broken'
        assert result['unavailable'][0]['code'] == 'corrupt'
    finally:
        manager.close()


def test_precise_id_outside_window_is_restored(tmp_path):
    import os
    with scoped_workdir(tmp_path):
        old = sessions.save_session([{'role': 'user', 'content': 'old'}], session_id='old', activate=False, model='test')
        os.utime(old, (1, 1))
        sessions.save_session([{'role': 'user', 'content': 'new'}], session_id='new', activate=False, model='test')
    manager = SessionManager(workspace_roots=[tmp_path], max_saved_sessions_per_workspace=1)
    try:
        assert [item['id'] for item in manager.list()['sessions']] == ['new']
        assert manager.get('old').history[0]['content'] == 'old'
    finally:
        manager.close()


def test_budget_failure_preserves_all_existing_json(tmp_path, monkeypatch):
    import hashlib
    with scoped_workdir(tmp_path):
        from nz_coder.protocol.message_schema import SESSION_SUMMARY_KEY
        summary = {'additions': 1, 'deletions': 1, 'files': 1, 'diffs': [{'file': 'a.py', 'patch': '-old\n+new', 'additions': 1, 'deletions': 1, 'status': 'modified'}]}
        messages = [{'role': 'user', 'content': 'small'}, {'role': 'assistant', 'content': 'done', SESSION_SUMMARY_KEY: summary}]
        sessions.save_session(messages, session_id='one', model='test')
        assert sessions.session_diff_path('one').exists()
        root = sessions.session_dir()
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json')}
        monkeypatch.setattr(sessions, '_session_byte_limit', lambda: 2048)
        with pytest.raises(sessions.SessionReadError, match='resource_limit'):
            messages[0]['content'] = '界' * 2048
            summary['diffs'][0]['patch'] = '-old\n+changed'
            sessions.save_session(messages, session_id='one', model='test')
        assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json')} == before


def test_large_session_through_real_http_client(tmp_path):
    import threading
    from nz_coder.http_service import NZCoderClient, SessionHTTPService
    body = 'z' * (17 * 1024 * 1024)
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': body}], session_id='http-large', activate=False, model='test')
    manager = SessionManager(workspace_roots=[tmp_path])
    service = SessionHTTPService(port=0, manager=manager, token='offline-test-token-1234')
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=20)
        result = client.list_sessions()
        assert result['unavailable'] == []
        assert set(result['sessions'][0]) == {'id', 'workspace_id', 'title', 'status', 'runtime_status', 'parent_session_id', 'created_at', 'updated_at', 'message_count', 'running'}
        assert len(str(result)) < 2000
        assert client.get_session('http-large')['id'] == 'http-large'
        assert client.messages('http-large')[0]['content'] == body
        assert path.stat().st_size > 16 * 1024 * 1024
    finally:
        service.shutdown()
        thread.join(timeout=5)


def test_reader_exact_utf8_byte_boundaries(tmp_path, monkeypatch):
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': '中文'}], session_id='boundary', activate=False, model='test')
        size = path.stat().st_size
        monkeypatch.setattr(sessions, '_session_byte_limit', lambda: size + 1)
        assert sessions.load_session('boundary')['messages'][0]['content'] == '中文'
        monkeypatch.setattr(sessions, '_session_byte_limit', lambda: size)
        assert sessions.load_session('boundary')['messages'][0]['content'] == '中文'
        monkeypatch.setattr(sessions, '_session_byte_limit', lambda: size - 1)
        with pytest.raises(sessions.SessionReadError) as error:
            sessions.load_session('boundary')
        assert error.value.kind == 'resource_limit'


def test_duplicate_id_in_second_workspace_is_ambiguous(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    for root in (first, second):
        with scoped_workdir(root):
            path = sessions.save_session([{'role': 'user', 'content': 'a'}], session_id='duplicate', activate=False, model='test')
    path.write_bytes(b'{broken')
    manager = SessionManager(workspace_roots=[first, second])
    try:
        with pytest.raises(sessions.SessionReadError) as error:
            manager.get('duplicate')
        assert error.value.kind == 'ambiguous'
        assert {item['code'] for item in manager.list()['unavailable']} == {'ambiguous'}
    finally:
        manager.close()


def test_remote_attach_only_unavailable_does_not_create(tmp_path, monkeypatch):
    import argparse
    import asyncio
    import threading
    from rich.console import Console
    from nz_coder.http_service import NZCoderClient, SessionHTTPService
    from nz_coder.interface import remote
    with scoped_workdir(tmp_path):
        path = sessions.session_dir() / 'broken.json'
        path.parent.mkdir(parents=True)
        path.write_text('{bad', encoding='utf-8')
    manager = SessionManager(workspace_roots=[tmp_path])
    service = SessionHTTPService(port=0, manager=manager, token='offline-test-token-1234')
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(remote, '_connection', lambda args: (service.base_url, service.token, '', False))
    try:
        args = argparse.Namespace(new=False, session_id=None, workspace_id=None)
        with pytest.raises(RuntimeError, match='unavailable'):
            asyncio.run(remote._attach(args, Console()))
        result = NZCoderClient(service.base_url, service.token).list_sessions()
        assert result['sessions'] == []
        assert result['unavailable'][0]['candidate_id'] == 'broken'
    finally:
        service.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize('mutation', ['grow', 'replace'])
def test_reader_detects_file_change_during_read(tmp_path, monkeypatch, mutation):
    import os
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'before'}], session_id='changing', activate=False, model='test')
        original_fstat = os.fstat
        calls = 0
        def mutate_after_read(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                if mutation == 'grow':
                    with path.open('ab') as handle:
                        handle.write(b' ')
                else:
                    replacement = path.with_suffix('.replacement')
                    replacement.write_bytes(path.read_bytes())
                    replacement.replace(path)
            return original_fstat(fd)
        monkeypatch.setattr(os, 'fstat', mutate_after_read)
        with pytest.raises(sessions.SessionReadError) as error:
            sessions.load_session('changing')
        assert error.value.kind == 'changed'


def test_strict_read_never_requests_more_than_budget_plus_one(tmp_path, monkeypatch):
    import os
    with scoped_workdir(tmp_path):
        sessions.save_session([{'role': 'user', 'content': 'test'}], session_id='limited', activate=False, model='test')
        monkeypatch.setattr(sessions, '_session_byte_limit', lambda: 16)
        read = os.read
        requested = []
        def counted(fd, count):
            requested.append(count)
            return read(fd, count)
        monkeypatch.setattr(os, 'read', counted)
        with pytest.raises(sessions.SessionReadError):
            sessions.load_session('limited')
        assert requested and sum(requested) <= 17


def test_legacy_store_bad_parts_report_corrupt_without_rewrite(tmp_path):
    import asyncio
    import json
    from nz_coder.runtime.session.model import SessionIdentity
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'good'}], session_id='badpart', activate=False, model='test')
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['messages'][0]['_nz_parts'] = 3
        path.write_text(json.dumps(payload), encoding='utf-8')
        original = path.read_bytes()
    with pytest.raises(sessions.SessionReadError) as error:
        asyncio.run(LegacyJsonSessionStore().load(SessionIdentity('badpart'), tmp_path))
    assert error.value.kind == 'corrupt'
    assert path.read_bytes() == original


def test_summary_window_info_and_unavailable_contract(tmp_path):
    import json
    import os
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'ok'}], session_id='valid', activate=False, model='test')
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload['parent_session_id'] = 'p' * 300
        too_long = path.parent / 'badparent.json'
        too_long.write_text(json.dumps(dict(payload, session_id='badparent')), encoding='utf-8')
        corrupt = path.parent / 'corrupt.json'
        corrupt.write_text('{bad', encoding='utf-8')
        os.utime(path, (1, 1))
    manager = SessionManager(workspace_roots=[tmp_path], max_saved_sessions_per_workspace=2)
    try:
        result = manager.list()
        assert len(result['sessions']) + len(result['unavailable']) == 2
        assert {e['candidate_id'] for e in result['unavailable']} == {'badparent', 'corrupt'}
        assert all(set(e) == {'candidate_id', 'workspace_id', 'code', 'size_bytes', 'limit_bytes'} for e in result['unavailable'])
        assert all(e['size_bytes'] is None and e['limit_bytes'] is None for e in result['unavailable'])
        assert manager.info('valid')['id'] == 'valid'
        assert manager._sessions == {}
    finally:
        manager.close()


def test_open_then_remove_does_not_leave_stale_candidate(tmp_path):
    with scoped_workdir(tmp_path):
        sessions.save_session([{'role': 'user', 'content': 'ok'}], session_id='remove-me', activate=False, model='test')
    manager = SessionManager(workspace_roots=[tmp_path])
    try:
        manager.get('remove-me')
        manager.remove('remove-me')
        assert manager.list() == {'sessions': [], 'unavailable': []}
    finally:
        manager.close()


@pytest.mark.parametrize('damage', ['wrong_id', 'workspace', 'parent', 'parts', 'empty'])
def test_shared_recovery_gate_rejects_bad_envelope_across_surfaces(tmp_path, damage):
    import asyncio
    import json
    from nz_coder.interface.timeline import session_options
    from nz_coder.runtime.session.model import SessionIdentity
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'safe'}], session_id='target', activate=False, model='test')
        sessions.save_session([{'role': 'user', 'content': 'good'}], session_id='good', activate=False, model='test')
        item = json.loads(path.read_text())
        if damage == 'wrong_id':
            item['session_id'] = 'other'
        elif damage == 'workspace':
            item['workspace'] = str(tmp_path / 'other')
        elif damage == 'parent':
            item['parent_session_id'] = '../escape'
        elif damage == 'parts':
            item['messages'][0]['_nz_parts'] = 3
        else:
            item = {}
        path.write_text(json.dumps(item))
        original = path.read_bytes()
        with pytest.raises(sessions.SessionReadError) as exc:
            sessions.load_session('target')
        assert exc.value.kind == 'corrupt'
        with pytest.raises(sessions.SessionReadError):
            asyncio.run(LegacyJsonSessionStore().load(SessionIdentity('target'), tmp_path))
        with pytest.raises(sessions.SessionReadError):
            sessions.save_session([], session_id='target', model='test')
        options = {option.session_id: option for option in session_options()}
        assert options['target'].unavailable == 'corrupt'
        assert not options['good'].unavailable
        assert path.read_bytes() == original
    manager = SessionManager(workspace_roots=[tmp_path])
    try:
        assert any(item['candidate_id'] == 'target' and item['code'] == 'corrupt' for item in manager.list()['unavailable'])
    finally:
        manager.close()


def test_local_selector_isolates_null_messages_and_bounds_metadata(tmp_path):
    import hashlib
    import json
    from nz_coder.interface.timeline import session_options
    with scoped_workdir(tmp_path):
        bad = sessions.save_session([], session_id='bad', activate=False, model='test')
        data = json.loads(bad.read_text())
        data['messages'] = None
        bad.write_text(json.dumps(data))
        good = sessions.save_session([], session_id='good', activate=False, model='test')
        data = json.loads(good.read_text())
        data.update(model='x' * 1_000_000, timestamp='y' * 1_000_000, mode='default')
        good.write_text(json.dumps(data))
        digest = hashlib.sha256(good.read_bytes()).hexdigest()
        options = {option.session_id: option for option in session_options()}
        assert options['bad'].unavailable == 'corrupt'
        assert len(options['good'].model) <= 160
        assert len(options['good'].timestamp) <= 32
        assert options['good'].mode == 'default'
        assert hashlib.sha256(good.read_bytes()).hexdigest() == digest


def test_session_error_public_projection_is_safe():
    from nz_coder.protocol.public_error import to_public_error
    error = sessions.SessionReadError('resource_limit', __import__('pathlib').Path('/private/secret'), detail='secret')
    public = to_public_error(error)
    assert public.code == 'resource_limit'
    assert 'secret' not in str(public.to_dict())


@pytest.mark.parametrize(('case', 'status', 'code'), [
    ('missing', 404, 'session_not_found'), ('changed', 409, 'changed'),
    ('ambiguous', 409, 'ambiguous'), ('resource_limit', 409, 'resource_limit'),
    ('corrupt', 422, 'corrupt'), ('unreadable', 403, 'unreadable'),
    ('second_unreadable', 409, 'ambiguous'), ('second_oversized', 409, 'ambiguous'),
])
def test_http_client_restore_error_matrix(tmp_path, monkeypatch, case, status, code):
    import os
    import threading
    from nz_coder.http_service import NZCoderClient, NZCoderHTTPError, SessionHTTPService
    first = tmp_path / 'first'
    first.mkdir()
    roots = [first]
    with scoped_workdir(first):
        path = sessions.save_session([{'role': 'user', 'content': 'ok'}], session_id='target', activate=False, model='test')
    if case in {'ambiguous', 'second_unreadable', 'second_oversized'}:
        second = tmp_path / 'second'
        second.mkdir()
        roots.append(second)
        with scoped_workdir(second):
            other = sessions.save_session([{'role': 'user', 'content': 'other'}], session_id='target', activate=False, model='test')
        if case == 'second_oversized':
            import json
            payload = json.loads(other.read_text(encoding='utf-8'))
            payload['messages'][0]['content'] = '界' * 4096
            other.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            limit = path.stat().st_size
            assert path.stat().st_size <= limit < other.stat().st_size
            monkeypatch.setattr(sessions, '_session_byte_limit', lambda: limit)
            assert sessions._read_session_payload(path)['session_id'] == 'target'
            with pytest.raises(sessions.SessionReadError) as error:
                sessions._read_session_payload(other)
            assert error.value.kind == 'resource_limit'
        if case == 'second_unreadable':
            open_file = os.open
            def deny_other(candidate, *args, **kwargs):
                if candidate == other:
                    raise PermissionError('injected')
                return open_file(candidate, *args, **kwargs)
            monkeypatch.setattr(os, 'open', deny_other)
    if case == 'missing':
        path.unlink()
    if case == 'corrupt':
        path.write_bytes(b'{broken')
    manager = SessionManager(workspace_roots=roots)
    if case == 'resource_limit':
        monkeypatch.setattr(sessions, '_session_byte_limit', lambda: 1)
    if case == 'unreadable':
        open_file = os.open
        def deny(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError('injected')
            return open_file(candidate, *args, **kwargs)
        monkeypatch.setattr(os, 'open', deny)
    if case == 'changed':
        read = os.read
        changed = False
        def mutate(fd, size):
            nonlocal changed
            raw = read(fd, size)
            if not changed and os.fstat(fd).st_ino == path.stat().st_ino:
                changed = True
                with path.open('ab') as target:
                    target.write(b' ')
            return raw
        monkeypatch.setattr(os, 'read', mutate)
    service = SessionHTTPService(port=0, manager=manager, token='offline-test-token-1234')
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token)
        with pytest.raises(NZCoderHTTPError) as error:
            client.get_session('target')
        assert (error.value.status, error.value.code) == (status, code)
    finally:
        service.shutdown()
        thread.join(timeout=5)


def test_large_history_fork_preserves_each_prefix_and_snapshots(tmp_path):
    from tests.test_http_service import _fake_factory, _fork_history_with_snapshots
    from nz_coder.protocol.message_schema import SUMMARY_KEY, SESSION_SUMMARY_KEY
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False)
    parent_id = manager.create(workspace_id=manager.workspaces.id_for(tmp_path))['id']
    original_store, _, history, snapshots = _fork_history_with_snapshots(tmp_path, parent_id)
    history[0]['content'] = 'a' * (17 * 1024 * 1024)
    from nz_coder.protocol.message_schema import ensure_message_identities
    ensure_message_identities(history, parent_id)
    expected_diffs = [original_store.diff_full(start, end) for start, end in snapshots]
    with scoped_workdir(tmp_path):
        path = sessions.save_session(history, session_id=parent_id, activate=False, model='test')
    assert path.stat().st_size > 16 * 1024 * 1024
    manager.close()
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path])
    try:
        parent = manager.get(parent_id)
        assert parent.history == history
        for turn in (1, 2, 3):
            child_id = manager.fork(parent_id, turn)['id']
            child = manager.get(child_id)
            with scoped_workdir(tmp_path):
                child_store = WorkspaceSnapshotStore(tmp_path, sessions.session_snapshot_dir(child_id))
            assert child.history[0]['content'] == history[0]['content']
            for i, (start, end) in enumerate(snapshots[:turn]):
                assert child_store.diff_full(start, end) == expected_diffs[i]
                assert child.history[2*i][SUMMARY_KEY] == history[2*i][SUMMARY_KEY]
                assert child.history[2*i+1][SESSION_SUMMARY_KEY] == history[2*i+1][SESSION_SUMMARY_KEY]
                assert [part['type'] for part in child.history[2*i+1]['_nz_parts']] == [part['type'] for part in history[2*i+1]['_nz_parts']]
    finally:
        manager.close()


def test_sdk_open_reports_resource_limit_without_overwriting_history(tmp_path, monkeypatch):
    import asyncio
    from nz_coder.protocol.public_error import to_public_error
    from nz_coder.runtime.core.profiles import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest
    from nz_coder.sdk import AgentClient
    from tests.runtime.test_native_product_runtime import _install_runtime
    _install_runtime(monkeypatch, object())
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'retain'}], session_id='sdk-budget', activate=False, model='test')
        original = path.read_bytes()
    monkeypatch.setattr(sessions, '_session_byte_limit', lambda: 1)
    request = RunRequest(agent=AgentDefinition(name='sdk', instructions='offline'), profile=MAIN_PROFILE,
                         messages=[{'role': 'user', 'content': 'continue'}], workspace=tmp_path,
                         session_id='sdk-budget', stream=False)
    with pytest.raises(sessions.SessionReadError) as error:
        asyncio.run(AgentClient().run(request))
    assert to_public_error(error.value).code == 'resource_limit'
    assert str(path) not in str(to_public_error(error.value).to_dict())
    assert path.read_bytes() == original


def test_writer_utf8_budget_boundary_preserves_diff_and_aliases(tmp_path, monkeypatch):
    import hashlib
    from nz_coder.protocol.message_schema import SESSION_SUMMARY_KEY
    monkeypatch.setattr(sessions.time, 'strftime', lambda *_args: '2026-11-01 00:00:00')
    with scoped_workdir(tmp_path):
        summary = {'additions': 1, 'deletions': 1, 'files': 1, 'diffs': [{'file': 'a.py', 'patch': '-old\n+new', 'additions': 1, 'deletions': 1, 'status': 'modified'}]}
        old = [{'role': 'user', 'content': 'small'}, {'role': 'assistant', 'content': 'done', SESSION_SUMMARY_KEY: summary}]
        path = sessions.save_session(old, session_id='budget-check', model='offline')
        root = sessions.session_dir()
        previous = {p: p.read_bytes() for p in root.rglob('*.json')}
        target = [{'role': 'user', 'content': '界' * 1000}, {'role': 'assistant', 'content': 'done', SESSION_SUMMARY_KEY: summary}]
        sessions.save_session(target, session_id='budget-check', model='offline')
        size = path.stat().st_size
        for offset in (-1, 0, 1):
            for file, data in previous.items():
                file.write_bytes(data)
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json')}
            monkeypatch.setattr(sessions, '_session_byte_limit', lambda: size + offset)
            if offset == -1:
                with pytest.raises(sessions.SessionReadError) as error:
                    sessions.save_session(target, session_id='budget-check', model='offline')
                assert error.value.kind == 'resource_limit'
                assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json')} == before
            else:
                sessions.save_session(target, session_id='budget-check', model='offline')
                assert path.stat().st_size == size
                assert path.read_bytes() == sessions._latest_path().read_bytes() == sessions._active_path().read_bytes()


def test_active_alias_cannot_bind_other_session_body(tmp_path):
    import json
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'target'}], session_id='target', model='test')
        alias = sessions._active_path()
        original = alias.read_bytes()
        payload = json.loads(path.read_text())
        payload['session_id'] = 'other'
        path.write_text(json.dumps(payload))
        for operation in (lambda: sessions.load_session('active'), lambda: sessions.activate_session('target')):
            with pytest.raises(sessions.SessionReadError) as error:
                operation()
            assert error.value.kind == 'corrupt'
        assert alias.read_bytes() == original


@pytest.mark.parametrize("bad_metadata", [[], None])
def test_local_selector_isolates_bad_metadata(tmp_path, bad_metadata):
    import json
    from nz_coder.interface.timeline import session_options
    with scoped_workdir(tmp_path):
        bad = sessions.save_session([], session_id='badmeta', activate=False, model='test')
        payload = json.loads(bad.read_text())
        payload['metadata'] = bad_metadata
        bad.write_text(json.dumps(payload))
        options = {option.session_id: option for option in session_options()}
        assert options['badmeta'].unavailable == 'corrupt'


def test_session_writer_preserves_deep_json_and_normalizes_nonfinite(tmp_path):
    from nz_coder.state import sessions
    from nz_coder.state.workdir import scoped_workdir

    value = {"score": float("nan")}
    for _ in range(40):
        value = {"child": value}
    with scoped_workdir(tmp_path):
        sessions.save_session([{"role": "assistant", "content": "ok", "_extension": value}], session_id="deep", activate=False)
        restored = sessions.load_session("deep")["messages"][0]["_extension"]
    for _ in range(40):
        restored = restored["child"]
    assert restored == {"score": None}


def test_session_id_obeys_native_component_limit(tmp_path):
    import os
    from nz_coder.state import sessions
    from nz_coder.state.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        sessions.save_session([], session_id="ordinary", activate=False)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            root = ctypes.create_unicode_buffer(32768)
            assert ctypes.windll.kernel32.GetVolumePathNameW(str(sessions.session_dir()), root, len(root))
            max_component = wintypes.DWORD()
            assert ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, ctypes.byref(max_component), None, None, 0)
            # 字母数字的非 BMP 字符占两个 UTF-16 代码单元。
            char = "\U00010400"
            length = (max_component.value - len('.json')) // 2
        else:
            char = "a"
            length = os.pathconf(sessions.session_dir(), 'PC_NAME_MAX') - len('.json')
        boundary_id = char * length
        assert sessions._exact_session_id(boundary_id) == boundary_id
        path = sessions.save_session([], session_id=boundary_id, activate=False)
        assert path.name == boundary_id + ".json"
        assert sessions.load_session(boundary_id)["session_id"] == boundary_id
        with pytest.raises(ValueError, match="exact non-reserved"):
            sessions._exact_session_id(char * (length + 1))


def test_excessive_writer_depth_preserves_existing_session_and_aliases(tmp_path):
    from nz_coder.state import sessions
    from nz_coder.state.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        path = sessions.save_session([], session_id='depth-guard', model='offline')
        paths = [path, sessions._latest_path(), sessions._active_path()]
        before = [item.read_bytes() for item in paths]
        value = 'leaf'
        for _ in range(205):
            value = {'child': value}
        with pytest.raises(ValueError, match='deeply nested'):
            sessions.save_session([{'role': 'assistant', 'content': 'ok', '_extension': value}],
                                  session_id='depth-guard', model='offline')
        assert [item.read_bytes() for item in paths] == before
