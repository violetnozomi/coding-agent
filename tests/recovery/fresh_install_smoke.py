"""Offline wheel smoke: installed SDK -> journal -> byte-exact Undo/Redo."""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile

import nz_coder
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
from nz_coder.sdk import AgentClient
from nz_coder.state.sessions import save_session
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir


def main() -> None:
    installed = Path(nz_coder.__file__).resolve()
    assert installed.is_relative_to(Path(sys.prefix).resolve()), "smoke imported the checkout"
    with tempfile.TemporaryDirectory(prefix="nz-recovery-wheel-") as temporary:
        root = Path(temporary)
        (root / "a.bin").write_bytes(b"\xff\x00\r\n")
        messages = [{"role": "user", "content": "edit", "_nz_message_id": "msg-user", "_nz_session_id": "wheel-session"},
                    {"role": "assistant", "content": "done", "_nz_message_id": "msg-step", "_nz_session_id": "wheel-session"}]
        ledger = ToolLedger(root)
        row = ledger.register(session_id="wheel-session", interaction_id="interaction-wheel", assistant_step_id="msg-step",
                              agent_id="agent-wheel", call_id="call-wheel", tool="write_file", tool_input={"path": "a.bin"})
        with checkpoint_execution(ledger, row["execution_id"]):
            WorkspaceFileAccess(root).write_bytes("a.bin", b"replacement")
        with scoped_workdir(root):
            save_session(messages, session_id="wheel-session", activate=False)
        client = AgentClient()
        assert asyncio.run(client.undo_session(workspace=root, session_id="wheel-session")).status == "completed"
        assert (root / "a.bin").read_bytes() == b"\xff\x00\r\n"
        assert asyncio.run(client.redo_session(workspace=root, session_id="wheel-session")).status == "completed"
        assert (root / "a.bin").read_bytes() == b"replacement"
    print("fresh-wheel SDK recovery smoke passed; provider_calls=0")


if __name__ == "__main__":
    main()
