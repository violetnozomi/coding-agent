from __future__ import annotations

import json

from nz_coder.interface.platform_capabilities import (
    collect_platform_capabilities,
    platform_main,
)


def _which(*available: str):
    names = set(available)
    return lambda name: f"/usr/bin/{name}" if name in names else None


def test_linux_wayland_reports_native_clipboard_and_pty_support():
    report = collect_platform_capabilities(
        platform="linux",
        os_name="posix",
        environ={"WAYLAND_DISPLAY": "wayland-0", "EDITOR": "vim"},
        which=_which("wl-copy", "wl-paste", "bash", "basedpyright-langserver"),
        terminal=True,
    )

    assert report["platform"] == "linux"
    assert report["capabilities"]["terminal_ui"]["status"] == "supported"
    assert report["capabilities"]["clipboard_text"]["status"] == "supported"
    assert report["capabilities"]["clipboard_image"]["status"] == "supported"
    assert report["capabilities"]["process_pty"]["status"] == "supported"
    assert report["capabilities"]["terminal_resize"]["status"] == "supported"
    assert report["capabilities"]["lsp"]["status"] == "supported"


def test_headless_linux_does_not_claim_a_working_clipboard():
    report = collect_platform_capabilities(
        platform="linux",
        os_name="posix",
        environ={},
        which=_which("bash"),
        terminal=False,
        module_available=lambda _name: False,
    )

    assert report["capabilities"]["terminal_ui"]["status"] == "partial"
    assert report["capabilities"]["clipboard_text"]["status"] == "unavailable"
    assert report["capabilities"]["clipboard_image"]["status"] == "unavailable"
    assert report["capabilities"]["external_editor"]["status"] == "unavailable"
    assert report["capabilities"]["tree_sitter"]["status"] == "optional"


def test_windows_reports_pipe_fallback_without_claiming_pty():
    report = collect_platform_capabilities(
        platform="win32",
        os_name="nt",
        environ={"EDITOR": "notepad"},
        which=_which("clip", "powershell.exe"),
        terminal=True,
    )

    assert report["platform"] == "windows"
    assert report["capabilities"]["process_service"]["status"] == "partial"
    assert report["capabilities"]["process_pty"]["status"] == "unavailable"
    assert report["capabilities"]["terminal_resize"]["status"] == "partial"
    assert "ConPTY" in report["capabilities"]["process_pty"]["detail"]
    assert report["capabilities"]["process_pty"]["tier"] == "B"
    assert report["capabilities"]["shell"]["tier"] == "A"
    assert report["capabilities"]["process_tree"]["tier"] == "A"
    assert report["capabilities"]["token_security"]["tier"] == "B"


def test_windows_reports_conpty_tier_a_when_pywinpty_is_installed():
    report = collect_platform_capabilities(
        platform="win32",
        os_name="nt",
        environ={"EDITOR": "code --wait"},
        which=_which("pwsh.exe", "clip", "code", "pyright-langserver"),
        terminal=True,
        module_available=lambda name: name in {"tree_sitter", "winpty"},
    )

    assert report["capabilities"]["process_service"]["tier"] == "A"
    assert report["capabilities"]["process_pty"]["tier"] == "A"
    assert report["capabilities"]["terminal_resize"]["tier"] == "A"
    assert report["capabilities"]["external_editor"]["tier"] == "A"
    assert report["capabilities"]["tree_sitter"]["tier"] == "A"


def test_windows_reports_token_tier_a_only_when_acl_adapter_is_available():
    report = collect_platform_capabilities(
        platform="win32",
        os_name="nt",
        environ={"EDITOR": "notepad"},
        which=_which("powershell.exe"),
        terminal=True,
        module_available=lambda _name: False,
        private_acl_available=lambda: True,
    )

    assert report["capabilities"]["token_permission"]["tier"] == "A"
    assert report["capabilities"]["token_security"]["tier"] == "A"
    assert "DACL" in report["capabilities"]["token_security"]["detail"]


def test_wsl_is_identified_separately_and_uses_powershell_clipboard():
    report = collect_platform_capabilities(
        platform="linux",
        os_name="posix",
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
        which=_which("bash", "powershell.exe"),
        terminal=True,
    )

    assert report["platform"] == "wsl"
    assert report["capabilities"]["clipboard_image"]["status"] == "supported"
    assert "powershell.exe" in report["capabilities"]["clipboard_image"]["detail"]
    assert report["capabilities"]["process_pty"]["status"] == "supported"


def test_platform_command_has_stable_json_contract(capsys):
    assert platform_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == 1
    assert payload["platform"] in {"linux", "macos", "windows", "wsl", "other"}
    assert {"terminal_ui", "daemon", "http_attach", "process_service"} <= set(
        payload["capabilities"]
    )
