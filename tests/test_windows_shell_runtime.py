"""Windows shell execution and recursive cleanup contracts."""
from __future__ import annotations

import subprocess

from nz_coder.runtime.platform_runtime import terminate_process_tree


class _Process:
    pid = 4242

    def __init__(self) -> None:
        self.killed = False
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_windows_tree_cleanup_uses_bounded_taskkill_fallback():
    calls = []
    process = _Process()

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    terminate_process_tree(process, os_name="nt", runner=run, force=True)

    assert calls[0][0] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert calls[0][1]["timeout"] == 5
    assert process.killed is False


def test_windows_tree_cleanup_falls_back_to_process_kill_when_taskkill_fails():
    process = _Process()

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)

    terminate_process_tree(process, os_name="nt", runner=run, force=True)

    assert process.killed is True
