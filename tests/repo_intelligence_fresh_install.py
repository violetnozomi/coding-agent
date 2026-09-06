"""Offline installed-package smoke for live index publication and SDK recovery."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile

import nz_coder
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.intelligence.code_index import update_code_index_after_write
from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence
from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
from nz_coder.sdk import AgentClient
from nz_coder.state.sessions import save_session
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir


def main():
    assert Path(nz_coder.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    with tempfile.TemporaryDirectory(prefix="nz-index-wheel-") as temporary:
        root = Path(temporary)
        (root / "a.py").write_text("def before(): pass\n", encoding="utf-8")
        service = workspace_repo_intelligence(root)
        try:
            assert service.wait_ready(5).status == "ready"
            messages = [{"role": "user", "content": "edit", "_nz_message_id": "user", "_nz_session_id": "wheel-index"},
                        {"role": "assistant", "content": "changed", "_nz_message_id": "step", "_nz_session_id": "wheel-index"}]
            ledger = ToolLedger(root)
            row = ledger.register(session_id="wheel-index", interaction_id="interaction", assistant_step_id="step",
                                  agent_id="agent", call_id="write", tool="write_file", tool_input={"path": "a.py"})
            with checkpoint_execution(ledger, row["execution_id"]):
                WorkspaceFileAccess(root).write_text("a.py", "def after(): pass\n")
            stats = update_code_index_after_write(["a.py"], root)
            assert service.symbol_context("after")["generation"] == stats.generation
            assert service.module_context("a.py")["generation"] == stats.generation
            with scoped_workdir(root):
                save_session(messages, session_id="wheel-index", activate=False)
            client = AgentClient()
            assert asyncio.run(client.undo_session(workspace=root, session_id="wheel-index")).status == "completed"
            assert service.symbol_context("before")["definition"]["name"] == "before"
            assert service.symbol_context("after")["definition"] is None
            assert asyncio.run(client.redo_session(workspace=root, session_id="wheel-index")).status == "completed"
            assert service.symbol_context("after")["definition"]["name"] == "after"
            assert service.symbol_context("before")["definition"] is None
        finally:
            release_repo_intelligence(root)
    print("fresh-wheel index + SDK Undo/Redo smoke passed; provider_calls=0")


if __name__ == "__main__":
    main()
