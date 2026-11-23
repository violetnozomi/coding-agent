"""用实际安装的 Windows 终端测试首次连接、中文问答和重启后恢复。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep

from tests.evaluation.fixtures.review_execution_fake import ExecutionFake


MODEL = "nz-windows-smoke"
GREETING = "你好，请跟我打个招呼。"
REPLY = "WinFrontReady7C"
FOLLOWUP = "重启前我问了什么？"
RESTORED = "WinHistorySeen5A"


class GreetingServer(ExecutionFake):
    models = (MODEL,)

    def message(self, request):
        conversation = {"user": [], "assistant": []}
        for message in request["messages"]:
            role = message["role"]
            if role not in conversation:
                continue
            content = message["content"]
            if isinstance(content, list):
                content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
            conversation[role].append(content or "")
        users = "\n".join(conversation["user"])
        answers = "\n".join(conversation["assistant"])
        # 只有重启后的请求确实带上原用户问题及助手回复时，才返回恢复标记。
        answer = "OK"
        if request.get("stream") and request.get("model") == MODEL:
            if FOLLOWUP in users:
                answer = RESTORED if GREETING in users and REPLY in answers else "MissingHistory"
            elif GREETING in users:
                answer = REPLY
        return {"role": "assistant", "content": answer}


class Terminal:
    def __init__(self, process):
        import pyte

        self.process = process
        self.output = Queue()
        self.screen = pyte.Screen(116, 34)
        self.stream = pyte.Stream(self.screen)
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while chunk := self.process.read(8192):
                self.output.put(chunk)
        except (EOFError, OSError):
            pass
        finally:
            self.output.put(None)

    def submit(self, text):
        self.process.write(text)
        self.process.write("\r")

    def wait_for(self, marker):
        deadline = monotonic() + 35
        while (remaining := deadline - monotonic()) > 0:
            try:
                chunk = self.output.get(timeout=remaining)
            except Empty:
                break
            if chunk is None:
                break
            self.stream.feed(chunk)
            if "\x1b[6n" in chunk:
                cursor = self.screen.cursor
                self.process.write(f"\x1b[{cursor.y + 1};{cursor.x + 1}R")
            if marker in "\n".join(self.screen.display):
                return
        # 英文代码页的 Windows CI 也应能打印完整的诊断。
        raise RuntimeError(f"Missing {marker!a}. Last screen: {ascii(self.screen.display)}")

    def exit(self):
        self.submit("/exit")
        deadline = monotonic() + 10
        while self.process.isalive() and monotonic() < deadline:
            sleep(0.05)
        if self.process.isalive() or self.process.exitstatus != 0:
            raise RuntimeError("The installed frontend did not exit cleanly.")


@contextmanager
def installed_terminal(exe, workspace, env):
    from winpty import PtyProcess

    # pywinpty 2.x 会把整数 0 当作没传 backend，字符串 "0" 显式选中系统 ConPTY。
    process = PtyProcess.spawn([str(exe)], cwd=str(workspace), env=env, dimensions=(34, 116), backend="0")
    terminal = Terminal(process)
    try:
        terminal.wait_for("Enter send")
        yield terminal
    finally:
        process.close(force=True)
        terminal.reader.join(timeout=3)


def exercise(exe, workspace, profile, server):
    # 工作目录来自已安装的快捷方式；用户资料从空目录开始，重启后沿用。
    env = {key: os.environ[key] for key in (
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "USERPROFILE",
        "USERNAME", "TEMP", "TMP",
    ) if key in os.environ}
    env.update(
        APPDATA=str(profile / "roaming"), LOCALAPPDATA=str(profile / "local"),
        NZ_CODER_USER_CONFIG=str(profile / "config.env"), NO_PROXY="127.0.0.1,localhost",
        TERM="xterm-256color", PYTHONUTF8="1",
    )
    url = f"http://127.0.0.1:{server.server_port}/v1"
    with installed_terminal(exe, workspace, env) as terminal:
        terminal.submit("/connect")
        terminal.wait_for("Connect provider")
        terminal.submit("")
        terminal.wait_for("API_KEY:")
        terminal.submit("offline-test")
        terminal.wait_for("API_BASE_URL:")
        terminal.process.write("\x05" + "\x7f" * 64 + url)
        terminal.wait_for(url)
        terminal.submit("")
        terminal.wait_for(MODEL)
        terminal.submit("")
        terminal.wait_for("Switched model to openai-compatible/" + MODEL)
        terminal.submit(GREETING)
        terminal.wait_for(REPLY)
        terminal.exit()
    # 恢复指定的对话，避免未经使用的新进程创建的空会话被认作前一条。
    import json

    matches = []
    for file in profile.rglob("sessions/session-*.json"):
        data = json.loads(file.read_bytes())
        if data.get("session_id") == file.stem and any(
            m.get("role") == "assistant" and REPLY in str(m.get("content", ""))
            for m in data.get("messages", [])
        ):
            matches.append(file.stem)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one saved conversation; found {len(matches)}.")
    with installed_terminal(exe, workspace, env) as terminal:
        terminal.submit("/resume " + matches[0])
        terminal.wait_for(REPLY)
        terminal.submit(FOLLOWUP)
        terminal.wait_for(RESTORED)
        terminal.exit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    with TemporaryDirectory() as profile, GreetingServer() as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            exercise(args.exe.resolve(strict=True), args.workspace.resolve(strict=True), Path(profile), server)
        finally:
            server.shutdown()
            worker.join()
    print("PASS: /connect, Chinese reply, clean exit, saved model and two-turn restart.")


if __name__ == "__main__":
    main()
