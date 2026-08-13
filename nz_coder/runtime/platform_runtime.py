"""Cross-platform shell, path, decoding, and process lifecycle contracts.

This module keeps operating-system decisions out of tools and product surfaces.
Callers receive explicit argv values and never rely on ``shell=True`` semantics.
"""
from __future__ import annotations

import locale
import ntpath
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable


class ShellKind(str, Enum):
    """Shell families whose invocation rules are materially different."""

    BASH = "bash"
    SH = "sh"
    POWERSHELL = "powershell"
    CMD = "cmd"
    CUSTOM = "custom"


@dataclass(frozen=True)
class ShellSpec:
    """An executable shell and its safe, explicit command invocation."""

    kind: ShellKind
    executable: str

    def argv(self, command: str) -> tuple[str, ...]:
        if self.kind is ShellKind.POWERSHELL:
            return (
                self.executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            )
        if self.kind is ShellKind.CMD:
            return (self.executable, "/d", "/s", "/c", command)
        return (self.executable, "-lc", command)


def select_shell(
    *,
    os_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    custom: str | None = None,
) -> ShellSpec:
    """Select a shell deterministically for the current operating system."""
    selected_os = os.name if os_name is None else os_name
    if custom:
        executable = which(custom) or custom
        kind = _shell_kind(executable, selected_os)
        return ShellSpec(kind, executable)

    candidates = (
        ("pwsh.exe", ShellKind.POWERSHELL),
        ("pwsh", ShellKind.POWERSHELL),
        ("powershell.exe", ShellKind.POWERSHELL),
        ("powershell", ShellKind.POWERSHELL),
        ("cmd.exe", ShellKind.CMD),
        ("cmd", ShellKind.CMD),
    ) if selected_os == "nt" else (
        ("bash", ShellKind.BASH),
        ("sh", ShellKind.SH),
    )
    for name, kind in candidates:
        executable = which(name)
        if executable:
            return ShellSpec(kind, executable)
    family = "PowerShell or cmd.exe" if selected_os == "nt" else "bash or sh"
    raise RuntimeError(f"No supported shell found; install or expose {family} on PATH")


def _shell_kind(executable: str, os_name: str) -> ShellKind:
    name = ntpath.basename(executable).lower() if os_name == "nt" else Path(executable).name.lower()
    if name in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}:
        return ShellKind.POWERSHELL
    if name in {"cmd", "cmd.exe"}:
        return ShellKind.CMD
    if name == "bash":
        return ShellKind.BASH
    if name == "sh":
        return ShellKind.SH
    return ShellKind.CUSTOM


def decode_process_output(data: bytes | str, *, preferred_encoding: str | None = None) -> str:
    """Decode terminal output without losing Unicode or raising on legacy bytes."""
    if isinstance(data, str):
        return data
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    utf16 = _utf16_without_bom(data)
    if utf16:
        try:
            return data.decode(utf16)
        except UnicodeDecodeError:
            pass
    encodings: list[str] = ["utf-8"]
    if preferred_encoding:
        encodings.append(preferred_encoding)
    encodings.extend(_windows_code_page_encodings())
    system_encoding = locale.getpreferredencoding(False)
    if system_encoding:
        encodings.append(system_encoding)
    tried: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    fallback = preferred_encoding or system_encoding or "utf-8"
    try:
        return data.decode(fallback, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _utf16_without_bom(data: bytes) -> str | None:
    """Recognize common redirected UTF-16 output without treating binary as text."""
    if len(data) < 4 or len(data) % 2:
        return None
    pairs = len(data) // 2
    even_nuls = data[0::2].count(0)
    odd_nuls = data[1::2].count(0)
    threshold = max(2, pairs // 4)
    if odd_nuls >= threshold and even_nuls <= max(1, pairs // 16):
        return "utf-16-le"
    if even_nuls >= threshold and odd_nuls <= max(1, pairs // 16):
        return "utf-16-be"
    return None


def _windows_code_page_encodings() -> tuple[str, ...]:
    """Return active console, OEM, and ANSI codecs on native Windows."""
    if os.name != "nt":
        return ()
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        values = (
            int(kernel32.GetConsoleOutputCP()),
            int(kernel32.GetOEMCP()),
            int(kernel32.GetACP()),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return ()
    result: list[str] = []
    for value in values:
        if value > 0:
            codec = f"cp{value}"
            if codec not in result:
                result.append(codec)
    return tuple(result)


def is_within_workspace(
    candidate: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    *,
    platform: str | None = None,
) -> bool:
    """Return whether *candidate* is inside *workspace* using native path rules."""
    selected = platform or ("windows" if os.name == "nt" else "posix")
    if selected == "windows":
        child = ntpath.normcase(ntpath.abspath(ntpath.normpath(os.fspath(candidate))))
        root = ntpath.normcase(ntpath.abspath(ntpath.normpath(os.fspath(workspace))))
        try:
            lexical_match = ntpath.commonpath((child, root)) == root
        except ValueError:
            return False
        if not lexical_match:
            return False
        # Native Windows paths must also pass a resolved-filesystem check so a
        # junction/reparse point cannot turn a lexically safe child into an
        # outside target. PathLike values are resolved in tests on any host,
        # which keeps the security contract executable without pretending that
        # POSIX string simulation is native Windows evidence. ``strict=False``
        # follows every existing ancestor, including the nearest parent of a
        # file that is about to be created.
        if os.name == "nt" or isinstance(candidate, os.PathLike) or isinstance(workspace, os.PathLike):
            try:
                resolved_child = Path(candidate).resolve(strict=False)
                resolved_root = Path(workspace).resolve(strict=False)
                resolved_child.relative_to(resolved_root)
            except (OSError, ValueError):
                return False
        return True
    try:
        Path(candidate).resolve(strict=False).relative_to(Path(workspace).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def terminate_process_tree(
    process,
    *,
    os_name: str | None = None,
    runner=subprocess.run,
    force: bool = True,
) -> None:
    """Terminate an owned process tree without process-name scans."""
    selected_os = os.name if os_name is None else os_name
    try:
        if selected_os != "nt":
            selected_signal = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
            os.killpg(process.pid, selected_signal)
            return
        if not force:
            process.terminate()
            return
        completed = runner(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            process.kill()
    except (OSError, ProcessLookupError, subprocess.SubprocessError):
        try:
            process.kill() if force else process.terminate()
        except (OSError, ProcessLookupError):
            pass


def executable_argv(
    command,
    *,
    os_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:  # noqa: ANN001
    """Resolve stdio executables, including Windows cmd/PowerShell wrappers."""
    values = tuple(str(value) for value in command)
    if not values:
        raise ValueError("command cannot be empty")
    selected_os = os.name if os_name is None else os_name
    executable = which(values[0]) or values[0]
    resolved = (executable, *values[1:])
    if selected_os != "nt":
        return resolved
    suffix = ntpath.splitext(executable)[1].lower()
    if suffix in {".cmd", ".bat"}:
        shell = select_shell(os_name="nt", which=which)
        cmd_shell = shell
        if shell.kind is not ShellKind.CMD:
            cmd_path = which("cmd.exe") or which("cmd")
            if not cmd_path:
                raise RuntimeError("cmd.exe is required to launch .cmd/.bat stdio servers")
            cmd_shell = ShellSpec(ShellKind.CMD, cmd_path)
        return cmd_shell.argv(subprocess.list2cmdline(list(resolved)))
    if suffix == ".ps1":
        shell = select_shell(os_name="nt", which=which)
        if shell.kind is not ShellKind.POWERSHELL:
            raise RuntimeError("PowerShell is required to launch .ps1 stdio servers")
        return (
            shell.executable, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-File", *resolved,
        )
    return resolved


__all__ = [
    "ShellKind",
    "ShellSpec",
    "decode_process_output",
    "executable_argv",
    "is_within_workspace",
    "select_shell",
    "terminate_process_tree",
]
