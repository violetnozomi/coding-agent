"""HTTP 控制在原服务 owner 执行，不能跨工作区操纵 entry。"""

from tests.test_http_service import local_service  # noqa: F401 共享真实HTTP服务fixture
from tests.test_lsp import _registered_client
from types import SimpleNamespace
import pytest


def test_http_lsp_status_and_cross_workspace_control(local_service, tmp_path):  # noqa: F811
    from nz_coder.http_service.client import NZCoderHTTPError
    from nz_coder.lsp import manager
    from nz_coder.http_service.client import NZCoderClient

    service = local_service
    client = NZCoderClient(service.base_url, service.token)
    session = client.create_session()
    session_id = session["id"]
    other = tmp_path / "foreign"
    other.mkdir()
    entry = _registered_client(other, SimpleNamespace())
    try:
        status = client.lsp_status(session_id)
        assert status["entries"] == []
        with pytest.raises(NZCoderHTTPError):
            client.lsp_control(session_id, entry.entry_id, "restart")
        assert manager._ENTRIES[entry.key] is entry
    finally:
        manager._ENTRIES.pop(entry.key, None)


def test_remote_lsp_controls_forward_to_original_owner():
    from nz_coder.interface.backend import RemoteTerminalBackend

    class Client:
        def lsp_status(self, session_id):
            return {"owner": session_id}

        def lsp_control(self, session_id, entry_id, action):
            return {"owner": session_id, "entry_id": entry_id, "action": action}

    backend = RemoteTerminalBackend(Client(), "original")
    assert backend.lsp_status() == {"owner": "original"}
    assert backend.lsp_control("entry", "restart")["owner"] == "original"


def test_http_restart_from_separate_client_process_replaces_original_entry(
    local_service,  # noqa: F811
    monkeypatch,
):
    import json
    import subprocess
    import sys
    from pathlib import Path
    from nz_coder.http_service.client import NZCoderClient
    from nz_coder.lsp import manager
    from tests.test_lsp import _write_fake_server

    service = local_service
    remote = NZCoderClient(service.base_url, service.token)
    session_id = remote.create_session()["id"]
    workspace = service.manager.get(session_id).workspace
    (workspace / "app.py").write_text("answer=42")
    server = _write_fake_server(workspace)
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", f"{sys.executable} -u {server}")
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(workspace.parent / (workspace.name + "-lsp-trust.json")),
    )
    try:
        assert manager.get_client_for_file(workspace / "app.py", workspace) is None
        candidate = remote.lsp_status(session_id)["entries"][0]
        approved = remote.lsp_control(session_id, candidate["entry_id"], "trust")
        assert approved["status"] == "ready"
        original = manager.get_client_for_file(workspace / "app.py", workspace)
        program = (
            "import json,sys;from nz_coder.http_service.client import NZCoderClient;"
            'print(json.dumps(NZCoderClient(sys.argv[1],sys.argv[2]).lsp_control(sys.argv[3],sys.argv[4],"restart")))'
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                program,
                service.base_url,
                service.token,
                session_id,
                approved["entry_id"],
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        replacement = json.loads(result.stdout)
        assert replacement["status"] == "ready"
        assert replacement["entry_id"] != approved["entry_id"]
        assert original.process.poll() is not None
        current = manager.get_client_for_file(workspace / "app.py", workspace)
        assert current.process.pid != original.process.pid
        assert (
            remote.lsp_control(session_id, approved["entry_id"], "restart")["status"]
            == "no_active_service"
        )
    finally:
        manager.close_workspace_clients(workspace)


def test_untrust_still_reclaims_original_process_after_payload_changes(
    tmp_path, monkeypatch
):
    import sys
    from nz_coder.lsp import manager
    from nz_coder.lsp.servers import trust_server
    from tests.test_lsp import _write_fake_server

    source = tmp_path / "app.py"
    source.write_text("answer=42")
    server = _write_fake_server(tmp_path)
    monkeypatch.setenv("NZ_LSP_PYTHON_COMMAND", f"{sys.executable} {server}")
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path.parent / (tmp_path.name + "-trust.json")),
    )
    trust_server(source, tmp_path)
    original = manager.get_client_for_file(source, tmp_path)
    entry_id = manager.client_status_summary(tmp_path)[0]["entry_id"]
    server.write_text(server.read_text() + "\n# payload changed\n")
    try:
        assert manager.get_client_for_file(source, tmp_path) is None
        result = manager.control_client(tmp_path, entry_id, "untrust")
        assert result["status"] == "untrusted"
        assert original.process.poll() is not None
    finally:
        manager.close_workspace_clients(tmp_path)
