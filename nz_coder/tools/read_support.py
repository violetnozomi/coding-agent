"""Bounded text/directory primitives shared by the ``read_file`` tool."""
from __future__ import annotations

import locale
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_LINE_SUFFIX = f"... (line truncated to {MAX_LINE_LENGTH} chars)"
MAX_READ_BYTES = 50 * 1024
SAMPLE_BYTES = 4096

_BINARY_EXTENSIONS = frozenset({
    ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".class", ".jar",
    ".war", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".bin", ".dat", ".obj", ".o", ".a", ".lib",
    ".wasm", ".pyc", ".pyo",
})


@dataclass(frozen=True)
class TextReadResult:
    """One bounded, line-oriented text-file read."""

    lines: list[str]
    count: int
    offset: int
    cut: bool
    more: bool
    encoding: str


def is_binary_file(path: Path, sample: bytes) -> bool:
    """Match InfCode's extension and control-byte binary classification."""
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    if not sample:
        return False
    if _has_wide_bom(sample):
        return False
    controls = 0
    for value in sample:
        if value == 0:
            return True
        if value < 9 or 13 < value < 32:
            controls += 1
    return controls / len(sample) > 0.3


def read_text_lines(path: Path, *, offset: int, limit: int) -> TextReadResult:
    """Read lines with InfCode's 2,000-line, long-line, and 50 KiB rules."""
    encoding = _bom_encoding(_sample(path)) or "utf-8"
    try:
        with path.open("r", encoding=encoding, errors="strict", newline=None) as stream:
            return _read_stream(stream, offset=offset, limit=limit, encoding=encoding)
    except UnicodeDecodeError:
        data = path.read_bytes()
        text, detected = _decode_legacy(data)
        return _read_stream(text.splitlines(), offset=offset, limit=limit, encoding=detected)


def directory_entries(path: Path) -> list[str]:
    """Return locale-sorted direct children with directory suffixes."""
    entries = []
    for item in path.iterdir():
        entries.append(item.name + "/" if item.is_dir() else item.name)
    return sorted(entries, key=locale.strxfrm)


def missing_path_message(path: Path, requested: str) -> str:
    """Return a missing-file error with at most three nearby sibling names."""
    base = path.name.lower()
    suggestions = []
    try:
        children = list(path.parent.iterdir())
    except OSError:
        children = ()
    for child in children:
        name = child.name.lower()
        if base in name or name in base:
            suggestions.append(str(Path(requested).parent / child.name))
            if len(suggestions) == 3:
                break
    message = f"File not found: {requested}"
    if suggestions:
        message += "\n\nDid you mean one of these?\n" + "\n".join(suggestions)
    return message


_WARM_LOCK = threading.Lock()
_WARM_CAPACITY = threading.BoundedSemaphore(2)
_WARM_PENDING: set[tuple[Path, Path]] = set()


def warm_lsp(path: Path, workspace: Path) -> None:
    """Best-effort asynchronous equivalent of InfCode's forked LSP touch."""
    from nz_coder.foundation.workspace_trust import current_config_snapshot

    config_snapshot = current_config_snapshot(workspace)
    key = (path.resolve(), workspace.resolve())
    with _WARM_LOCK:
        if key in _WARM_PENDING:
            return
        _WARM_PENDING.add(key)
    if not _WARM_CAPACITY.acquire(blocking=False):
        with _WARM_LOCK:
            _WARM_PENDING.discard(key)
        return

    def run() -> None:
        try:
            from nz_coder.lsp import get_client_for_file

            try:
                client = get_client_for_file(
                    key[0], key[1], config_snapshot=config_snapshot,
                )
            except TypeError as exc:
                if "config_snapshot" not in str(exc):
                    raise
                client = get_client_for_file(key[0], key[1])
            if client is not None:
                client.open_document(key[0])
        except Exception:
            pass
        finally:
            with _WARM_LOCK:
                _WARM_PENDING.discard(key)
            _WARM_CAPACITY.release()

    worker = threading.Thread(
        target=run,
        name="nz-lsp-warm",
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError:
        with _WARM_LOCK:
            _WARM_PENDING.discard(key)
        _WARM_CAPACITY.release()


def _read_stream(stream, *, offset: int, limit: int, encoding: str) -> TextReadResult:
    start = offset - 1
    selected: list[str] = []
    byte_count = 0
    count = 0
    cut = False
    more = False
    for raw_line in stream:
        count += 1
        if count <= start:
            continue
        if len(selected) >= limit:
            more = True
            continue
        line = raw_line.rstrip("\r\n")
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH] + MAX_LINE_SUFFIX
        size = len(line.encode("utf-8")) + (1 if selected else 0)
        if byte_count + size > MAX_READ_BYTES:
            cut = True
            more = True
            break
        selected.append(line)
        byte_count += size
    return TextReadResult(selected, count, offset, cut, more, encoding)


def _sample(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(SAMPLE_BYTES)


def _bom_encoding(data: bytes) -> str | None:
    if data.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32"
    if data.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8"
    return None


def _has_wide_bom(data: bytes) -> bool:
    return data.startswith((
        b"\x00\x00\xfe\xff",
        b"\xff\xfe\x00\x00",
        b"\xff\xfe",
        b"\xfe\xff",
    ))


def _decode_legacy(data: bytes) -> tuple[str, str]:
    preferred = locale.getpreferredencoding(False) or "utf-8"
    candidates = [preferred, "gb18030", "shift_jis", "big5", "cp1252", "latin-1"]
    decoded = []
    seen = {"utf-8"}
    for encoding in candidates:
        key = encoding.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            text = data.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
        decoded.append((_text_score(text), text, encoding))
    if not decoded:
        return data.decode("utf-8", errors="replace"), "utf-8-replace"
    _score, text, encoding = max(decoded, key=lambda item: item[0])
    return text, encoding


def _text_score(text: str) -> float:
    """Prefer printable, assigned text and plausible CJK over mojibake."""
    sample = text[:64 * 1024]
    if not sample:
        return 0.0
    score = 0.0
    for char in sample:
        category = unicodedata.category(char)
        codepoint = ord(char)
        if char in "\n\r\t" or category[0] in {"L", "N", "P", "Z"}:
            score += 1.0
        if 0x4E00 <= codepoint <= 0x9FFF:
            score += 0.3
        if category in {"Cc", "Cs", "Cn"}:
            score -= 4.0
        if 0x80 <= codepoint <= 0x9F:
            score -= 2.0
    return score / len(sample)
