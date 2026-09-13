"""Terminal clipboard writing with native helpers and OSC 52 fallback."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from nz_coder.protocol.attachments import MAX_IMAGE_BYTES, sniff_image_mime
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.user_paths import (
    prepare_user_storage,
    private_attachment_reference,
)


_MAX_CLIPBOARD_BYTES = 1_000_000


@dataclass(frozen=True)
class ClipboardImage:
    """One validated image obtained from a native system clipboard."""

    data: bytes
    mime: str
    source: str


def read_image(
    *, runner=subprocess.run, which=shutil.which,
    platform: str | None = None, os_name: str | None = None,
    environ: dict | None = None,
) -> ClipboardImage | None:
    """Read a bounded clipboard image using installed platform-native helpers."""
    selected_platform = sys.platform if platform is None else platform
    selected_os = os.name if os_name is None else os_name
    environment = os.environ if environ is None else environ
    commands: list[tuple[str, ...]] = []
    if selected_platform == "darwin":
        commands.append(("pngpaste", "-"))
    elif selected_os == "nt" or environment.get("WSL_DISTRO_NAME"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$i=[Windows.Forms.Clipboard]::GetImage();"
            "if($null -eq $i){exit 1};"
            "$m=New-Object IO.MemoryStream;"
            "$i.Save($m,[Drawing.Imaging.ImageFormat]::Png);"
            "$b=$m.ToArray();"
            "[Console]::OpenStandardOutput().Write($b,0,$b.Length)"
        )
        commands.append(("powershell.exe", "-NoProfile", "-Command", script))
    else:
        commands.extend((
            ("wl-paste", "--no-newline", "--type", "image/png"),
            ("xclip", "-selection", "clipboard", "-t", "image/png", "-o"),
        ))
    for command in commands:
        if which(command[0]) is None:
            continue
        try:
            result = runner(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=3.0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        data = bytes(result.stdout or b"")
        mime = sniff_image_mime(data[:16])
        if result.returncode == 0 and mime and len(data) < MAX_IMAGE_BYTES:
            return ClipboardImage(data, mime, command[0])
    return None


def persist_image(workspace: str | Path, image: ClipboardImage) -> str:
    """Persist an image in user state and return an opaque attachment reference."""
    if not isinstance(image, ClipboardImage):
        raise TypeError("image must be ClipboardImage")
    mime = sniff_image_mime(image.data[:16])
    if not mime or mime != image.mime or len(image.data) >= MAX_IMAGE_BYTES:
        raise ValueError("Clipboard image is invalid or exceeds the image limit")
    root = Path(workspace).resolve(strict=True)
    directory = prepare_user_storage(root).workspace_state / "attachments"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    harden_private_path(directory)
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}[mime]
    digest = hashlib.sha256(image.data).hexdigest()[:16]
    target = directory / f"clipboard-{digest}.{extension}"
    descriptor, temporary = tempfile.mkstemp(prefix=".clipboard-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(image.data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        harden_private_path(target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return private_attachment_reference(target.name)


def copy_text(text: str) -> bool:
    """Copy bounded UTF-8 text using InfCode-compatible terminal strategies."""
    payload = str(text).encode("utf-8")
    if len(payload) > _MAX_CLIPBOARD_BYTES:
        raise ValueError("Clipboard content exceeds 1,000,000 bytes")

    copied = _write_osc52(payload)
    for command in _native_copy_commands():
        if shutil.which(command[0]) is None:
            continue
        try:
            result = subprocess.run(
                command,
                input=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3.0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return copied


def _write_osc52(payload: bytes) -> bool:
    stream = sys.stdout
    if not getattr(stream, "isatty", lambda: False)():
        return False
    encoded = base64.b64encode(payload).decode("ascii")
    sequence = f"\x1b]52;c;{encoded}\x07"
    if os.environ.get("TMUX") or os.environ.get("STY"):
        sequence = f"\x1bPtmux;\x1b{sequence}\x1b\\"
    try:
        stream.write(sequence)
        stream.flush()
    except (OSError, UnicodeError):
        return False
    return True


def _native_copy_commands() -> tuple[tuple[str, ...], ...]:
    if sys.platform == "darwin":
        return (("pbcopy",),)
    if os.name == "nt":
        return (("clip",),)
    return (
        ("wl-copy",),
        ("xclip", "-selection", "clipboard"),
        ("xsel", "--clipboard", "--input"),
    )
