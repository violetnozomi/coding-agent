"""使用真实原子发布故障核对冻结结果，禁止重加用量或覆盖外部修改。"""

import asyncio
import json
from dataclasses import replace
import pytest

from nz_coder.runtime.core.result import RunStatus, TokenUsage
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from tests.runtime.test_native_product_runtime import _request


def opened(tmp_path):
    runtime = SessionRuntime(LegacyJsonSessionStore())
    request = replace(_request(tmp_path), interaction_run_id="original-interaction")
    context = asyncio.run(runtime.open(request))
    asyncio.run(runtime.checkpoint(context))
    context.session.append({"role": "assistant", "content": "original result"})
    context.add_usage(TokenUsage(input_tokens=7, output_tokens=3))
    with scoped_workdir(tmp_path):
        path = sessions.session_dir() / (request.session_id + ".json")
    return runtime, context, path


def fail_terminal(monkeypatch, *, after=False):
    original = sessions._publish_session_json
    attempts = []
    enabled = [True]

    def publish(prepared, path):
        data = json.loads(prepared.read_text())
        if path.name == "native-product.json" and data.get("run_status") == "completed":
            attempts.append(data)
            if after:
                original(prepared, path)
            if enabled[0]:
                raise OSError("offline disk failure")
            if after:
                return
        return original(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", publish)
    return enabled, attempts


@pytest.mark.parametrize("after", [False, True])
def test_retry_confirms_original_commit_without_recharging(
    monkeypatch, tmp_path, after
):
    runtime, context, path = opened(tmp_path)
    enabled, attempts = fail_terminal(monkeypatch, after=after)
    with pytest.raises(OSError):
        asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    assert context.terminal_status is RunStatus.COMPLETED
    assert not context.finalized and context.session.usage.total_tokens == 0
    enabled[0] = False
    asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    assert context.finalized and context.session.usage.total_tokens == 10
    data = json.loads(path.read_text())
    assert data["metadata"]["_nz_session_snapshot"]["usage"]["input_tokens"] == 7
    assert len(attempts) == (1 if after else 2)
    assert data["messages"][-1]["content"] == "original result"


@pytest.mark.parametrize(
    "change",
    ["text", "part", "metadata", "workspace", "model", "usage", "inner_timestamp"],
)
def test_external_update_never_overwritten(monkeypatch, tmp_path, change):
    from nz_coder.runtime.session.store import SessionCommitConflict

    runtime, context, path = opened(tmp_path)
    enabled, _ = fail_terminal(monkeypatch)
    with pytest.raises(OSError):
        asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    data = json.loads(path.read_text())
    if change == "text":
        data["messages"][0]["content"] = "external"
    elif change == "part":
        data["messages"][0]["_nz_parts"] = []
    elif change == "metadata":
        data["metadata"]["another_writer"] = 1
    elif change == "workspace":
        data["workspace"] = str(tmp_path.parent)
    elif change == "model":
        data["model"] = "external-model"
    elif change == "usage":
        data["metadata"]["_nz_session_snapshot"]["usage"]["input_tokens"] = 999
    else:
        data["metadata"]["timestamp"] = "external"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    enabled[0] = False
    with pytest.raises(SessionCommitConflict):
        asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    assert path.read_bytes() == before and not context.finalized


def test_in_memory_mutation_cannot_redefine_frozen_candidate(monkeypatch, tmp_path):
    from nz_coder.runtime.session.store import SessionCommitConflict

    runtime, context, path = opened(tmp_path)
    enabled, _ = fail_terminal(monkeypatch)
    with pytest.raises(OSError):
        asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    before = path.read_bytes()
    context.transcript[-1]["content"] = "changed after freeze"
    enabled[0] = False
    with pytest.raises(SessionCommitConflict):
        asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    assert path.read_bytes() == before


def test_outer_generated_timestamp_does_not_hide_committed_snapshot(
    monkeypatch, tmp_path
):
    runtime, context, path = opened(tmp_path)
    enabled, attempts = fail_terminal(monkeypatch, after=True)
    with pytest.raises(OSError):
        asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    data = json.loads(path.read_text())
    data["timestamp"] = "another generated time"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    enabled[0] = False
    asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    assert context.finalized and path.read_bytes() == before and len(attempts) == 1


def test_terminal_confirmation_requires_exact_interaction_marker(tmp_path):
    from nz_coder.runtime.session.store import confirms_terminal_commit

    runtime, context, path = opened(tmp_path)
    asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))
    data = json.loads(path.read_text())
    assert confirms_terminal_commit(
        data, tmp_path, context.session.session_id, context.interaction_run_id
    )
    copied = json.loads(path.read_text())
    copied["metadata"]["active_interaction_run_id"] = (
        context.interaction_run_id + ":other"
    )
    assert not confirms_terminal_commit(
        copied,
        tmp_path,
        context.session.session_id,
        context.interaction_run_id + ":other",
    )
    data["metadata"]["_nz_checkpoint_commits"] = ["terminal-old-unscoped-marker"]
    assert not confirms_terminal_commit(
        data, tmp_path, context.session.session_id, context.interaction_run_id
    )
