"""Final product-control contracts left open by the parity audit."""
from __future__ import annotations

from types import SimpleNamespace



def test_agent_catalog_projects_runtime_agent_and_supported_child_profiles(tmp_path):
    from nz_coder.interface.agent_catalog import agent_catalog

    agent = SimpleNamespace(
        model_id="provider/model",
        provider_id="provider",
        model_variant="high",
        permissions=SimpleNamespace(mode="default"),
    )

    items = agent_catalog(agent, tmp_path)

    assert items[0] == {
        "name": "worker",
        "description": "Primary coding Agent for this Session",
        "model": "provider/model",
        "reasoning_effort": "high",
        "tools": "session tool policy",
        "permissions": "default",
        "role": "primary",
    }
    profiles = {item["name"]: item for item in items[1:]}
    assert set(profiles) == {"explore", "plan", "general-purpose", "reflection"}
    assert profiles["explore"]["permissions"] == "read-only"
    assert profiles["general-purpose"]["permissions"] == "isolated write worktree"


def test_remote_backend_delegates_agent_and_workflow_controls():
    from nz_coder.interface.backend import RemoteTerminalBackend

    class Client:
        def list_agents(self, session_id):
            return [{"name": "worker", "session_id": session_id}]

        def list_workflows(self, session_id):
            return {"runs": [{"run_id": "wf-1"}], "session_id": session_id}

        def get_workflow(self, session_id, run_id):
            return {"run_id": run_id, "session_id": session_id}

        def control_workflow(self, session_id, run_id, action):
            return {"run_id": run_id, "action": action, "session_id": session_id}

        def memory_status(self, session_id):
            return {"pending": [], "ledger": [], "session_id": session_id}

        def get_memory_proposal(self, session_id, fingerprint):
            return {"fingerprint": fingerprint, "session_id": session_id}

        def review_memory(self, session_id, fingerprint, action, reason=""):
            return {
                "fingerprint": fingerprint, "action": action,
                "reason": reason, "session_id": session_id,
            }

    backend = RemoteTerminalBackend(Client(), "session-1")

    assert backend.agents()[0]["name"] == "worker"
    assert backend.workflows()["runs"][0]["run_id"] == "wf-1"
    assert backend.workflow("wf-1")["session_id"] == "session-1"
    assert backend.control_workflow("wf-1", "pause")["action"] == "pause"
    assert backend.memory()["session_id"] == "session-1"
    assert backend.memory_proposal("fp-1")["fingerprint"] == "fp-1"
    assert backend.review_memory("fp-1", "reject", reason="wrong")["reason"] == "wrong"


def test_remote_registry_exposes_agents_and_workflow_lifecycle():
    from nz_coder.interface.remote import _remote_command_registry

    commands = {item.name: item for item in _remote_command_registry().visible_commands()}

    assert commands["agents"].usage == "/agents"
    assert commands["workflow"].usage == (
        "/workflow [list|show ID|run NAME [JSON_ARGS|REQUEST]|pause ID|resume ID|stop ID]"
    )
    assert commands["memory"].usage == (
        "/memory [pending|inspect FINGERPRINT|approve FINGERPRINT|reject FINGERPRINT [REASON]|ledger]"
    )


def test_embedded_registry_exposes_agent_inspector():
    from nz_coder.interface.commands import default_command_registry

    commands = {item.name: item for item in default_command_registry.visible_commands()}

    assert commands["agents"].usage == "/agents"


def test_terminal_startup_error_is_traceback_free_by_default(monkeypatch):
    from nz_coder.interface import cli

    messages = []

    async def fail():
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(cli, "_run_cli", fail)
    monkeypatch.setattr(cli, "console", SimpleNamespace(
        print=lambda value="", **_kwargs: messages.append(str(value))
    ))

    assert cli.main([]) == 1
    output = "\n".join(messages)
    assert "NZ-Coder could not start: startup exploded" in output
    assert "Traceback" not in output
