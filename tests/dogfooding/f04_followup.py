"""Installed F04-only PTY run and same-session HTTP wheel smoke, not a coding eval."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

from offline import Terminal, wait_until
from run_r1 import environment, save
from nz_coder.http_service.client import NZCoderClient
from nz_coder.http_service.daemon import start_daemon, stop_daemon


def measure(*, identity, marker, command, model_replies, snapshot, terminal_text):
    """Accept only current-call output; command/history substring matches do not count."""
    replies = [m for record in model_replies if record.get("attempt_id") == identity["attempt_id"]
               for m in record.get("tool_replies", [])
               if m.get("tool_call_id") == identity["tool_call_id"]]
    run = snapshot.get("run", {})
    parts = [p for p in run.get("parts", [])
             if run.get("interaction_run_id") == identity["interaction_run_id"]
             and snapshot.get("session", {}).get("id") == identity["session_id"]
             and p.get("call_id") == identity["tool_call_id"]]
    return {"marker_not_in_command": marker not in command,
            "model_reply": bool(replies) and any(marker in str(r.get("content")) for r in replies),
            "public_snapshot": bool(parts) and any(marker in p.get("state", {}).get("output", "") for p in parts),
            "terminal_current_request": marker not in command and marker in terminal_text}


def run(root: Path, *, smoke: bool = True):
    root = root.resolve()
    root.mkdir(exist_ok=False)
    env = environment(root, provider="f04-offline")
    for key in list(env):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            env.pop(key)
    env.update({"API_KEY": "offline-dummy", "API_BASE_URL": "https://api.deepseek.com",
                "NZ_PROCESS_BUFFER_BYTES": "4096", "NZ_F04_ATTEMPT": root.name,
                "NZ_F04_CAPTURE_RECORD": str(root / "capture.jsonl"),
                "NZ_F04_MODEL_RECORD": str(root / "model-replies.jsonl")})
    os.environ.clear()
    os.environ.update(env)
    workspace = root / "workspace"
    workspace.mkdir()
    shutil.copyfile(Path(__file__).with_name("f04_emit.py"), workspace / "emit_failure.py")
    # The default daemon workspace is its launch cwd. Do not authorize both
    # an ancestor evidence directory and its child task workspace.
    os.chdir(workspace)
    state = root / "daemon"
    daemon = start_daemon(state_root=state, port=0, workspaces=[str(workspace)], startup_timeout=20)
    client = NZCoderClient(daemon["endpoint"], Path(daemon["token_path"]).read_text().strip(), timeout=10)
    wid = next(w["id"] for w in client.list_workspaces() if Path(w["path"]) == workspace)
    sid = client.create_session("default", wid)["id"]
    terminal = Terminal(root, state, sid, workspace, "F04-current-request")
    try:
        terminal.drain(1)
        terminal.send("R1:F04 show controlled long-output failure 中文\r")
        wait_until(lambda: client.pending_permissions(sid), terminal)
        offset = len(terminal.buffer)
        terminal.send("\r")
        snapshot = wait_until(lambda: s if (s := client.attach_snapshot(sid))["settled"] else None,
                              terminal, timeout=40)
        terminal.drain(1)
        save(root, "snapshot.json", snapshot)
        expected = json.loads((workspace / "expected.json").read_text())
        parts = [p for p in snapshot["run"]["parts"] if p.get("type") == "tool" and p.get("tool") == "bash"]
        assert len(parts) == 1
        part = parts[0]
        props = part["state"]
        identity = {"attempt_id": root.name, "session_id": sid,
                    "interaction_run_id": snapshot["run"]["interaction_run_id"],
                    "tool_call_id": part["call_id"]}
        assert all(identity.values())
        replies = [json.loads(s) for s in (root / "model-replies.jsonl").read_text().splitlines()]
        text = bytes(terminal.buffer[offset:]).decode(errors="replace")
        # The independent numeric location occurs in both raw Python diagnostics
        # and the reconstructed safe summary; do not demand the new formatter
        # when measuring old captures.
        marker = f"line {expected['line']}"
        checks = measure(identity=identity, marker=marker, command=props["input"]["command"],
                         model_replies=replies, snapshot=snapshot, terminal_text=text)
        captures = [json.loads(s) for s in (root / "capture.jsonl").read_text().splitlines()]
        capture = [c for c in captures if c.get("tool_call_id") == identity["tool_call_id"]
                   and c.get("attempt_id") == identity["attempt_id"]]
        checks["capture_result"] = len(capture) == 1 and marker in capture[0]["output"]
        checks["diagnostic_kind"] = "SyntaxError" in text
        checks.update({"exit_code": props.get("metadata", {}).get("exit"),
                       "truncated": props.get("metadata", {}).get("truncated"),
                       "exit_visible": "exit code 7" in text,
                       "truncation_visible": "Output truncated" in text,
                       "secret_absent": "F04_PRIVATE_SENTINEL" not in json.dumps([snapshot, replies, text, captures]),
                       "pty_bytes": len(terminal.buffer), "product_status": snapshot["session"]["status"]})
        terminal.close()
        terminal = Terminal(root, state, sid, workspace, "F04-reconnected")
        terminal.drain(2)
        checks["reconnect_marker"] = marker in terminal.buffer.decode(errors="replace")
        terminal.resize(50)
        terminal.drain(.2)
        terminal.resize(110)
        terminal.drain(.2)
        # One final HTTP write plus same-session reuse; no complete F matrix.
        if smoke:
            smoke_sid = client.create_session("acceptEdits", wid)["id"]
            for prompt in ("R1:F01 file smoke", "R1:F05 reuse"):
                client.run(smoke_sid, prompt)
                smoke_state = wait_until(lambda: s if (s := client.attach_snapshot(smoke_sid))["settled"] else None,
                                        timeout=20)
                assert smoke_state["session"]["status"] == "completed"
            assert (workspace / "smoke.txt").read_text() == "F04 wheel smoke\n"
            messages = client.messages(smoke_sid)
            assert len([m for m in messages if m.get("role") == "user"]) == 2
            assert len([m for m in messages if m.get("role") == "assistant" and m.get("content")]) == 2
            checks["http_file_and_reuse"] = True
        save(root, "metrics.json", {"identity": identity, "checks": checks,
            "layer_A": "test-only pass-through observer of installed run_bash ToolOutput, before common projection",
            "visual_status": "NOT_VERIFIED", "module": __import__("nz_coder").__file__})
        print(json.dumps(checks), flush=True)
    finally:
        terminal.close()
        stop_daemon(state_root=state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args()
    run(args.root, smoke=not args.skip_smoke)
