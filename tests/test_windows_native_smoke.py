"""Native Windows host smoke; collected on Linux but executed only on Windows CI."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="requires a native Windows host")


def test_first_startup_imports_terminal_and_reports_windows():
    from nz_coder.interface.platform_capabilities import collect_platform_capabilities
    from nz_coder.interface.fullscreen import FullscreenComposer
    from prompt_toolkit.completion import DummyCompleter
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.styles import Style

    assert collect_platform_capabilities()["platform"] == "windows"

    async def start_and_close() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "",
                status_provider=lambda: "idle",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            await composer.start_async()
            assert composer.application.is_running
            await composer.close_async()

    asyncio.run(start_and_close())


def test_powershell_tool_utf8_and_warning_semantics(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.bash import run_bash
    with scoped_workdir(tmp_path):
        result = run_bash("Write-Output '中文 日本語 🚀'; Write-Warning 'warning'; exit 0")
    assert "中文 日本語" in str(result)
    assert result.metadata["exit"] == 0
    assert result.metadata["shell_kind"] == "powershell"


@pytest.mark.parametrize("executable", ["pwsh.exe", "powershell.exe"])
def test_powershell_versions_preserve_multilingual_output(executable):
    from nz_coder.runtime.process.platform_runtime import decode_process_output

    resolved = shutil.which(executable)
    assert resolved is not None, f"{executable} must be installed on the Windows RC runner"
    completed = subprocess.run(
        [
            resolved,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$OutputEncoding=[Text.UTF8Encoding]::new($false);"
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                "Write-Output '中文 日本語 🚀'"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    output = decode_process_output(completed.stdout)
    assert completed.returncode == 0
    assert "中文" in output and "日本語" in output and "🚀" in output


def test_windows_powershell_utf16_without_bom_is_decoded():
    from nz_coder.runtime.process.platform_runtime import decode_process_output

    text = "PowerShell 中文 日本語 output"
    environment = os.environ.copy()
    environment["NZ_RC_TEXT"] = text
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$b=[Text.Encoding]::Unicode.GetBytes($env:NZ_RC_TEXT);"
                "[Console]::OpenStandardOutput().Write($b,0,$b.Length)"
            ),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode == 0
    assert decode_process_output(completed.stdout) == text


def test_native_windows_private_acl_round_trip(tmp_path):
    from nz_coder.foundation.private_paths import (
        harden_private_path,
        inspect_private_path,
    )

    state = tmp_path / "private-state"
    state.mkdir()
    token = state / "token"
    token.write_text("not-a-real-token", encoding="utf-8")

    directory_result = harden_private_path(state)
    token_result = harden_private_path(token)

    assert directory_result.hardened is True, directory_result.detail
    assert token_result.hardened is True, token_result.detail
    assert inspect_private_path(state).hardened is True
    assert inspect_private_path(token).hardened is True


@pytest.mark.parametrize("directory", ["My Project (a)[b]#", "代码项目🚀"])
def test_space_path_and_cjk_path_file_edit(tmp_path: Path, directory: str):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import write_file, read_file
    target = tmp_path / directory
    target.mkdir()
    relative = f"{directory}/main.py"
    with scoped_workdir(tmp_path):
        assert not str(write_file(relative, "print('ok')\n")).startswith("Error: ")
        assert "print('ok')" in read_file(relative)


def test_junction_escape_blocks_existing_read_and_new_file_write(tmp_path: Path):
    from nz_coder.runtime.process.platform_runtime import decode_process_output
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import read_file, write_file

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret", encoding="utf-8")
    link = workspace / "external"
    environment = os.environ.copy()
    environment["NZ_RC_LINK"] = str(link)
    environment["NZ_RC_TARGET"] = str(outside)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$null=New-Item -ItemType Junction -Path $env:NZ_RC_LINK -Target $env:NZ_RC_TARGET",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0, decode_process_output(completed.stdout)

    with scoped_workdir(workspace):
        assert str(read_file("external/secret.txt")).startswith("Error: ")
        assert str(write_file("external/new.txt", "blocked")).startswith("Error: ")
    assert not (outside / "new.txt").exists()


def test_persistent_process_and_conpty_repl_and_ctrl_c(tmp_path):
    from nz_coder.runtime.process.process_service import ProcessService
    service = ProcessService(tmp_path, kill_grace_seconds=0.1)
    try:
        handle = service.start(
            f'"{sys.executable}" -u -i', cwd=tmp_path,
            owner_session_id="windows", tty=True,
        )
        service.write(handle.process_id, "print('READY-中文')\n", owner_session_id="windows")
        cursor = 0
        observed = ""
        deadline = time.monotonic() + 3
        while "READY-中文" not in observed and time.monotonic() < deadline:
            output = service.read(
                handle.process_id,
                owner_session_id="windows",
                cursor=cursor,
                wait_seconds=min(0.5, max(0.0, deadline - time.monotonic())),
            )
            observed += output.output
            cursor = output.next_cursor
        assert "READY-中文" in observed
        service.write(handle.process_id, "\x03", owner_session_id="windows")
        assert service.get(handle.process_id, owner_session_id="windows").tty is True
    finally:
        service.close()
    assert service.list(active_only=True) == []


def test_native_windows_pipe_child_reports_job_object_binding(tmp_path):
    from nz_coder.runtime.process.process_backends import create_process_backend

    backend = create_process_backend(
        f'"{sys.executable}" -c "import time; time.sleep(30)"',
        cwd=tmp_path,
        tty=False,
        rows=24,
        cols=80,
    )
    try:
        assert backend.lifecycle_mode == "windows-job-object"
    finally:
        backend.terminate_tree(grace_seconds=0.1)
        backend.close()


def test_conpty_resize(tmp_path):
    from nz_coder.runtime.process.process_service import ProcessService
    service = ProcessService(tmp_path, kill_grace_seconds=0.1)
    try:
        handle = service.start(
            f'"{sys.executable}" -u -i', cwd=tmp_path,
            owner_session_id="windows", tty=True,
        )
        for rows, cols in ((24, 80), (40, 120), (60, 200)):
            assert service.resize(
                handle.process_id, rows=rows, cols=cols,
                owner_session_id="windows",
            ).tty
    finally:
        service.close()


def test_clipboard_capability_is_explicit():
    from nz_coder.interface.platform_capabilities import collect_platform_capabilities
    report = collect_platform_capabilities()
    assert report["capabilities"]["clipboard_text"]["tier"] in {"A", "B"}


def test_lsp_resolution_accepts_windows_pathext_wrappers():
    from nz_coder.lsp.servers import resolve_server

    expected = {
        "app.py": "python",
        "app.ts": "typescript",
        "app.go": "go",
    }
    for filename, language in expected.items():
        resolved = resolve_server(Path(filename), Path.cwd())
        assert resolved is not None, f"missing installed {language} language server"
        assert resolved.language_id == language
        assert Path(resolved.command[0]).suffix.lower() in {".exe", ".cmd", ".bat", ".ps1"}


def test_tree_sitter_default_language_wheels_import_on_windows():
    import tree_sitter  # noqa: F401
    import tree_sitter_go  # noqa: F401
    import tree_sitter_javascript  # noqa: F401
    import tree_sitter_typescript  # noqa: F401


def test_mcp_stdio_runtime_imports_on_windows():
    from nz_coder.mcp.client import MCPClient

    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    client = MCPClient(
        name="windows-stdio",
        command=(sys.executable, str(fixture)),
        cwd=Path.cwd(),
        startup_timeout_seconds=5,
        tool_timeout_seconds=2,
    )
    try:
        result = client.start()
        assert result["serverInfo"]["name"] == "test-echo"
        assert client.call_tool("echo", {"value": "windows"})["content"][0]["text"] == "echo:windows"
    finally:
        client.close()
