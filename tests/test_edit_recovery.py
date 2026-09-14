"""Regression coverage for precise edit failure evidence and recovery state."""
from __future__ import annotations


def test_edit_failure_keeps_bounded_file_anchors_and_version(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import edit_file

    target = tmp_path / "module.py"
    target.write_text(
        "def first(value):\n"
        "    return value + 1\n\n"
        "# repeated prefix\n"
        "def second(value):\n"
        "    return value + 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    with scoped_workdir(tmp_path):
        result = edit_file("module.py", "def missing(value):\n    return value", "")

    assert "old_text not found" in result
    metadata = result.metadata
    recovery = metadata["edit_recovery"]
    assert recovery["code"] == "EDIT_NOT_FOUND"
    assert recovery["path"] == "module.py"
    assert recovery["exists"] is True
    assert recovery["version"]["content_hash"]
    assert recovery["candidates"]
    assert all(candidate["start_line"] <= candidate["end_line"] for candidate in recovery["candidates"])
    assert any("def first" in candidate["excerpt"] for candidate in recovery["candidates"])
    assert any("def second" in candidate["excerpt"] for candidate in recovery["candidates"])


def test_bash_text_that_mentions_old_text_does_not_create_edit_recovery():
    from nz_coder.runtime.verification.recovery import RecoveryState

    recovery = RecoveryState()
    diagnostic = recovery.tool_failure_diagnostic(
        "bash",
        "Error: old_text not found in a shell log",
    )

    assert "could not find the exact old_text" not in diagnostic
    assert recovery.blocked_edit_writes == set()


def test_real_tool_runtime_projects_edit_recovery_and_blocks_whole_file_write(
    tmp_path, monkeypatch,
):
    """The production pipeline carries edit evidence into the next model turn."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    target = tmp_path / "module.py"
    target.write_text(
        "def first(value):\n    return value + 1\n\n"
        "def second(value):\n    return value + 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=object(), trace_enabled=False)
    messages: list[dict] = []
    try:
        failed = [{
            "id": "edit-failed",
            "function": {
                "name": "edit_file",
                "arguments": {
                    "path": "module.py",
                    "old_text": "def missing(value):\n    return value",
                    "new_text": "def missing(value):\n    return value + 3",
                },
            },
        }]
        action = agent.tool_runtime.execute_batch_sync(agent, failed, messages)
        assert action == "continue"
        recovery_messages = [
            message for message in messages
            if message.get("_nz_synthetic") and "<edit-recovery>" in message.get("content", "")
        ]
        assert len(recovery_messages) == 1
        recovery_text = recovery_messages[0]["content"]
        assert "candidate_lines:" in recovery_text
        assert "read_file: path=module.py" in recovery_text
        assert agent.recovery.blocked_edit_writes

        whole_file = [{
            "id": "write-blocked",
            "function": {
                "name": "write_file",
                "arguments": {"path": "module.py", "content": "# guessed rewrite\n"},
            },
        }]
        agent.tool_runtime.execute_batch_sync(agent, whole_file, messages)
        assert any(
            "whole-file overwrite is not evidence-backed" in message.get("content", "")
            for message in messages
        )
        assert target.read_text(encoding="utf-8").startswith("def first")
    finally:
        agent.close()


def test_successful_precise_edit_clears_only_that_file_recovery_state(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    first = tmp_path / "first.py"
    second = tmp_path / "second.txt"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=object(), trace_enabled=False)
    messages: list[dict] = []
    try:
        for path in ("first.py", "second.txt"):
            agent.tool_runtime.execute_batch_sync(agent, [{
                "id": f"failed-{path}",
                "function": {
                    "name": "edit_file",
                    "arguments": {"path": path, "old_text": "missing", "new_text": "fixed"},
                },
            }], messages)
        assert len(agent.recovery.blocked_edit_writes) == 2

        agent.tool_runtime.execute_batch_sync(agent, [{
            "id": "fixed-first",
            "function": {
                "name": "edit_file",
                "arguments": {"path": "first.py", "old_text": "value = 1", "new_text": "value = 3"},
            },
        }], messages)
        assert not agent.recovery.edit_write_blocked("first.py")
        assert agent.recovery.edit_write_blocked("second.txt")
        assert first.read_text(encoding="utf-8") == "value = 3\n"
        assert second.read_text(encoding="utf-8") == "value = 2\n"
    finally:
        agent.close()


def test_ambiguous_apply_patch_preserves_locations_without_writing(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import apply_patch

    target = tmp_path / "settings.txt"
    original = "mode = safe\n\nmode = safe\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    with scoped_workdir(tmp_path):
        result = apply_patch([{
            "path": "settings.txt",
            "op": "replace",
            "old_text": "mode = safe",
            "new_text": "mode = fast",
        }])

    assert "matches 2 locations" in result
    assert result.metadata["edit_recovery"]["code"] == "EDIT_AMBIGUOUS"
    assert "start_line" in result.metadata["edit_recovery"]["candidates"][0]
    assert target.read_text(encoding="utf-8") == original


def test_oversized_patch_has_distinct_edit_recovery_classification(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import apply_patch

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    changes = [
        {"path": "new.txt", "op": "create", "content": str(index)}
        for index in range(21)
    ]
    with scoped_workdir(tmp_path):
        result = apply_patch(changes)

    assert "Max 20 changes" in result
    assert result.metadata["edit_recovery"]["code"] == "EDIT_TOO_LARGE"


def test_external_file_change_discards_stale_whole_file_write_block(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop("test", permission_mode="auto", client=object(), trace_enabled=False)
    messages: list[dict] = []
    try:
        agent.tool_runtime.execute_batch_sync(agent, [{
            "id": "failed-edit",
            "function": {
                "name": "edit_file",
                "arguments": {"path": "module.py", "old_text": "missing", "new_text": "fixed"},
            },
        }], messages)
        target.write_text("value = 2\n", encoding="utf-8")
        assert agent.recovery.edit_write_blocked("module.py") is False
        agent.tool_runtime.execute_batch_sync(agent, [{
            "id": "authorized-rewrite",
            "function": {
                "name": "write_file",
                "arguments": {"path": "module.py", "content": "value = 3\n"},
            },
        }], messages)
        assert target.read_text(encoding="utf-8") == "value = 3\n"
    finally:
        agent.close()
