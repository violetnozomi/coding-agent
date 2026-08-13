"""Platform-neutral contracts for Windows shell, paths, and output decoding."""
from __future__ import annotations

from pathlib import Path

import pytest


def _which(mapping):
    return lambda name: mapping.get(name)


def test_windows_shell_prefers_pwsh_then_windows_powershell_then_cmd():
    from nz_coder.runtime.platform_runtime import ShellKind, select_shell

    pwsh = select_shell(
        os_name="nt", which=_which({"pwsh.exe": r"C:\Program Files\PowerShell\pwsh.exe"})
    )
    powershell = select_shell(
        os_name="nt", which=_which({"powershell.exe": r"C:\Windows\powershell.exe"})
    )
    cmd = select_shell(os_name="nt", which=_which({"cmd.exe": r"C:\Windows\cmd.exe"}))

    assert pwsh.kind is ShellKind.POWERSHELL
    assert pwsh.argv("git status") == (
        r"C:\Program Files\PowerShell\pwsh.exe",
        "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "git status",
    )
    assert powershell.kind is ShellKind.POWERSHELL
    assert cmd.kind is ShellKind.CMD
    assert cmd.argv("python -m pytest") == (
        r"C:\Windows\cmd.exe", "/d", "/s", "/c", "python -m pytest",
    )


def test_powershell_invokes_a_quoted_executable_with_call_operator():
    from nz_coder.runtime.platform_runtime import ShellKind, ShellSpec

    shell = ShellSpec(ShellKind.POWERSHELL, r"C:\Program Files\PowerShell\pwsh.exe")

    assert shell.argv(r'"C:\Program Files\Python\python.exe" -u -i')[-1] == (
        r'& "C:\Program Files\Python\python.exe" -u -i'
    )


def test_posix_shell_is_explicit_bash_or_sh_argv():
    from nz_coder.runtime.platform_runtime import ShellKind, select_shell

    shell = select_shell(os_name="posix", which=_which({"bash": "/bin/bash"}))

    assert shell.kind is ShellKind.BASH
    assert shell.argv("git status") == ("/bin/bash", "-lc", "git status")


def test_windows_stdio_executable_wraps_cmd_and_powershell_scripts():
    from nz_coder.runtime.platform_runtime import executable_argv

    mapping = {
        "typescript-language-server": r"C:\npm\typescript-language-server.cmd",
        "server.ps1": r"C:\tools\server.ps1",
        "cmd.exe": r"C:\Windows\cmd.exe",
        "pwsh.exe": r"C:\PowerShell\pwsh.exe",
    }
    which = _which(mapping)
    cmd = executable_argv(
        ("typescript-language-server", "--stdio"), os_name="nt", which=which,
    )
    ps1 = executable_argv(("server.ps1", "--stdio"), os_name="nt", which=which)
    assert cmd[:5] == (r"C:\Windows\cmd.exe", "/d", "/s", "/c", r"C:\npm\typescript-language-server.cmd --stdio")
    assert ps1 == (
        r"C:\PowerShell\pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-File", r"C:\tools\server.ps1", "--stdio",
    )


@pytest.mark.parametrize(
    ("candidate", "workspace", "expected"),
    [
        (r"C:\Repo\src\main.py", r"c:\repo", True),
        (r"C:\Repo With Space\代码\[a]#.py", r"c:\repo with space", True),
        (r"C:\repo2\main.py", r"C:\repo", False),
        (r"C:\repo\..\outside.py", r"C:\repo", False),
        (r"D:\代码\项目\a.py", r"D:\代码\项目", True),
        (r"\\server\share\repo\x.py", r"\\server\share\repo", True),
        (r"\\server\share2\repo\x.py", r"\\server\share\repo", False),
    ],
)
def test_windows_workspace_containment_is_drive_and_case_aware(
    candidate, workspace, expected
):
    from nz_coder.runtime.platform_runtime import is_within_workspace

    assert is_within_workspace(candidate, workspace, platform="windows") is expected


def test_process_output_decodes_utf8_utf16_and_legacy_without_exceptions():
    from nz_coder.runtime.platform_runtime import decode_process_output

    assert decode_process_output("中文 日本語 🚀".encode("utf-8")) == "中文 日本語 🚀"
    assert decode_process_output("中文警告".encode("utf-16")) == "中文警告"
    assert decode_process_output("中文错误".encode("gb18030"), preferred_encoding="gb18030") == "中文错误"
    assert "�" in decode_process_output(b"bad\x81", preferred_encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "preferred", "expected"),
    [
        ("中文输出 日本語 🚀".encode("utf-8"), None, "中文输出 日本語 🚀"),
        ("PowerShell 中文".encode("utf-16"), None, "PowerShell 中文"),
        ("中文错误".encode("gbk"), "cp936", "中文错误"),
        (b"prefix\x81suffix", "utf-8", "prefix�suffix"),
    ],
)
def test_process_output_preserves_multilingual_diagnostics(payload, preferred, expected):
    from nz_coder.runtime.platform_runtime import decode_process_output

    assert decode_process_output(payload, preferred_encoding=preferred) == expected


@pytest.mark.parametrize(
    ("encoding", "text"),
    [
        ("utf-16-le", "PowerShell 中文 output"),
        ("utf-16-be", "PowerShell 日本語 output"),
    ],
)
def test_process_output_detects_utf16_without_bom(encoding, text):
    from nz_coder.runtime.platform_runtime import decode_process_output

    assert decode_process_output(text.encode(encoding)) == text


def test_process_output_uses_native_windows_ansi_and_oem_candidates(monkeypatch):
    import nz_coder.runtime.platform_runtime as platform_runtime

    monkeypatch.setattr(
        platform_runtime,
        "_windows_code_page_encodings",
        lambda: ("cp932", "cp437"),
    )
    monkeypatch.setattr(
        platform_runtime.locale,
        "getpreferredencoding",
        lambda _setlocale=False: "utf-8",
    )
    text = "日本語エラー"

    assert platform_runtime.decode_process_output(text.encode("cp932")) == text


def test_posix_workspace_containment_uses_resolved_path_boundary(tmp_path: Path):
    from nz_coder.runtime.platform_runtime import is_within_workspace

    workspace = tmp_path / "repo"
    workspace.mkdir()
    assert is_within_workspace(workspace / "src.py", workspace, platform="posix")
    assert not is_within_workspace(tmp_path / "repo2" / "src.py", workspace, platform="posix")


def test_workspace_containment_resolves_existing_link_and_new_child_parent(tmp_path: Path):
    from nz_coder.runtime.platform_runtime import is_within_workspace

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"host cannot create directory symlinks: {exc}")

    assert not is_within_workspace(link / "secret.txt", workspace, platform="posix")
    assert not is_within_workspace(link / "new" / "file.txt", workspace, platform="posix")
    assert not is_within_workspace(link / "secret.txt", workspace, platform="windows")
    assert not is_within_workspace(link / "new" / "file.txt", workspace, platform="windows")
