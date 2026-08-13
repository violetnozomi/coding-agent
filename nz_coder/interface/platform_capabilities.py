"""Probe terminal product capabilities without performing destructive actions."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from typing import Callable

from rich.console import Console
from rich.table import Table

from nz_coder.private_paths import windows_private_acl_available


Status = dict[str, str]


def _status(value: str, detail: str, tier: str | None = None) -> Status:
    inferred = {"supported": "A", "partial": "B", "optional": "B", "unavailable": "C"}
    return {"status": value, "detail": detail, "tier": tier or inferred[value]}


def collect_platform_capabilities(
    *,
    platform: str | None = None,
    os_name: str | None = None,
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    module_available: Callable[[str], bool] | None = None,
    private_acl_available: Callable[[], bool] | None = None,
    terminal: bool | None = None,
) -> dict:
    """Return an honest support matrix for the current product installation."""
    selected_platform = sys.platform if platform is None else platform
    selected_os = os.name if os_name is None else os_name
    environment = dict(os.environ if environ is None else environ)
    is_terminal = (
        bool(getattr(sys.stdin, "isatty", lambda: False)())
        if terminal is None
        else bool(terminal)
    )
    if environment.get("WSL_DISTRO_NAME") or environment.get("WSL_INTEROP"):
        family = "wsl"
    elif selected_platform == "darwin":
        family = "macos"
    elif selected_os == "nt" or selected_platform.startswith("win"):
        family = "windows"
    elif selected_platform.startswith("linux"):
        family = "linux"
    else:
        family = "other"

    posix_terminal = selected_os == "posix" and family != "windows"
    clipboard_text, clipboard_image = _clipboard_capabilities(
        family, environment, which, is_terminal
    )
    editor = environment.get("VISUAL") or environment.get("EDITOR")
    editor_binary = str(editor or "").split()[0] if editor else ""
    discovered_editor = editor_binary or next(
        (name for name in ("code", "vim", "vi", "nano", "notepad") if which(name)),
        "",
    )
    lsp_servers = tuple(
        name
        for name in (
            "basedpyright-langserver",
            "pyright-langserver",
            "typescript-language-server",
            "clangd",
            "gopls",
            "rust-analyzer",
        )
        if which(name)
    )
    has_module = module_available or (
        lambda name: importlib.util.find_spec(name) is not None
    )
    has_tree_sitter = bool(has_module("tree_sitter"))
    has_conpty = family == "windows" and bool(has_module("winpty"))
    has_private_acl = family == "windows" and bool(
        (private_acl_available or windows_private_acl_available)()
    )
    shell_candidates = (
        ("pwsh.exe", "PowerShell 7"),
        ("pwsh", "PowerShell 7"),
        ("powershell.exe", "Windows PowerShell"),
        ("cmd.exe", "Command Prompt"),
    ) if family == "windows" else (("bash", "Bash"), ("sh", "POSIX sh"))
    shell_name = next((name for name, _label in shell_candidates if which(name)), "")
    shell_label = next((label for name, label in shell_candidates if name == shell_name), "")
    capabilities = {
        "terminal_ui": _status(
            "supported" if is_terminal else "partial",
            "interactive prompt_toolkit TUI"
            if is_terminal
            else "non-interactive input uses the line/headless surface",
        ),
        "clipboard_text": clipboard_text,
        "clipboard_image": clipboard_image,
        "file_attachments": _status(
            "supported", "workspace-confined files and validated image attachments"
        ),
        "external_editor": _status(
            "supported" if discovered_editor else "unavailable",
            f"editor: {discovered_editor}" if discovered_editor else "set VISUAL or EDITOR",
        ),
        "shell": _status(
            "supported" if shell_name else "unavailable",
            f"{shell_label}: {which(shell_name)}" if shell_name else "no supported shell found on PATH",
        ),
        "bash": _status(
            "supported" if shell_name else "unavailable",
            f"compatibility tool uses {shell_label}: {which(shell_name)}"
            if shell_name
            else "no compatible command shell was found on PATH",
        ),
        "process_service": _status(
            "supported" if posix_terminal or has_conpty else "partial",
            "PTY-backed persistent processes" if posix_terminal
            else "ConPTY-backed persistent processes" if has_conpty
            else "pipe-backed process fallback; terminal semantics are limited",
        ),
        "process_pty": _status(
            "supported" if posix_terminal or has_conpty else "unavailable",
            "POSIX PTY is available" if posix_terminal
            else "ConPTY is available through pywinpty" if has_conpty
            else "ConPTY dependency is unavailable; pipe mode is used",
            "A" if posix_terminal or has_conpty else "B",
        ),
        "terminal_resize": _status(
            "supported" if posix_terminal or has_conpty else "partial",
            "SIGWINCH/TIOCSWINSZ propagation" if posix_terminal
            else "ConPTY resize propagation" if has_conpty
            else "pipe fallback has no terminal resize channel",
        ),
        "signals": _status(
            "supported" if posix_terminal else "partial",
            "POSIX signal lifecycle"
            if posix_terminal
            else "Ctrl+C is supported; POSIX-only signals are unavailable",
        ),
        "process_tree": _status(
            "supported",
            "process-group termination" if family != "windows"
            else "Windows process-tree termination with taskkill fallback",
        ),
        "ctrl_c": _status("supported", "cancel current run; press twice while idle to exit"),
        "daemon": _status("supported", "authenticated loopback daemon lifecycle"),
        "token_permission": _status(
            "supported" if family != "windows" or has_private_acl else "partial",
            "owner-only token file and per-request authentication"
            if family != "windows"
            else "protected current-user-and-SYSTEM Windows DACL plus per-request authentication"
            if has_private_acl
            else "private user directory and owner checks; POSIX chmod is not a Windows ACL",
        ),
        "token_security": _status(
            "supported" if family != "windows" or has_private_acl else "partial",
            "owner-only token file" if family != "windows"
            else "protected current-user-and-SYSTEM Windows DACL"
            if has_private_acl
            else "private user directory and owner checks; Windows ACL hardening is unavailable",
        ),
        "http_attach": _status(
            "supported", "Session attach, reconnect, events, interactions, and files"
        ),
        "tree_sitter": _status(
            "supported" if has_tree_sitter else "optional",
            "tree-sitter runtime installed; language adapters are reported by `nz-coder doctor`"
            if has_tree_sitter
            else "tree-sitter runtime is not installed; Python AST and structural search remain available",
        ),
        "lsp": _status(
            "supported" if lsp_servers else "optional",
            ", ".join(lsp_servers) if lsp_servers else "no language server found on PATH",
        ),
        "mcp_stdio": _status(
            "supported",
            "argv-based stdio transport with platform-native executable resolution",
        ),
    }
    return {
        "schema_version": 1,
        "platform": family,
        "python_platform": selected_platform,
        "os_name": selected_os,
        "interactive_terminal": is_terminal,
        "capabilities": capabilities,
    }


def _clipboard_capabilities(
    family: str,
    environment: dict[str, str],
    which: Callable[[str], str | None],
    is_terminal: bool,
) -> tuple[Status, Status]:
    if family == "macos":
        text = _native_or_osc("pbcopy", which, is_terminal)
        image = _native_only("pngpaste", which)
    elif family == "windows":
        text = _native_or_osc("clip", which, is_terminal)
        image = _native_only("powershell.exe", which)
    elif family == "wsl":
        text = _native_or_osc("powershell.exe", which, is_terminal)
        image = _native_only("powershell.exe", which)
    else:
        text_helper = next(
            (name for name in ("wl-copy", "xclip", "xsel") if which(name)), ""
        )
        image_helper = next(
            (name for name in ("wl-paste", "xclip") if which(name)), ""
        )
        display = bool(environment.get("WAYLAND_DISPLAY") or environment.get("DISPLAY"))
        text = (
            _status("supported", f"native helper: {text_helper}")
            if text_helper and display
            else _status("partial", "OSC 52 terminal fallback")
            if is_terminal
            else _status("unavailable", "no display-aware clipboard helper found")
        )
        image = (
            _status("supported", f"native helper: {image_helper}")
            if image_helper and display
            else _status("unavailable", "image paste requires wl-paste or xclip and a display")
        )
    return text, image


def _native_or_osc(
    helper: str, which: Callable[[str], str | None], is_terminal: bool
) -> Status:
    if which(helper):
        return _status("supported", f"native helper: {helper}")
    if is_terminal:
        return _status("partial", "OSC 52 terminal fallback")
    return _status("unavailable", f"{helper} was not found")


def _native_only(helper: str, which: Callable[[str], str | None]) -> Status:
    return _status(
        "supported" if which(helper) else "unavailable",
        f"native helper: {helper}" if which(helper) else f"{helper} was not found",
    )


def platform_main(argv: list[str] | None = None) -> int:
    """Render the platform capability matrix for humans or automation."""
    parser = argparse.ArgumentParser(prog="nz-coder platform")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = collect_platform_capabilities()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    table = Table(title=f"NZ-Coder platform · {report['platform']}", expand=True)
    table.add_column("Capability", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Tier", no_wrap=True)
    table.add_column("Detail")
    for name, capability in report["capabilities"].items():
        table.add_row(
            name,
            capability["status"].upper(),
            capability["tier"],
            capability["detail"],
        )
    Console().print(table)
    return 0


__all__ = ["collect_platform_capabilities", "platform_main"]
