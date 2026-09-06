"""Concurrent terminal reports must preserve committed execution facts."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import copy_context
import threading

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.process.checkpoint_runtime import (
    dispatch_checkpoint,
    recovery_run,
    register_batch,
    settle_batch,
    settle_execution,
)
from nz_coder.tool_platform.execution import ToolExecutionResult


def test_settle_execution_success_survives_stale_concurrent_cancellation(tmp_path, monkeypatch):
    """Removing the terminal UPDATE's CAS lets late cancellation erase success."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    arguments = {"path": "a.txt", "content": "written once"}
    call = {"id": "call-write", "function": {
        "name": "write_file", "arguments": arguments,
    }}
    result = ToolExecutionResult(
        name="write_file", tool_input=arguments, output="One write completed.",
        executed=True, dispatch_failed=False, command_failed=False, is_write=True,
    )

    with recovery_run(workspace, "session-a") as run:
        run.attach("interaction-a", [])
        register_batch([{"role": "assistant", "_nz_message_id": "msg-a"}], [call])
        access = WorkspaceFileAccess(workspace)
        with dispatch_checkpoint(call["id"]) as allowed:
            assert allowed
            access.write_text("a.txt", "written once")
        before_mutations = run.ledger.mutations("session-a")
        assert len(before_mutations) == 1
        assert before_mutations[0]["disposition"] == "committed"

        original_connection = run.ledger.connection
        both_read_running = threading.Barrier(2)
        success_committed = threading.Event()
        observed_states = []
        errors = []

        class OrderedCursor:
            """Pause only after the real SQLite SELECT has returned its row."""

            def __init__(self, cursor):
                self.cursor = cursor

            def fetchone(self):
                row = self.cursor.fetchone()
                observed_states.append(row[0])
                both_read_running.wait(timeout=5)
                if threading.current_thread().name == "late-cancel":
                    assert success_committed.wait(5), "success did not commit"
                return row

        class OrderedConnection:
            """Forward SQL unchanged; the test controls scheduling, not storage."""

            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *parameters):
                cursor = self.connection.execute(sql, *parameters)
                if sql.startswith("SELECT execution_state"):
                    return OrderedCursor(cursor)
                return cursor

        @contextmanager
        def ordered_connection():
            with original_connection() as connection:
                yield OrderedConnection(connection)
            # Signal only after the actual SQLite transaction has committed.
            if threading.current_thread().name == "success":
                success_committed.set()

        def settle(cancelled):
            try:
                settle_execution(call["id"], result, cancelled=cancelled)
            except BaseException as error:
                errors.append(error)

        with monkeypatch.context() as scheduling:
            scheduling.setattr(run.ledger, "connection", ordered_connection)
            workers = [
                threading.Thread(
                    name=name, target=copy_context().run, args=(settle, cancelled),
                    daemon=True,
                )
                for name, cancelled in (("success", False), ("late-cancel", True))
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
            assert not any(worker.is_alive() for worker in workers)
        assert not errors
        assert observed_states == ["running", "running"]

        row, = run.ledger.executions("session-a")
        assert row["execution_state"] == "succeeded"
        assert row["terminal_cause"] == "none"
        assert row["result_preview"] == ""
        assert row["preview_admitted"] == 0

        # Repeated terminal reports cannot change the row or dispatch again.
        settle_execution(call["id"], result)
        settle_execution(call["id"], cancelled=True)
        with dispatch_checkpoint(call["id"]) as allowed:
            assert not allowed
        assert run.ledger.executions("session-a") == [row]
        assert run.ledger.mutations("session-a") == before_mutations
        assert access.read_bytes("a.txt") == b"written once"

        # Only the post-guardrail batch boundary may publish a preview.
        settle_batch([(0, call, result)], [])
        published, = run.ledger.executions("session-a")
        assert published["result_preview"] == "One write completed."
        assert published["preview_admitted"] == 1
        assert published["execution_state"] == "succeeded"
