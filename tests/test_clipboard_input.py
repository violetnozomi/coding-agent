"""Product contracts for image paste and terminal path-drop attachments."""
from __future__ import annotations

import os
import subprocess

from nz_coder.interface.commands import build_default_registry
from nz_coder.interface.terminal_input import TerminalInput


PNG = b"\x89PNG\r\n\x1a\nimage-bytes"


def test_linux_clipboard_image_uses_available_native_helper():
    from nz_coder.interface.clipboard import read_image

    calls = []

    def runner(command, **_kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout=PNG, stderr=b"")

    image = read_image(
        runner=runner,
        which=lambda name: f"/usr/bin/{name}" if name == "wl-paste" else None,
        platform="linux",
        os_name="posix",
        environ={},
    )
    assert image is not None
    assert image.mime == "image/png"
    assert image.data == PNG
    assert calls[0][0] == "wl-paste"


def test_macos_clipboard_image_uses_pngpaste():
    from nz_coder.interface.clipboard import read_image

    commands = []
    image = read_image(
        runner=lambda command, **_kwargs: (
            commands.append(tuple(command))
            or subprocess.CompletedProcess(command, 0, stdout=PNG, stderr=b"")
        ),
        which=lambda name: f"/usr/local/bin/{name}",
        platform="darwin",
        os_name="posix",
        environ={},
    )
    assert image is not None
    assert commands == [("pngpaste", "-")]


def test_windows_clipboard_image_uses_argument_safe_powershell():
    from nz_coder.interface.clipboard import read_image

    commands = []
    image = read_image(
        runner=lambda command, **_kwargs: (
            commands.append(tuple(command))
            or subprocess.CompletedProcess(command, 0, stdout=PNG, stderr=b"")
        ),
        which=lambda name: f"C:/{name}",
        platform="win32",
        os_name="nt",
        environ={},
    )
    assert image is not None
    assert commands[0][:3] == ("powershell.exe", "-NoProfile", "-Command")


def test_clipboard_image_missing_helper_bad_mime_and_oversize_fail_soft():
    from nz_coder.protocol.attachments import MAX_IMAGE_BYTES
    from nz_coder.interface.clipboard import read_image

    assert read_image(
        which=lambda _name: None, platform="linux", os_name="posix", environ={},
    ) is None
    payloads = (b"not an image", PNG + b"x" * MAX_IMAGE_BYTES)
    for payload in payloads:
        assert read_image(
            runner=lambda command, **_kwargs: subprocess.CompletedProcess(
                command, 0, stdout=payload, stderr=b"",
            ),
            which=lambda _name: "/bin/helper",
            platform="darwin", os_name="posix", environ={},
        ) is None


def test_clipboard_image_persistence_is_private_and_workspace_local(tmp_path):
    from nz_coder.interface.clipboard import ClipboardImage, persist_image
    from nz_coder.foundation.private_paths import inspect_private_path

    relative = persist_image(tmp_path, ClipboardImage(PNG, "image/png", "test"))
    path = tmp_path / relative
    assert path.read_bytes() == PNG
    assert path.resolve().is_relative_to(tmp_path.resolve())
    assert inspect_private_path(path).hardened is True
    assert inspect_private_path(path.parent).hardened is True


def test_clipboard_image_hardens_cache_and_final_attachment(tmp_path, monkeypatch):
    import nz_coder.interface.clipboard as clipboard

    hardened = []
    monkeypatch.setattr(
        clipboard,
        "harden_private_path",
        lambda path: hardened.append(os.fspath(path)),
    )
    relative = clipboard.persist_image(
        tmp_path,
        clipboard.ClipboardImage(PNG, "image/png", "test"),
    )

    assert os.fspath(tmp_path / ".nz-coder" / "attachments") in hardened
    assert os.fspath(tmp_path / relative) in hardened


def test_ctrl_v_falls_back_to_image_callback_when_text_clipboards_are_empty(monkeypatch):
    from nz_coder.interface.terminal_input import _build_key_bindings

    pasted = []
    bindings = _build_key_bindings(paste_image=lambda: pasted.append(True) or True)
    handler = next(
        binding.handler for binding in bindings.bindings if binding.keys == ("c-v",)
    )

    class Event:
        current_buffer = type("Buffer", (), {"insert_text": lambda *_args: None})()
        app = type("App", (), {
            "clipboard": type("Clipboard", (), {
                "get_data": lambda _self: type("Data", (), {"text": ""})(),
            })(),
            "output": object(),
            "invalidate": lambda _self: None,
        })()

    monkeypatch.setattr(
        "nz_coder.interface.terminal_input._system_clipboard_text", lambda: "",
    )
    handler(Event())
    assert pasted == [True]


def test_terminal_image_paste_queues_existing_attachment_pipeline(monkeypatch, tmp_path):
    from nz_coder.interface.clipboard import ClipboardImage

    terminal = TerminalInput(
        console=object(), registry=build_default_registry(), workspace=tmp_path,
        interactive=False,
    )
    monkeypatch.setattr(
        "nz_coder.interface.clipboard.read_image",
        lambda: ClipboardImage(PNG, "image/png", "test"),
    )
    assert terminal.paste_clipboard_image() is True
    content, attachments = terminal.prepare_submission("inspect this")
    assert "clipboard-" in content
    assert len(attachments) == 1


def test_plain_file_drop_becomes_attachments_without_absolute_path_leak(tmp_path):
    (tmp_path / "one.py").write_text("one", encoding="utf-8")
    (tmp_path / "two file.txt").write_text("two", encoding="utf-8")
    terminal = TerminalInput(
        console=object(), registry=build_default_registry(), workspace=tmp_path,
        interactive=False,
    )
    text, attachments = terminal.prepare_submission(
        f"{tmp_path / 'one.py'} '{tmp_path / 'two file.txt'}'"
    )
    assert [item.path for item in attachments] == ["one.py", "two file.txt"]
    assert str(tmp_path) not in text
    assert "Please inspect the attached file(s)." in text


def test_windows_file_drop_tokenizer_preserves_backslashes_and_removes_quotes():
    from nz_coder.interface.terminal_input import _split_dropped_paths

    assert _split_dropped_paths(
        r"C:\repo\one.py 'C:\repo\two file.txt'", os_name="nt",
    ) == (r"C:\repo\one.py", r"C:\repo\two file.txt")


def test_file_drop_rejects_outside_and_symlink_paths(tmp_path):
    outside = tmp_path.parent / "outside-drop.txt"
    outside.write_text("outside", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    terminal = TerminalInput(
        console=object(), registry=build_default_registry(), workspace=tmp_path,
        interactive=False,
    )
    for value in (str(outside), str(tmp_path / "link.txt")):
        text, attachments = terminal.prepare_submission(value)
        assert attachments == ()
        assert text == value
