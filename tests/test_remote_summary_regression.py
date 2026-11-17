"""最终审查：真实 Manager/HTTP 的自动附着摘要交接探针。"""
import argparse
import asyncio
import io
import threading

from rich.console import Console

from nz_coder.http_service import SessionHTTPService
from nz_coder.http_service.manager import SessionManager
from nz_coder.interface import remote
from nz_coder.state import sessions
from nz_coder.state.workdir import scoped_workdir


def test_auto_attach_retains_selected_workspace_and_metadata(tmp_path, monkeypatch):
    for key in ('XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path / key))
    workspace = tmp_path / 'server-project'
    workspace.mkdir()
    client_cwd = tmp_path / 'client-project'
    client_cwd.mkdir()
    monkeypatch.chdir(client_cwd)
    with scoped_workdir(workspace):
        sessions.save_session([{'role': 'user', 'content': 'previous'}], session_id='selected',
                              activate=False, model='offline/review-model', mode='plan')
    observed = []
    original_init = remote.TerminalInput.__init__

    def capture_init(self, *args, **kwargs):
        observed.append({'workspace': str(kwargs['workspace']), 'state': kwargs['state_provider']()})
        return original_init(self, *args, **kwargs)

    async def stop_ui(self):
        raise EOFError

    monkeypatch.setattr(remote.TerminalInput, '__init__', capture_init)
    monkeypatch.setattr(remote.TerminalInput, 'read_async', stop_ui)
    for sid in ('', 'selected'):
        # 两种路径各自使用冷启动管理器，避免首次附着改变后续结果。
        manager = SessionManager(workspace_roots=[workspace])
        service = SessionHTTPService(port=0, manager=manager, token='offline-review-token-1234')
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(remote, '_connection', lambda args: (service.base_url, service.token, '', True))
        try:
            args = argparse.Namespace(new=False, session_id=sid, workspace_id=None)
            asyncio.run(remote._attach(args, Console(file=io.StringIO())))
        finally:
            service.shutdown()
            thread.join(timeout=5)
    assert observed[1]['workspace'] == str(workspace)
    assert observed[1]['state']['model'] == 'offline/review-model'
    assert observed[0] == observed[1]
