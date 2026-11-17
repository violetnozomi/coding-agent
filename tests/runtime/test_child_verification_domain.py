"""子结果携带域结论；后置条件不能篡改原始退出原因。"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nz_coder.runtime.agent import subagent
from nz_coder.runtime.execution.loop import ProductRunEnvironment
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools.files import bind_tool_state
from nz_coder.state.changes import ChangeTracker


@pytest.mark.parametrize("raw_status", ["error", "cancelled", "interrupted", "max_turns"])
def test_child_postcondition_keeps_administrative_status(monkeypatch, tmp_path, raw_status):
    async def run(_self, messages, **_kwargs):
        messages.append({"role": "assistant", "content": "short"})
        return {"status": raw_status, "authored_content": "short",
                "verification": {"domain_revision": 7, "tests_passed": False}}

    class LocalClient:
        def close(self):
            pass

    monkeypatch.setattr(subagent, "_parent_config_snapshot", lambda *_args: None)
    monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: LocalClient())
    monkeypatch.setattr(ProductRunEnvironment, "run", run)
    with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id="parent"):
        output = subagent.run_subagent("inspect", cancel_event=threading.Event(),
            verification={"enforcement": "warn", "min_final_text_chars": 100})
    child = output.metadata["child_result"]
    assert child["status"] == raw_status
    assert child["verification"] == {"domain_revision": 7, "tests_passed": False}
    assert child["postconditions"]["ok"] is False


def test_non_git_static_verification_consumes_bound_tracker(tmp_path):
    from nz_coder.tools.repo_intel import verify_changed_files

    (tmp_path / "app.py").write_text("def broken(:\n")
    tracker = ChangeTracker(change_dir=tmp_path / "isolated-owner")
    tracker.record_before("app.py", False)
    tracker.record_after("app.py", True, "def broken(:\n")
    with scoped_workdir(tmp_path), bind_tool_state(change_tracker=tracker):
        result = verify_changed_files()
    assert str(result).startswith("FAIL:"), str(result)
    assert "app.py" in result
    assert result.execution_facts == ()


def test_native_git_child_does_not_claim_an_unperformed_rollback(monkeypatch, tmp_path):
    import subprocess
    from nz_coder.foundation import config
    from tests.test_subagent import FakeClient, FakeMessage, FakeResponse, FakeToolCall

    (tmp_path / "app.py").write_text("VALUE = 0\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall("write_file", {"path": "app.py", "content": "def broken(\n"})])),
        FakeResponse(FakeMessage("patched")),
    ])
    monkeypatch.setattr(config, "SUBAGENT_MAX_TURNS", 2)
    monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(config, "REFLECTION_ENABLED", False)
    monkeypatch.setattr(config, "LSP_WRITE_DIAGNOSTICS_ENABLED", False)
    monkeypatch.setattr(subagent, "_parent_config_snapshot", lambda *_args: None)
    monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)
    with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id="parent"):
        output = subagent.run_subagent("patch app.py", agent_type="general-purpose", target_paths=["app.py"])
        child = output.metadata["child_result"]
        state = subagent._load_subagent_state("parent", child["session_id"], tmp_path)
    child_path = Path(state["worktree"]["path"]) / "app.py"
    assert child_path.read_text() == "def broken(\n"
    assert (tmp_path / "app.py").read_text() == "VALUE = 0\n"
    assert "rolled_back" not in child["status"]
    assert child["status"] != "completed"
    assert child["verification"]["domain_revision"] > 0


def test_late_child_stop_cannot_rewrite_frozen_execution(monkeypatch, tmp_path):
    from nz_coder.foundation import config
    from tests.test_subagent import FakeClient, FakeMessage, FakeResponse

    cancelled = threading.Event()
    original = ProductRunEnvironment.run
    async def run_and_stop(self, messages, **kwargs):
        result = await original(self, messages, **kwargs)
        assert self.verification_runtime.run_context.terminal_status.value == result["status"]
        cancelled.set()
        return result

    fake = FakeClient([FakeResponse(FakeMessage("The workspace is empty."))])
    monkeypatch.setattr(config, "SUBAGENT_MAX_TURNS", 2)
    monkeypatch.setattr(config, "REFLECTION_ENABLED", False)
    monkeypatch.setattr(subagent, "_parent_config_snapshot", lambda *_args: None)
    monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)
    monkeypatch.setattr(ProductRunEnvironment, "run", run_and_stop)
    with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id="parent"):
        output = subagent.run_subagent("Inspect the empty workspace.", cancel_event=cancelled)
    child = output.metadata["child_result"]
    assert child["status"] == "completed_unverified"
    assert child["verification"]["terminal_reason"] == "completed_unverified"
