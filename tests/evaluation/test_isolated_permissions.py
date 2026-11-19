import time
from types import SimpleNamespace

import pytest


def test_offline_approval_binds_full_inputs_and_live_request(tmp_path):
    from nz_coder.evaluation.isolated_permissions import _reply_once, permission_wait_seconds
    from nz_coder.tool_platform.permissioning.auto_router import canonical_action_digest
    sub = tmp_path / "src"
    sub.mkdir()
    (tmp_path / "linked").symlink_to(sub)
    args = dict(command="python -m unittest", workdir="linked")
    grant = {"tool": "bash", "args": args, "cwd": str(sub)}
    start = time.time()
    def request(key, **changes):
        return {**dict(kind="permission", id=key, session_id="one", permission="bash", tool_input=args,
                       created_at=start - 2, expires_at=start + 20), **changes}
    pending = [request("real"), request("manual", tool_input={**args, "timeout": 12}),
               request("old", expires_at=start - 1), request("foreign", session_id="other"),
               request("q", kind="question")]
    replies = []
    client = SimpleNamespace(pending_permissions=lambda sid: pending,
        reply_permission=lambda *values, **kwargs: replies.append(values) or True,
        get_session=lambda sid: dict(id=sid, running=True, workspace=str(tmp_path), active_interaction_run_id="run"))
    allowed = {(canonical_action_digest("bash", args, tmp_path), grant["cwd"])}
    for req in pending:
        _reply_once(client, "one", "run", tmp_path, allowed, req["id"], "test-marker")
    assert replies == [("one", "real", "once")]
    assert permission_wait_seconds([(0, 3), (1, 2), (2, 5), (8, 10)]) == 7
    # 同一个工具参数经重定向后落在未经授权的目录。
    (tmp_path / "linked").unlink()
    (tmp_path / "linked").symlink_to(tmp_path)
    pending[:] = [request("redirected")]
    _reply_once(client, "one", "run", tmp_path, allowed, "redirected", "test-marker")
    assert len(replies) == 1


def test_container_preflight_rejects_original_writable_case_and_extra_mount(tmp_path):
    from nz_coder.evaluation.isolated_permissions import validate_container
    cid, image = 'a' * 64, 'sha256:' + 'b' * 64
    rw = dict(Type="bind", Source=str(tmp_path), Destination="/work", RW=True, Propagation="rprivate")
    cfg = dict(NetworkMode="none", ReadonlyRootfs=True, Privileged=False, CapDrop=["ALL"],
               SecurityOpt=["no-new-privileges"], Tmpfs={"/tmp": "rw"}, IpcMode="private")
    info = dict(Id=cid, Image=image, State=dict(Running=True), HostConfig=cfg, Config=dict(User="1000:1000"), Mounts=[rw])
    validate_container(info, cid, image, tmp_path, None)
    for changed in (dict(Destination="/case"), dict(Source="/home"), dict(Propagation="rshared")):
        with pytest.raises(ValueError):
            validate_container({**info, "Mounts":[{**rw, **changed}]}, cid, image, tmp_path, None)
    with pytest.raises(ValueError):
        validate_container({**info, "Mounts":[rw, {**rw, "RW":False, "Destination":"/ledger"}]}, cid, image, tmp_path, None)


def test_event_audit_includes_request_replied_before_observer_attached(tmp_path):
    from nz_coder.evaluation.isolated_permissions import monitor_run, _approval_marker
    now = time.time()
    def event(kind, ts, **data):
        return dict(type=kind, meta=dict(timestamp=ts, session_id="s", interaction_run_id="r"), properties=data)
    events = [
        {"type":"server.connected", "properties":{}},
        {"type":"server.heartbeat", "properties":{}},
        event("session.run.started", now),
        event("permission.asked", now+1, id="manual", created_at=now+1),
        event("permission.replied", now+3, request_id="manual", reply="once"),
        event("permission.asked", now+2, id="auto", created_at=now+2),
        event("permission.replied", now+4, request_id="auto", reply="once", message=_approval_marker("offline", "s", "r", "auto")),
        event("session.run.settled", now+5),
    ]
    request = dict(id="auto", kind="permission", session_id="s", permission="bash", tool_input={"command":"pwd"},
                   created_at=now, expires_at=now+10)
    client = SimpleNamespace(get_session=lambda _: dict(id="s", running=True, workspace=str(tmp_path), active_interaction_run_id="r"),
        events=lambda _sid, **options: iter(events) if options['last_event_id']=="start" else iter(()),
        pending_permissions=lambda _: [request], reply_permission=lambda *args, **kwargs: True)
    config = dict(session_id="s", run_id="r", start_cursor="start", token="offline",
                  operations=[dict(tool="bash", args={"command":"pwd"}, cwd=str(tmp_path))])
    result = monitor_run(client, config, tmp_path, lambda _event: None)
    assert result["permission_requests"] == 2 and result["automatically_approved"] == 1
    assert result["external_replies"] == 1 and result["user_clicks"] is None and result["evaluator_clicks"] is None
    assert result["permission_wait_seconds"] == 3 and result["automatic_wait_seconds"] == 2
    client.pending_permissions = lambda _: []
    assert monitor_run(client, config, tmp_path, lambda _event: None) == result
    with pytest.raises(ValueError):
        monitor_run(client, {**config, "run_id": "wrong"}, tmp_path, lambda _event: None)
    client.events = lambda *_a, **_kw: iter([{"type":"server.event_gap", "properties":{}}])
    with pytest.raises(ValueError, match="Gap"):
        monitor_run(client, config, tmp_path, lambda _event: None)
    client.get_session = lambda _: dict(id="s", running=False, workspace=str(tmp_path), active_interaction_run_id="r")
    client.events = lambda *_a, **_kw: iter([{"type":"server.heartbeat", "properties":{}}] * 2)
    with pytest.raises(ValueError, match="without a replayable settlement"):
        monitor_run(client, config, tmp_path, lambda _event: None)


def test_unknown_container_environment_refuses_automatic_approval(tmp_path):
    from nz_coder.evaluation.isolated_permissions import validate_container
    cid, image = "a"*64, "sha256:"+"b"*64
    cfg = dict(NetworkMode="none", ReadonlyRootfs=True, Privileged=False, CapDrop=["ALL"],
               SecurityOpt=["no-new-privileges"], Tmpfs={"/tmp":"rw"}, IpcMode="private")
    info = dict(Id=cid, Image=image, State=dict(Running=True), HostConfig=cfg,
        Config=dict(User="1234", Env=["EVALUATION_CONTROL=fixture-only"]),
        Mounts=[dict(Type="bind", Source=str(tmp_path), Destination="/work", RW=True, Propagation="rprivate")])
    with pytest.raises(ValueError):
        validate_container(info, cid, image, tmp_path, None)


def test_formal_http_approval_and_settled_replay(tmp_path, monkeypatch):
    import json
    import threading
    from pathlib import Path
    from nz_coder.evaluation.isolated_permissions import monitor_run
    from nz_coder.http_service import NZCoderClient, SessionHTTPService, SessionManager
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime

    cmd = "python -m unittest discover -s tests -v"
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_demo.py").write_text("import unittest\nclass Demo(unittest.TestCase):\n def test_ok(self): self.assertEqual(2,2)\n")
    class Provider:
        name = "offline"
        calls = 0
        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            if self.calls > 1:
                return _completion("The test ran.")
            reply = _completion(tool="bash")
            reply.choices[0].message.tool_calls[0].function.arguments = json.dumps({"command":cmd})
            return reply

    _install_runtime(monkeypatch, Provider())
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    service = SessionHTTPService(manager=manager, port=0, heartbeat_seconds=0.05, token="offline-isolated-test-token")
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        workspace = next(w["id"] for w in manager.list_workspaces() if Path(w["path"]) == tmp_path)
        sid = client.create_session("default", workspace_id=workspace)["id"]
        before = client.attach_snapshot(sid)["cursor"]["event_id"]
        client.run(sid, "Please run the project tests and report whether they pass.", model="offline/offline-model")
        packet = dict(session_id=sid, start_cursor=before, run_id=client.get_session(sid)["active_interaction_run_id"],
                      token=service.token, operations=[dict(tool="bash", cwd=str(tmp_path), args={"command":cmd})])
        events = []
        result = monitor_run(client, packet, tmp_path, events.append)
        assert len(events) == 1 and events[0]["event"] == "automatic_once"
        assert (result["permission_requests"], result["automatically_approved"], result["user_clicks"]) == (1, 1, 0)
        assert result["permission_wait_seconds"] == result["automatic_wait_seconds"]
        assert manager.get(sid).wait(timeout=3)
        assert monitor_run(client, packet, tmp_path, lambda _row: None) == result
        late = client.attach_snapshot(sid)["cursor"]["event_id"]
        with pytest.raises(ValueError, match="without a replayable settlement"):
            monitor_run(client, {**packet, "start_cursor":late}, tmp_path, lambda _row: None)
    finally:
        service.shutdown()
        thread.join(timeout=3)
