"""通过真实模型/工具循环验证来源、核对、维护和落盘；模型为本地脚本。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from nz_coder.runtime.execution.composition import build_coding_agent
from nz_coder.state.workdir import scoped_workdir


def response(name=None, arguments=None, call_id=""):
    calls = [] if name is None else [SimpleNamespace(id=call_id, type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))]
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="Recorded project background with its source." if name is None else "", tool_calls=calls),
        finish_reason="stop" if name is None else "tool_calls")],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3))


def test_real_loop_binds_read_and_source_check_metadata_without_repeat_read(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "LSP_ENABLED", False)
    monkeypatch.setattr(config, "MAX_AGENT_TURNS", 8)
    (tmp_path / "config.py").write_text("DATABASE = 'memory'\n")

    class Completions:
        count = 0
        def create(self, **kwargs):
            self.count += 1
            tools = [m for m in kwargs["messages"] if m.get("role") == "tool"]
            if self.count == 1:
                return response("read_file", {"path": "config.py"}, "read-config")
            if self.count == 2:
                return response("save_memory", {"action": "create", "name": "database",
                    "description": "Tests use an in-memory database", "type": "project",
                    "content": "The configured database is memory.",
                    "applicability": "While config.py retains this database setting.",
                    "source_refs": [{"kind": "file", "locator": "config.py", "tool_call_id": "read-config"}]}, "save-note")
            if self.count == 3:
                saved = json.loads(tools[-1]["content"])
                assert saved["status"] == "saved", saved
                self.memory_id = saved["memory_id"]
                return response("recall_memory", {"kind": "record", "id": self.memory_id,
                    "check_sources": True}, "check-note")
            if self.count == 4:
                checked = json.loads(tools[-1]["content"])
                assert checked["source_checks"][0]["status"] == "matching", checked
                observation = checked["source_checks"][0]["observation_key"]
                return response("save_memory", {"action": "assess", "memory_id": self.memory_id,
                    "expected_revision": 1, "judgment": {"coverage": "partial",
                        "covered_claims": "The database setting in config.py", "result": "supported",
                        "assessment": "Source fingerprint still matches the code previously read."},
                    "evidence_refs": [{"kind": "file", "locator": "config.py", "tool_call_id": "check-note",
                        "observation_key": observation}]}, "assess-note")
            return response()

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with scoped_workdir(tmp_path):
        agent = build_coding_agent("Inspect code and explicitly maintain useful project memory.",
            client=client, permission_mode="auto", permission_asker=lambda *_: True,
            sidecar_verifier=False, session_id="memory-journey")
        messages = [{"role": "user", "content": "Read config.py, save the database fact as project memory, then check its source."}]
        try:
            result = asyncio.run(agent.run(messages, stream=False))
            assert result["status"] == "completed_unverified", result
            record = agent._mm.read(completions.memory_id)
            assert record["source_refs"][0]["binding"] == "bound"
            assessment = record["assessments"][0]
            assert assessment["judgment"]["coverage"] == "partial"
            assert assessment["evidence_refs"][0]["binding"] == "bound"
            assert assessment["evidence_refs"][0]["observation_kind"] == "identity_check"
            assert sum(call.get("function", {}).get("name") == "read_file" for m in messages
                       for call in m.get("tool_calls", [])) == 1
        finally:
            agent.close()
