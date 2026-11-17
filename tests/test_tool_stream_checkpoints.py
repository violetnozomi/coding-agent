"""工具流累计快照不得让会话检查点退化为逐片段写盘。"""
from types import SimpleNamespace
import json

import pytest

from nz_coder.foundation import config
from nz_coder.providers.normalized import chunk
from nz_coder.protocol.message_schema import attach_message_identity
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.execution import stream_state
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.session_processor import SessionProcessor


@pytest.mark.parametrize(
    ("calls", "letter", "prefix_chars", "expected_checkpoints"),
    [(1, "x", 4096, 2), (2, "x", 4096, 3),
     (1, "汉", 2048, 1), (2, "汉", 2048, 2)],
)
def test_cumulative_tool_snapshots_checkpoint_only_new_characters(
    tmp_path, monkeypatch, calls, letter, prefix_chars, expected_checkpoints,
):
    class Completions:
        def create(self, **_kwargs):
            for index in range(calls):
                yield chunk(tool_calls=[{
                    "index": index, "id": f"call-{index}", "name": "write_file",
                    "arguments": '{"path":"a.txt","content":"' + letter * prefix_chars,
                }])
            for _ in range(128):
                for index in range(calls):
                    yield chunk(tool_calls=[{"index": index, "arguments": letter}])
            for index in range(calls):
                yield chunk(tool_calls=[{"index": index, "arguments": '"}'}])
            yield chunk(finish_reason="tool_calls")

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "STREAM_CHECKPOINT_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(config, "STREAM_CHECKPOINT_MIN_CHARS", 4096)
    # 只冻结检查点的本地计时，Gateway自身的线程/超时仍是真实执行。
    monkeypatch.setattr(stream_state, "time", SimpleNamespace(monotonic=lambda: 0.0))
    with scoped_workdir(tmp_path):
        agent = AgentLoop("unused", permission_mode="auto", trace_enabled=False,
            client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
            session_id="tool-checkpoints")
        try:
            part = agent._new_message_part(1)
            assistant = {"role": "assistant", "content": ""}
            attach_message_identity(assistant, part["message_id"], session_id="tool-checkpoints")
            agent._active_session_processor = SessionProcessor(assistant)
            agent._active_processor_messages = [assistant]
            writes = []
            monkeypatch.setattr(agent, "_checkpoint_messages", lambda _messages, status: writes.append(status))
            result = agent._call_streaming([], message_part=part)
        finally:
            agent.close()

    assert len(result.tool_calls) == calls
    for call in result.tool_calls:
        assert json.loads(call["function"]["arguments"])["content"] == letter * (prefix_chars + 128)
    assert len(writes) == expected_checkpoints
