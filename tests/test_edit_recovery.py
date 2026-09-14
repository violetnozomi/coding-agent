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


def test_read_version_blocks_edit_after_external_change_even_when_anchor_remains(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    executor = ToolExecutor(PermissionManager("auto"))
    with scoped_workdir(tmp_path):
        read = executor.execute_one({
            "id": "read-v1",
            "function": {"name": "read_file", "arguments": {"path": "module.py"}},
        }, 0)
        target.write_text("# user addition\nvalue = 1\n", encoding="utf-8")
        edit = executor.execute_one({
            "id": "edit-v1",
            "function": {
                "name": "edit_file",
                "arguments": {"path": "module.py", "old_text": "value = 1", "new_text": "value = 2"},
            },
        }, 1)

    assert read.executed is True
    assert read.metadata["model_read_observation"]["identity"]["content_hash"]
    assert edit.executed is False
    assert edit.dispatch_failed is True
    assert edit.metadata["stale_read"]["path"] == "module.py"
    assert "re-read" in edit.output.lower()
    assert target.read_text(encoding="utf-8") == "# user addition\nvalue = 1\n"


def test_read_version_blocks_replace_lines_after_external_line_shift(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "config.txt"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    executor = ToolExecutor(PermissionManager("auto"))
    with scoped_workdir(tmp_path):
        executor.execute_one({
            "id": "read-lines",
            "function": {"name": "read_file", "arguments": {"path": "config.txt"}},
        }, 0)
        target.write_text("user\nfirst\nsecond\nthird\n", encoding="utf-8")
        result = executor.execute_one({
            "id": "replace-stale-lines",
            "function": {
                "name": "replace_lines",
                "arguments": {"path": "config.txt", "start_line": 2, "end_line": 2, "new_text": "changed"},
            },
        }, 1)

    assert result.executed is False
    assert result.metadata["stale_read"]["path"] == "config.txt"
    assert target.read_text(encoding="utf-8") == "user\nfirst\nsecond\nthird\n"


def test_read_version_uses_content_hash_and_allows_touch(tmp_path, monkeypatch):
    """Timestamp-only changes are safe; same-size content changes are stale."""
    from nz_coder.foundation import config
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    with scoped_workdir(tmp_path):
        executor = ToolExecutor(PermissionManager("auto"))
        executor.execute_one({"function": {"name": "read_file", "arguments": {"path": "module.py"}}}, 0)
        import os
        os.utime(target, ns=(target.stat().st_atime_ns, target.stat().st_mtime_ns + 1_000_000))
        touched = executor.execute_one({"function": {"name": "edit_file", "arguments": {
            "path": "module.py", "old_text": "value = 1", "new_text": "value = 2",
        }}}, 1)
        assert touched.dispatch_failed is False

        executor.execute_one({"function": {"name": "read_file", "arguments": {"path": "module.py"}}}, 2)
        before = target.stat()
        target.write_text("value = 3\n", encoding="utf-8")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        stale = executor.execute_one({"function": {"name": "edit_file", "arguments": {
            "path": "module.py", "old_text": "value = 3", "new_text": "value = 4",
        }}}, 3)
    assert stale.metadata["stale_read"]["reason"] == "file_changed_after_model_read"
    assert target.read_text(encoding="utf-8") == "value = 3\n"


def test_read_then_delete_is_not_an_unobserved_create(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "gone.txt"
    target.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    with scoped_workdir(tmp_path):
        executor = ToolExecutor(PermissionManager("auto"))
        executor.execute_one({"function": {"name": "read_file", "arguments": {"path": "gone.txt"}}}, 0)
        target.unlink()
        result = executor.execute_one({"function": {"name": "write_file", "arguments": {
            "path": "gone.txt", "content": "new\n",
        }}}, 1)
    assert result.dispatch_failed is True
    assert result.metadata["stale_read"]["reason"] == "file_deleted_after_model_read"
    assert not target.exists()
