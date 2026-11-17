"""HTTP 收尾驱动免费验收：真实服务/AgentLoop，只有模型响应为确定性假实现。"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from scripts.delivery_smoke_driver import (
    BudgetExceeded,
    DeliverySmokeDriver,
    EvaluationBudget,
    ObservationError,
    file_request_allowed,
    input_sha256,
)


def _budget(
    path, *, new=True, limit=5, clock=time.monotonic, wall=10, boot="test-boot"
):
    return EvaluationBudget(
        path,
        wall_seconds=wall,
        model_start_limit=limit,
        new=new,
        clock=clock,
        boot_id=boot,
    )


def _attempt(root):
    return {"attempt_id": "a", "service_session_root": str(root)}


def _append(log, **fields):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stream:
        stream.write(json.dumps({"event": "model_call_start", **fields}) + "\n")


def test_budget_resume_counts_unfinished_starts_without_refund(tmp_path):
    root = tmp_path / "service"
    root.mkdir()
    log = root / "_artifacts/s/runtime/runs/main.jsonl"
    path = tmp_path / "budget.json"
    with _budget(path, limit=2) as budget:
        budget.register(_attempt(root))
        _append(log, purpose="coding")
        budget.scan()
        budget.scan()
        assert budget.state["model_start_count"] == 1
    _append(log, purpose="classifier", agent_type="child")
    with _budget(path, new=False, limit=2) as budget:
        budget.scan()
        assert budget.state["model_start_count"] == 2
        with pytest.raises(BudgetExceeded):
            budget.check()
        assert all(
            "model_call_finish" not in key for key in budget.state["counted_starts"]
        )
    with pytest.raises(ObservationError):
        _budget(path, new=False, limit=3)
    assert json.loads(path.read_text())["model_start_count"] == 2


@pytest.mark.parametrize("damage", ["truncate", "replace", "missing", "prefix"])
def test_budget_rejects_trace_identity_loss(tmp_path, damage):
    root = tmp_path / "service"
    root.mkdir()
    log = root / "runtime/runs/main.jsonl"
    path = tmp_path / "budget.json"
    with _budget(path) as budget:
        budget.register(_attempt(root))
        _append(log)
        budget.scan()
    if damage == "truncate":
        log.write_text("")
    elif damage == "missing":
        log.unlink()
    elif damage == "replace":
        new = log.with_suffix(".new")
        new.write_bytes(log.read_bytes())
        new.replace(log)
    else:
        log.write_text(log.read_text().replace("model_call_start", "model_call_other"))
    with pytest.raises(ObservationError):
        with _budget(path, new=False) as budget:
            budget.scan()
    assert json.loads(path.read_text())["model_start_count"] == 1


def test_budget_keeps_monotonic_deadline_and_stable_lock(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    now = [100.0]
    with _budget(path, clock=lambda: now[0]) as budget:
        budget.save()
        with pytest.raises(BlockingIOError):
            _budget(path, new=False)
        monkeypatch.setattr(time, "time", lambda: -1e9)
        now[0] = 111
        with pytest.raises(BudgetExceeded):
            budget.check()
    with pytest.raises(ObservationError):
        _budget(path, new=False, boot="different-boot")
    path.write_text("{corrupt")
    with pytest.raises(ValueError):
        _budget(path, new=False)
    with pytest.raises(FileExistsError):
        _budget(path)
    with pytest.raises(FileNotFoundError):
        _budget(tmp_path / "missing", new=False)


def test_history_watermark_excludes_old_calls_and_covers_new_child_logs(tmp_path):
    log = tmp_path / "runtime/runs/history.jsonl"
    _append(log)
    with _budget(tmp_path / "state") as budget:
        budget.register(_attempt(tmp_path))
        budget.scan()
        assert budget.state["model_start_count"] == 0
        _append(log)
        _append(
            tmp_path / "_artifacts/child/runtime/runs/child.jsonl", purpose="summary"
        )
        budget.scan()
        budget.scan()
        assert budget.state["model_start_count"] == 2


def test_parent_and_child_production_calls_count_once_with_history_and_resume(tmp_path):
    root = tmp_path / "service"
    parent = root / "_artifacts/parent/runtime/runs/main.jsonl"
    child = root / "_artifacts/parent/subagents/child-a/trace/main.jsonl"
    _append(child)  # 登记前的子调用属于历史，不可重新计入。
    path = tmp_path / "budget.json"
    with _budget(path, limit=30) as budget:
        budget.register(_attempt(root))
        for _ in range(8):
            _append(parent)
        for _ in range(2):
            _append(child)
            _append(root / "_artifacts/parent/subagents/child-b/trace/main.jsonl")
        _append(root / "fixtures/trace/fake.jsonl")
        _append(root / "fixtures/runtime/fake/runs/fake.jsonl")
        budget.scan()
        budget.scan()
        assert budget.state["model_start_count"] == 12
    with _budget(path, new=False, limit=30) as budget:
        _append(child)
        _append(root / "_artifacts/another-session/runtime/runs/new.jsonl")
        budget.scan()
        assert budget.state["model_start_count"] == 14


@pytest.mark.parametrize("at_register", [True, False])
@pytest.mark.parametrize("directory", [False, True])
def test_budget_rejects_child_trace_symlink_escape_before_reading(tmp_path, at_register, directory):
    root = tmp_path / "service"
    root.mkdir()
    outside = tmp_path / "outside" if directory else tmp_path / "outside.jsonl"
    _append(outside / "main.jsonl" if directory else outside)
    link = root / "_artifacts/parent/subagents/child/trace"
    if not directory:
        link = link / "main.jsonl"
    link.parent.mkdir(parents=True)
    with _budget(tmp_path / "budget") as budget:
        if not at_register:
            budget.register(_attempt(root))
        link.symlink_to(outside, target_is_directory=directory)
        with pytest.raises(ObservationError, match="escapes service root"):
            if at_register:
                budget.register(_attempt(root))
            else:
                budget.scan()


def test_file_policy_rejects_readonly_escape_and_unknown_tools(tmp_path):
    policy = {
        "write_roots": ["src"],
        "write_files": ["README.md"],
        "read_only": ["src/spec.md"],
    }
    assert file_request_allowed(tmp_path, "write_file", {"path": "src/new.txt"}, policy)
    for tool, path in (
        ("write_file", "src/spec.md"),
        ("write_file", "../outside"),
        ("write_file", "/tmp/out"),
        ("bash", "src/new.txt"),
        ("write_file", "extra.py"),
    ):
        assert not file_request_allowed(tmp_path, tool, {"path": path}, policy)
    (tmp_path / "src").symlink_to(tmp_path.parent)
    assert not file_request_allowed(tmp_path, "write_file", {"path": "src/out"}, policy)
    assert input_sha256("bash", {"command": "x", "workdir": "a"}) != input_sha256(
        "bash", {"command": "x", "workdir": "b"}
    )


@pytest.mark.parametrize("tool, data", [
    ("write_files_batch", {"files": [{"path": "src/a.py", "content": "a=1\n"}, {"path": "src/b.py", "content": "b=2\n"}]}),
    ("apply_patch", {"path": "src/a.py", "changes": [{"old_text": "a=1", "new_text": "a=2"}]}),
    ("apply_patch", {"changes": [{"op": "create", "path": "src/a.py", "content": "a=1\n"}, {"op": "delete", "path": "src/old.py"}]}),
])
def test_file_policy_authorizes_equivalent_local_file_tools(tmp_path, tool, data):
    assert file_request_allowed(tmp_path, tool, data, {"write_roots": ["src"], "read_only": ["src/spec.md"], "delete_files": ["src/old.py"]})


@pytest.mark.parametrize("tool, data", [
    ("write_files_batch", {"files": [{"path": "src/a.py", "content": "x"}, {"path": "src/spec.md", "content": "x"}]}),
    ("write_files_batch", {"files": [{"path": "src/a.py", "content": "x"}, {"content": "unknown target"}]}),
    ("apply_patch", {"changes": [{"op": "delete", "path": "src/a.py"}, {"op": "create", "path": "../outside.py", "content": "x"}]}),
    ("apply_patch", {"path": "src/a.py", "changes": [{"op": "rename", "destination_path": "src/b.py"}]}),
    ("apply_patch", {"changes": [{"new_text": "unknown target"}]}),
    ("scaffold_project", {"path": "src", "template": "unknown footprint"}),
    ("apply_patch", {"changes": [{"op": "delete", "path": "src/a.py"}]}),
])
def test_file_policy_requires_all_real_targets_and_known_operations(tmp_path, tool, data):
    assert not file_request_allowed(tmp_path, tool, data, {"write_roots": ["src"], "read_only": ["src/spec.md"]})


@contextmanager
def _service(tmp_path, monkeypatch, batches):
    from nz_coder.foundation import config
    from nz_coder.http_service import SessionHTTPService, SessionManager, NZCoderClient
    from nz_coder.providers.normalized import chunk
    from nz_coder.state.sessions import session_dir
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "service-state"))
    with scoped_workdir(workspace):
        root = session_dir()
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "PLANNING_ENABLED", False)
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(tmp_path / "user.env"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))
    real_connect = socket.socket.connect

    def local_only(sock, address):
        assert isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"), (
            "external network forbidden"
        )
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    requests = []

    class Completions:
        def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            batch = batches.pop(0) if batches else "done"
            if isinstance(batch, str):
                return iter([chunk(content=batch, finish_reason="stop")])
            return iter(
                [
                    chunk(
                        tool_calls=[
                            {
                                "index": i,
                                "id": f"tool-{len(requests)}-{i}",
                                "name": name,
                                "arguments": json.dumps(payload),
                            }
                            for i, (name, payload) in enumerate(batch)
                        ]
                    ),
                    chunk(finish_reason="tool_calls"),
                ]
            )

    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.providers.capabilities import ModelCapabilities

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            return Completions().create(**kwargs)

    def resolve(*a, **kw):
        return ResolvedModelRuntime(
            provider_id="offline",
            model_id="smoke",
            request_model_id="smoke",
            variant=None,
            provider=Provider(),
            client=SimpleNamespace(close=lambda: None),
            capabilities=ModelCapabilities(provider="offline", model_id="smoke"),
            owns_client=True,
        )

    for target in (
        "nz_coder.runtime.execution.native_sdk.resolve_model_runtime",
        "nz_coder.runtime.execution.loop.resolve_model_runtime",
        "nz_coder.runtime.agent.agent_role_runtime.resolve_model_runtime",
        "nz_coder.runtime.model_gateway.resolve_model_runtime",
    ):
        monkeypatch.setattr(target, resolve)
    factory = None
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    service = SessionHTTPService(
        port=0, token="offline-token-123456789", manager=manager, heartbeat_seconds=0.05
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    client = NZCoderClient(service.base_url, service.token, timeout=0.3)
    wid = next(
        item["id"]
        for item in client.list_workspaces()
        if item["path"] == str(workspace)
    )
    try:
        yield SimpleNamespace(
            client=client,
            manager=manager,
            root=root,
            workspace=workspace,
            wid=wid,
            factory=factory,
            requests=requests,
            scoped_workdir=scoped_workdir,
        )
    finally:
        service.shutdown()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _driver(env, budget, tmp_path, **kwargs):
    return DeliverySmokeDriver(
        env.client,
        budget,
        workspace_id=env.wid,
        service_session_root=kwargs.pop("root", env.root),
        evidence_root=tmp_path / "evidence",
        file_policy=kwargs.pop("policy", {"write_files": ["out.txt"]}),
        **kwargs,
    )


def test_real_http_agent_tools_settle_and_restore_persisted_answer(
    tmp_path, monkeypatch
):
    from nz_coder.http_service import SessionManager

    batches = [
        [("write_file", {"path": "out.txt", "content": "actual side effect"})],
        "Finished out.txt",
    ]
    with _service(tmp_path, monkeypatch, batches) as env:
        with _budget(tmp_path / "budget") as budget:
            driver = _driver(env, budget, tmp_path)
            result = driver.run(
                prompt="Write out.txt with the requested text, then report completion."
            )
            assert result["status"] == "completed", result
            assert (env.workspace / "out.txt").read_text() == "actual side effect"
            assert budget.state["model_start_count"] == 2
            assert len(budget.state["consumed_approvals"]) == 1
            sid = result["session_id"]
            assert env.client.attach_snapshot(sid)["events"] == []
            settled = [
                e
                for e in driver.attempt["events"]
                if e["type"] == "session.run.settled"
            ]
            assert (
                settled[-1]["meta"]["interaction_run_id"]
                == result["interaction_run_id"]
            )
            trace_ids = {
                json.loads(line)["run_id"]
                for p in env.root.rglob("*.jsonl")
                if p.parent.name == "runs"
                for line in p.read_text().splitlines()
            }
            assert result["interaction_run_id"] not in trace_ids
        env.manager.close()
        restored = SessionManager(
            agent_factory=env.factory,
            workspace_roots=[env.workspace],
            restore_saved=True,
        )
        try:
            assert restored.info(sid)["last_result"]["answer"] == result["answer"]
            assert (
                restored.info(sid)["active_interaction_run_id"]
                == result["interaction_run_id"]
            )
        finally:
            restored.close()


def test_fast_completion_uses_sse_even_when_idle_attach_events_empty(
    tmp_path, monkeypatch
):
    with (
        _service(tmp_path, monkeypatch, ["Fast answer"]) as env,
        _budget(tmp_path / "budget") as budget,
    ):
        driver = _driver(env, budget, tmp_path)
        original = env.client.run

        def slow_response(*a, **kw):
            accepted = original(*a, **kw)
            time.sleep(0.2)
            return accepted

        monkeypatch.setattr(env.client, "run", slow_response)
        result = driver.run(prompt="Say hello without tools.")
        assert result["status"] == "completed", result
        assert budget.state["model_start_count"] == len(env.requests) == 2


def test_wrong_service_root_stops_before_post(tmp_path, monkeypatch):
    with (
        _service(tmp_path, monkeypatch, ["must not run"]) as env,
        _budget(tmp_path / "budget") as budget,
    ):
        wrong = tmp_path / "driver-xdg"
        wrong.mkdir()
        result = _driver(env, budget, tmp_path, root=wrong).run(prompt="Say hello")
        assert result["status"] == "unconfirmed"
        assert not env.requests


def test_submission_response_loss_resume_never_reposts(tmp_path, monkeypatch):
    path = tmp_path / "budget"
    with _service(tmp_path, monkeypatch, ["Actual accepted answer"]) as env:
        original = env.client.run
        posts = []

        def lost(*a, **kw):
            posts.append(a)
            original(*a, **kw)
            raise OSError("response lost")

        monkeypatch.setattr(env.client, "run", lost)
        with _budget(path) as budget:
            result = _driver(env, budget, tmp_path).run(prompt="Say hello")
            assert result["status"] == "unconfirmed"
            assert budget.state["attempts"][0]["submission_inflight"]
        with _budget(path, new=False) as budget:
            result = _driver(env, budget, tmp_path).run(resume=True)
            assert result["status"] == "unconfirmed"
        assert len(posts) == 1


def test_three_requests_use_live_ids_before_batch_tail_persistence(
    tmp_path, monkeypatch
):
    batch = [
        ("write_file", {"path": name, "content": name})
        for name in ("a.txt", "b.txt", "c.txt")
    ]
    with (
        _service(tmp_path, monkeypatch, [batch, "done"]) as env,
        _budget(tmp_path / "budget", wall=1.2) as budget,
    ):
        seen = []
        original = env.client.attach_snapshot

        def capture(sid):
            snapshot = original(sid)
            pending = snapshot["run"]["pending"]["permissions"]
            if pending:
                seen.append(copy.deepcopy(snapshot))
            return snapshot

        monkeypatch.setattr(env.client, "attach_snapshot", capture)
        result = _driver(
            env, budget, tmp_path, policy={"write_files": ["a.txt", "b.txt"]}
        ).run(prompt="Write a.txt b.txt c.txt, then report.")
        assert result["status"] == "unconfirmed" and "deadline" in result["reason"], (
            result
        )
        assert [a["request_id"] for a in budget.state["consumed_approvals"]] == list(
            dict.fromkeys(a["request_id"] for a in budget.state["consumed_approvals"])
        )
        assert len(budget.state["consumed_approvals"]) == 2
        third = next(
            s
            for s in seen
            if s["run"]["pending"]["permissions"][0]["tool_input"]["path"] == "c.txt"
        )
        assert not (env.workspace / "c.txt").exists()
        assert third["session"]["running"]
        assert not any(
            p.get("type") == "tool" and p.get("state", {}).get("status") == "completed"
            for record in third["messages"]
            for p in record.get("parts", [])
        )


def _boundary_driver(env, budget, tmp_path):
    session = env.client.create_session("default", env.wid)
    driver = _driver(env, budget, tmp_path)
    driver.attempt = {
        **_attempt(env.root),
        "session_id": session["id"],
        "interaction_run_id": "current",
        "events": [],
        "evidence_root": str(driver.evidence),
        "workspace_id": env.wid,
    }
    budget.register(driver.attempt)
    driver.attempt = budget.state["attempts"][0]
    return driver, session


def test_cancelled_original_reply_cannot_approve_replacement(tmp_path, monkeypatch):
    with (
        _service(tmp_path, monkeypatch, []) as env,
        _budget(tmp_path / "budget") as budget,
    ):
        driver, session = _boundary_driver(env, budget, tmp_path)
        broker = env.manager.get(session["id"]).interactions
        broker.begin_run()
        threads = []

        def ask():
            from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError
            try:
                broker.ask_permission("bash", {"command": "echo ok"})
            except PermissionInteractionError as exc:
                assert exc.reason == "cancelled"

        def start():
            thread = threading.Thread(
                target=ask, args=()
            )
            threads.append(thread)
            thread.start()
            deadline = time.monotonic() + 1
            while not broker.list("permission"):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            return broker.list("permission")[0]

        try:
            first = start()
            key = driver._approval_key(first, "current")
            approvals = tmp_path / "approvals"
            approvals.write_text(json.dumps([key]))
            driver.approvals_file = approvals
            driver.attempt["events"] = [
                {
                    "type": "permission.asked",
                    "meta": {"interaction_run_id": "current"},
                    "properties": first,
                }
            ]
            snapshot = {
                "session": session,
                "run": {"pending": {"permissions": [first]}},
            }
            broker.reply_permission(first["id"], "reject")
            threads[0].join(timeout=1)
            second = start()
            assert second["id"] != first["id"]
            driver._approve_pending(snapshot)
            assert broker.list("permission")[0]["id"] == second["id"]
            assert budget.state["consumed_approvals"] == [key]
            assert driver.attempt["replies"][-1]["replied"] is False
            # 同参数的新 nonce、其他交互或会话不能复用批准。
            driver.attempt["events"].append(
                {
                    "type": "permission.asked",
                    "meta": {"interaction_run_id": "current"},
                    "properties": second,
                }
            )
            driver._approve_pending(
                {"session": session, "run": {"pending": {"permissions": [second]}}}
            )
            assert broker.list("permission")[0]["id"] == second["id"]
        finally:
            broker.cancel_all()
            for thread in threads:
                thread.join(timeout=1)


def test_reply_loss_keeps_inflight_and_never_retries_on_resume(tmp_path, monkeypatch):
    batch = [
        ("write_file", {"path": "out.txt", "content": "x"}),
        ("write_file", {"path": "other.txt", "content": "y"}),
    ]
    with _service(tmp_path, monkeypatch, [batch, "done"]) as env:
        path = tmp_path / "budget"
        original = env.client.reply_permission
        replies = []

        def lost(*args, **kwargs):
            replies.append(args)
            original(*args, **kwargs)
            raise OSError("lost response")

        monkeypatch.setattr(env.client, "reply_permission", lost)
        with _budget(path) as budget:
            result = _driver(env, budget, tmp_path).run(
                prompt="Write out.txt and other.txt."
            )
            assert result["status"] == "unconfirmed", result
            assert len(budget.state["inflight_approvals"]) == 1
        with _budget(path, new=False) as budget:
            result = _driver(env, budget, tmp_path).run(resume=True)
            assert result["status"] == "unconfirmed"
        assert len(replies) == 1


@pytest.mark.parametrize(
    "field",
    [
        "persisted",
        "event_status",
        "empty_answer",
        "saved_interaction",
        "saved_status",
        "saved_answer",
    ],
)
def test_completed_requires_matching_current_persisted_result(
    tmp_path, monkeypatch, field
):
    with (
        _service(tmp_path, monkeypatch, ["Actual current answer"]) as env,
        _budget(tmp_path / "budget") as budget,
    ):
        driver = _driver(env, budget, tmp_path)
        assert driver.run(prompt="Say hello")["status"] == "completed"
        sid = driver.attempt["session_id"]
        snapshot = env.client.attach_snapshot(sid)
        settled = next(
            e for e in driver.attempt["events"] if e["type"] == "session.run.settled"
        )
        record_path = env.root / (sid + ".json")
        saved = json.loads(record_path.read_text())
        if field == "persisted":
            settled["properties"]["persisted"] = False
        elif field == "event_status":
            settled["properties"]["status"] = "completed_unverified"
        elif field == "empty_answer":
            snapshot["session"]["last_result"]["answer"] = ""
        elif field == "saved_interaction":
            saved["metadata"]["_nz_http_terminal_result"]["interaction_run_id"] = "old"
        elif field == "saved_status":
            saved["run_status"] = "cancelled"
        else:
            saved["metadata"]["_nz_http_terminal_result"]["last_result"]["answer"] = (
                "OLD"
            )
        record_path.write_text(json.dumps(saved))
        with pytest.raises(ObservationError):
            driver._completed(snapshot)


def test_real_service_two_starts_reach_shared_soft_limit(tmp_path, monkeypatch):
    batches = [[("write_file", {"path": "out.txt", "content": "x"})], "done"]
    with (
        _service(tmp_path, monkeypatch, batches) as env,
        _budget(tmp_path / "budget", limit=2) as budget,
    ):
        result = _driver(env, budget, tmp_path).run(prompt="Write out.txt and report.")
        assert (
            result["status"] == "unconfirmed"
            and "model-start limit" in result["reason"]
        ), result
        assert budget.state["model_start_count"] == 2
        assert result["external_cleanup"] == "settled"


def test_approval_requires_current_asked_source_and_exact_parameters(
    tmp_path, monkeypatch
):
    with (
        _service(tmp_path, monkeypatch, []) as env,
        _budget(tmp_path / "budget") as budget,
    ):
        driver, session = _boundary_driver(env, budget, tmp_path)
        request = {
            "id": "nonce-new",
            "session_id": session["id"],
            "permission": "write_file",
            "tool_input": {"path": "out.txt", "content": "x"},
        }
        snapshot = {"session": session, "run": {"pending": {"permissions": [request]}}}
        driver.attempt["events"] = [
            {
                "type": "permission.asked",
                "meta": {"interaction_run_id": "old"},
                "properties": request,
            }
        ]
        driver._approve_pending(snapshot)
        assert not budget.state["consumed_approvals"]
        original = copy.deepcopy(request)
        original["tool_input"]["content"] = "other"
        driver.attempt["events"] = [
            {
                "type": "permission.asked",
                "meta": {"interaction_run_id": "current"},
                "properties": original,
            }
        ]
        with pytest.raises(ObservationError, match="differ"):
            driver._approve_pending(snapshot)


def test_event_gap_stops_observation_without_approval(tmp_path, monkeypatch):
    with (
        _service(tmp_path, monkeypatch, ["done"]) as env,
        _budget(tmp_path / "budget") as budget,
    ):

        def gap(*args, **kwargs):
            assert kwargs["replay"] == 0 and kwargs["last_event_id"]
            yield {"type": "server.event_gap", "properties": {}}

        monkeypatch.setattr(env.client, "events", gap)
        result = _driver(env, budget, tmp_path).run(prompt="Say hello")
        assert result["status"] == "unconfirmed" and "gap" in result["reason"], result
        assert not budget.state["consumed_approvals"]
        assert not any(t.name == "delivery-smoke-events" for t in threading.enumerate())


def test_failed_scan_persists_already_observed_starts(tmp_path):
    first = tmp_path / "runtime/runs/a.jsonl"
    later = tmp_path / "runtime/runs/z.jsonl"
    _append(first)
    _append(later)
    path = tmp_path / "budget"
    with _budget(path) as budget:
        budget.register(_attempt(tmp_path))
        _append(first)
        later.write_text("")
        with pytest.raises(ObservationError):
            budget.scan()
        assert budget.state["model_start_count"] == 1
        assert json.loads(path.read_text())["model_start_count"] == 1


def test_resume_rejects_infinite_monotonic_deadline(tmp_path):
    path = tmp_path / "budget"
    with _budget(path):
        pass
    state = json.loads(path.read_text())
    state["created_monotonic"] = "OVERFLOW"
    state["deadline_monotonic"] = "OVERFLOW"
    path.write_text(json.dumps(state).replace('"OVERFLOW"', "1e999"))
    with pytest.raises((ValueError, ObservationError)):
        _budget(path, new=False)


def test_expired_resume_aborts_only_registered_owned_session(tmp_path, monkeypatch):
    batch = [("write_file", {"path": "unknown.txt", "content": "x"})]
    with (
        _service(tmp_path, monkeypatch, [batch, "done"]) as env,
        _budget(tmp_path / "budget") as budget,
    ):
        driver = _driver(env, budget, tmp_path)
        session = env.client.create_session("default", env.wid)
        snapshot = env.client.attach_snapshot(session["id"])
        attempt = {
            **_attempt(env.root),
            "session_id": session["id"],
            "interaction_run_id": "pending",
            "evidence_root": str(driver.evidence),
            "workspace_id": env.wid,
            "base_url": env.client.base_url,
            "cursor": snapshot["cursor"]["event_id"],
            "events": [],
            "pre_submit_sequence": snapshot["cursor"]["sequence"],
        }
        budget.register(attempt)
        attempt = budget.state["attempts"][0]
        attempt["interaction_run_id"] = env.client.run(
            session["id"], "Write unknown.txt"
        )["active_interaction_run_id"]
        budget.save()
        budget.clock = lambda: budget.state["deadline_monotonic"] + 1
        result = driver.run(resume=True)
        assert result["status"] == "unconfirmed" and "deadline" in result["reason"], (
            result
        )
        assert result["external_cleanup"] == "settled"
        assert not env.client.get_session(session["id"])["running"]


def test_resume_damaged_trace_still_aborts_and_preserves_cleanup_evidence(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    batch = [("write_file", {"path": "unknown.txt", "content": "x"})]
    with _service(tmp_path, monkeypatch, [batch, "done"]) as env:
        path = tmp_path / "budget"
        with _budget(path) as budget:
            driver = _driver(env, budget, tmp_path)
            session = env.client.create_session("default", env.wid)
            snapshot = env.client.attach_snapshot(session["id"])
            attempt = {
                **_attempt(env.root),
                "session_id": session["id"],
                "interaction_run_id": "pending",
                "evidence_root": str(driver.evidence),
                "workspace_id": env.wid,
                "base_url": env.client.base_url,
                "cursor": snapshot["cursor"]["event_id"],
                "events": [],
                "pre_submit_sequence": snapshot["cursor"]["sequence"],
            }
            budget.register(attempt)
            attempt = budget.state["attempts"][0]
            attempt["interaction_run_id"] = env.client.run(
                session["id"], "Write unknown.txt"
            )["active_interaction_run_id"]
            deadline = time.monotonic() + 2
            while not env.client.pending_permissions(session["id"]):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            budget.scan()
            log = next(p for p in env.root.rglob("*.jsonl") if p.parent.name == "runs")
            log.write_text("")
        abort = Mock(wraps=env.client.abort)
        monkeypatch.setattr(env.client, "abort", abort)
        with _budget(path, new=False) as budget:
            result = _driver(env, budget, tmp_path).run(resume=True)
            assert budget.state["model_start_count"] == 1
        assert result["status"] == "unconfirmed"
        assert result["session_id"] == session["id"]
        assert result["external_cleanup"] == "settled"
        abort.assert_called_once_with(session["id"])
        assert json.loads((tmp_path / "evidence/result.json").read_text()) == result


@pytest.mark.parametrize('tool', ['write_file', 'write_files_batch', 'apply_patch'])
@pytest.mark.parametrize('path', ['src/spec.md ', 'src\\spec.md'])
def test_file_policy_cannot_bypass_readonly_with_alternate_spelling(tmp_path, tool, path):
    data = {'path': path}
    if tool == 'write_files_batch':
        data = {'files': [{'path': path, 'content': 'changed'}], 'overwrite': True}
    if tool == 'apply_patch':
        data = {'changes': [{'path': path, 'old_text': 'original', 'new_text': 'changed'}]}
    assert not file_request_allowed(tmp_path, tool, data, {'write_roots': ['src'], 'read_only': ['src/spec.md']})


def test_patch_delete_authorization_follows_actual_inherited_path(tmp_path):
    data = {'path': 'src/a.py', 'changes': [
        {'op': 'append', 'path': 'src/b.py', 'content': 'x'}, {'op': 'delete'},
    ]}
    assert not file_request_allowed(tmp_path, 'apply_patch', data,
                                   {'write_roots': ['src'], 'delete_files': ['src/a.py']})
    assert file_request_allowed(tmp_path, 'apply_patch', data,
                                {'write_roots': ['src'], 'delete_files': ['src/b.py']})


@pytest.mark.parametrize('value', [None, False, 0, [], {}])
def test_patch_explicit_nonstring_target_cannot_fall_back_to_approved_path(tmp_path, value):
    assert not file_request_allowed(tmp_path, 'apply_patch', {
        'path': 'src/a.py', 'changes': [{'op': 'create', 'path': value, 'content': 'x'}],
    }, {'write_roots': ['src']})
