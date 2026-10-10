"""R1 Linux PTY + installed daemon scenes, not a model coding evaluation."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

from run_r1 import environment, save
from nz_coder.http_service.client import NZCoderClient
from nz_coder.http_service.daemon import start_daemon, stop_daemon


class Terminal:
    def __init__(self, root, state, session, cwd, label):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
        self.buffer = bytearray()
        self.path = root / f"{label}.pty"
        self.handle = self.path.open("wb")
        self.process = subprocess.Popen([sys.executable, "-m", "nz_coder", "attach", session,
            "--state-root", str(state)], cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True)
        os.close(slave)

    def drain(self, duration=0.1):
        until = time.monotonic() + duration
        while time.monotonic() < until:
            if select.select([self.master], [], [], min(0.1, max(0,until-time.monotonic())))[0]:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                if len(self.buffer) + len(chunk) > 4_000_000:
                    raise RuntimeError("R1 bounded terminal capture exceeded 4 MB")
                self.buffer.extend(chunk)
                self.handle.write(chunk)
                self.handle.flush()

    def send(self, text):
        os.write(self.master, text.encode())

    def resize(self, width):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH",32,width,0,0))
        os.kill(self.process.pid, signal.SIGWINCH)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.drain()
        self.handle.close()
        os.close(self.master)


def wait_until(predicate, terminal=None, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if terminal:
            terminal.drain(0.1)
        else:
            select.select([], [], [], 0.1)
    raise TimeoutError("R1 state predicate deadline exceeded")


def run(root):
    if (root / "offline-results.json").exists():
        raise FileExistsError("Preserve previous R1 evidence; use a fresh attempt directory")
    env = environment(root, provider="r1-scripted")
    for key in list(env):
        if any(word in key.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
            env.pop(key)
    env.update({"API_KEY":"r1-dummy-not-a-secret", "API_BASE_URL":"https://api.deepseek.com",
                "PERMISSION_MODE":"default"})
    os.environ.clear()
    os.environ.update(env)
    workspace = root / "interaction-workspace"
    workspace.mkdir(exist_ok=True)
    state = root / "offline-daemon"
    daemon = start_daemon(state_root=state, port=0, workspaces=[str(workspace)], startup_timeout=20)
    token = Path(daemon["token_path"]).read_text().strip()
    client = NZCoderClient(daemon["endpoint"], token, timeout=10)
    wid = next(w["id"] for w in client.list_workspaces() if Path(w["path"]) == workspace)
    session = client.create_session("default",wid)["id"]
    terminal = Terminal(root,state,session,workspace,"F01")
    results = {}
    try:
        terminal.drain(2)
        print(json.dumps({"phase":"ready", "session":session}), flush=True)
        save(root,"offline-ready.json",{"session":session,"module":__import__("nz_coder").__file__})

        def snapshot(label):
            value = client.attach_snapshot(session)
            save(root, label+"-snapshot.json", value)
            return value

        def permission(prompt):
            terminal.send(prompt+"\r")
            value = wait_until(lambda: client.pending_permissions(session), terminal)
            terminal.drain(0.5)
            return value[0]

        def finished():
            value = wait_until(lambda: (s if not s["session"]["running"] else None)
                               if (s := client.attach_snapshot(session)) else None, terminal, timeout=40)
            terminal.drain(0.5)
            return value

        first = permission("R1:F01 reject the harmless permission-note.txt write")
        waiting = snapshot("F01-waiting")
        terminal.send("\x1b")
        denied = finished()
        absent_after_reject = not (workspace/"permission-note.txt").exists()
        second = permission("R1:F01 allow this write once")
        terminal.send("\r")
        allowed = finished()
        exists_after_allow = (workspace/"permission-note.txt").exists()
        third = permission("R1:F01 ask again for the same write")
        terminal.send("\x1b")
        finished()
        results["F01"] = {"waiting":waiting["session"]["status"],
            "first_permission":first,"second_permission":second,"third_permission":third,
            "absent_after_reject":absent_after_reject,"exists_after_once":exists_after_allow,
            "denied_status":denied["session"]["status"],"allowed_status":allowed["session"]["status"],
            "permission_asked_again":True,
            "ui_permission_text":b"Permission required" in terminal.buffer,
            "runtime_status_while_waiting":waiting["session"]["runtime_status"],
            "visual":"PARTIALLY_VERIFIED"}
        print("F01 captured",flush=True)

        permission("R1:F02 run the controlled slow tool; I will cancel")
        terminal.send("\r")
        wait_until(lambda:(workspace/"slow.pid").exists(),terminal)
        slow_pid = int((workspace/"slow.pid").read_text())
        cancel_start = time.time()
        client.abort(session)
        cancelled = finished()
        cancel_settled = time.time()
        def dead():
            try:
                os.kill(slow_pid,0)
                return False
            except ProcessLookupError:
                return True
        cleaned = wait_until(dead,terminal,timeout=10)
        terminal.send("R1:F05 verify same session reuse after cancellation\r")
        wait_until(lambda:b"session usable" in terminal.buffer,terminal)
        reused = finished()
        results["F02"] = {"cancel_settlement_seconds":cancel_settled-cancel_start,
            "cancel_and_reuse_seconds":time.time()-cancel_start,
            "cancelled_status":cancelled["session"]["status"],"process_dead":bool(cleaned),
            "late_file_absent":not (workspace/"late.txt").exists(),
            "reuse_status":reused["session"]["status"],
            "ui_cancelled_text":b"cancelled" in terminal.buffer.lower(),"visual":"PARTIALLY_VERIFIED"}
        snapshot("F02-after-reuse")
        print("F02 captured",flush=True)

        before = (workspace/"slow.pid").read_text()
        permission("R1:F03 controlled slow tool while client disconnects")
        terminal.send("\r")
        wait_until(lambda:(workspace/"slow.pid").read_text()!=before,terminal)
        pre_disconnect = snapshot("F03-before-disconnect")
        terminal.close()
        disconnected = snapshot("F03-disconnected")
        terminal = Terminal(root,state,session,workspace,"F03-reconnected")
        restored = finished()
        terminal.drain(2)
        messages = client.messages(session)
        user_count = sum(1 for m in messages if m.get("role")=="user" and "R1:F03" in str(m.get("content")))
        results["F03"] = {"status_at_disconnect":disconnected["session"]["status"],
            "cursor_before":pre_disconnect["cursor"],"cursor_after":restored["cursor"],
            "final_status":restored["session"]["status"],"user_request_count":user_count,
            "tool_finished":(workspace/"late.txt").exists(),
            "terminal_alive":terminal.process.poll() is None,"visual":"PARTIALLY_VERIFIED"}
        snapshot("F03-restored")
        print("F03 captured",flush=True)

        permission("R1:F04 display bounded long output and preserve error tail 中文")
        terminal.resize(50)
        terminal.send("\r")
        long_done = finished()
        save(root,"F04-long-done-snapshot.json",long_done)
        terminal.resize(110)
        terminal.drain(1)
        terminal.send("R1:F05 中文多行\x1b\r代码显示\r")
        terminal.drain(1)
        results["F04"] = {"final_status":long_done["session"]["status"],
            "captured_bytes":len(terminal.buffer),"widths":[100,50,110],
            # The marker is also in the echoed command: substring presence in a
            # PTY is NOT evidence that the tool's error tail was retained.
            "tail_marker_present_not_proof":b"R1_CRITICAL_TAIL_ERROR" in terminal.buffer,
            "error_tail_verified":False,
            "final_marker_present_not_proof":b"R1 final" in terminal.buffer,
            "truncation_visible":any(x in terminal.buffer.lower() for x in (b"truncat", b"more line", b"omitted")),
            "terminal_alive":terminal.process.poll() is None,
            "visual":"PARTIALLY_VERIFIED", "layout_visual":"NOT_VERIFIED"}
        snapshot("F04-final")
        print("F04 captured",flush=True)
    except Exception as exc:
        results["driver_error"] = {"type":type(exc).__name__, "message":str(exc)}
        save(root,"offline-error-snapshot.json",client.attach_snapshot(session))
        raise
    finally:
        terminal.close()
        stop_daemon(state_root=state)
        save(root,"offline-results.json",results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    run(parser.parse_args().root)
