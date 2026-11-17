"""撤销必须删除原启动候选的授权记录，而非重算当前路径身份。"""
from contextlib import contextmanager
import json
import subprocess
import sys

import pytest

from nz_coder.foundation.execution_identity import verify_execution_identity
from nz_coder.foundation.workspace_trust import WorkspaceTrustStore
from nz_coder.lsp.servers import resolve_server, server_trust_kind, trust_server
from tests.test_lsp_launch_environment import _program, _snapshot, _source


def _candidate(tmp_path, monkeypatch, *, symlink_interpreter=False):
    workspace = tmp_path / "workspace"
    source = _source(workspace)
    script = _program(workspace / "server.py", "import time\nprint('READY', flush=True)\ntime.sleep(60)\n")
    interpreter = sys.executable
    if symlink_interpreter:
        link = workspace / "bin/python"
        link.parent.mkdir()
        link.symlink_to(sys.executable)
        interpreter = link
    store = tmp_path / "user/trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(store))
    snapshot = _snapshot(workspace, f"{interpreter} -u {script}")
    profile = trust_server(source, workspace, config_snapshot=snapshot)
    return workspace, source, snapshot, profile


@contextmanager
def _running(profile):
    verify_execution_identity(profile.execution_identity)
    process = subprocess.Popen(profile.command, cwd=profile.root, env=dict(profile.environment), stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "READY"
        assert process.poll() is None
        yield process
    finally:
        process.terminate()
        process.wait(timeout=3)
        process.stdout.close()


def _revoke(profile):
    assert hasattr(profile, "trust_record_key"), "the launch must capture the original trust record"
    assert hasattr(profile, "trust_store_path"), "the launch must capture the original store"
    return WorkspaceTrustStore(profile.trust_store_path).remove_record(profile.trust_record_key)


def test_revoke_captured_record_after_interpreter_symlink_redirect(tmp_path, monkeypatch):
    workspace, source, snapshot, profile = _candidate(tmp_path, monkeypatch, symlink_interpreter=True)
    with _running(profile) as process:
        link = workspace / "bin/python"
        replacement = _program(tmp_path / "replacement-python")
        link.unlink()
        link.symlink_to(replacement)
        try:
            assert _revoke(profile)
            assert process.poll() is None
        finally:
            link.unlink()
            link.symlink_to(sys.executable)
    assert not resolve_server(source, workspace, config_snapshot=snapshot).trusted


def test_revoke_captured_record_after_workspace_directory_replacement(tmp_path, monkeypatch):
    workspace, source, snapshot, profile = _candidate(tmp_path, monkeypatch)
    with _running(profile) as process:
        old = tmp_path / "old-workspace"
        workspace.rename(old)
        workspace.mkdir()
        try:
            assert _revoke(profile)
            assert process.poll() is None
        finally:
            workspace.rmdir()
            old.rename(workspace)
    assert not resolve_server(source, workspace, config_snapshot=snapshot).trusted


def test_revoke_uses_captured_store_after_ambient_store_path_changes(tmp_path, monkeypatch):
    workspace, source, snapshot, first = _candidate(tmp_path, monkeypatch)
    second_store = tmp_path / "other-user/trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(second_store))
    second = trust_server(source, workspace, config_snapshot=snapshot)
    assert _revoke(first)
    assert first.trust_record_key == second.trust_record_key
    assert first.trust_store_path != second.trust_store_path
    assert resolve_server(source, workspace, config_snapshot=snapshot).trusted
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(first.trust_store_path))
    assert not resolve_server(source, workspace, config_snapshot=snapshot).trusted


def test_scope_uses_captured_root_text_after_root_symlink_replacement(tmp_path, monkeypatch):
    workspace, source, _snapshot_value, profile = _candidate(tmp_path, monkeypatch)
    original_scope = server_trust_kind(source, profile)
    old = tmp_path / "old-workspace"
    workspace.rename(old)
    other = tmp_path / "other"
    other.mkdir()
    workspace.symlink_to(other, target_is_directory=True)
    try:
        assert server_trust_kind(source, profile) == original_scope
    finally:
        workspace.unlink()
        old.rename(workspace)


@pytest.mark.parametrize(("field", "value"), [("config_type", 123), ("executable", 456)])
def test_captured_record_check_preserves_strict_record_fields(tmp_path, field, value):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = WorkspaceTrustStore(tmp_path / "user/trust.json")
    key = store.capture_record_key(workspace, "123", executable="456")
    store.trust(workspace, "123", "approved", executable="456")
    payload = json.loads(store.path.read_text())
    payload["entries"][key][field] = value
    store.path.write_text(json.dumps(payload))
    assert not store.is_trusted_record(key, "approved")
