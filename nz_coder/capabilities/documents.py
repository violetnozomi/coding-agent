"""Bounded PDF/DOCX conversion for user-turn document preflight.

DOCX extraction uses only the standard library. PDF extraction delegates to
the optional system ``pdftotext`` executable; no Python package is required.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.protocol.attachments import (
    MAX_DOCUMENT_BYTES,
    SUPPORTED_DOCUMENT_MIMES,
    normalize_document_attachments,
)

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MIME = "application/pdf"
DOCUMENT_DEFAULT_READ_LIMIT = 2000
DOCUMENT_MAX_PAGES = 20
DOCUMENT_CONVERT_TIMEOUT_SECONDS = 120
DOCUMENT_MAX_TEXT_CHARS = 200_000
_MAX_CONVERTED_BYTES = 4 * 1024 * 1024
_MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
_MAX_DOCX_TOTAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class DocumentReadResult:
    """One terminal local document conversion result."""

    text: str
    status: str = "completed"
    error: str = ""
    more: bool = False
    total_lines: int = 0
    read_lines: int = 0
    total_pages: int | None = None
    read_pages: int | None = None


@dataclass(frozen=True)
class DocumentPageRange:
    """Validated 1-based inclusive PDF page range."""

    start: int
    end: int
    label: str


def parse_document_pages(value: str) -> DocumentPageRange:
    """Parse InfCode's ``pages=\"5\"`` / ``pages=\"1-10\"`` contract."""
    text = str(value).strip()
    if not text:
        raise ValueError('Invalid pages value: expected "5" or "1-10"')
    pieces = text.split("-", 1)
    if len(pieces) == 1 and pieces[0].isdigit():
        page = int(pieces[0])
        if page < 1:
            raise ValueError(f"Invalid page number: {value}")
        return DocumentPageRange(page, page, str(page))
    if (
        len(pieces) == 2
        and pieces[0].isdigit()
        and pieces[1].isdigit()
    ):
        start, end = (int(item) for item in pieces)
        if start < 1 or end < start:
            raise ValueError(f"Invalid page range: {value}")
        if end - start + 1 > DOCUMENT_MAX_PAGES:
            raise ValueError(
                f"Page range exceeds maximum of {DOCUMENT_MAX_PAGES} pages per read"
            )
        return DocumentPageRange(start, end, f"{start}-{end}")
    raise ValueError(f'Invalid pages format: {value}. Use "5" or "1-10".')


def read_document_file(
    path: str,
    *,
    workspace: Path,
    session_id: str,
    pages: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
    cancel_event: threading.Event | None = None,
) -> DocumentReadResult:
    """Synchronously read a workspace PDF/DOCX for the ``read_file`` tool."""
    root = Path(workspace).resolve()
    source = (root / path).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return _error("Document path escapes workspace")
    if source.is_symlink() or not source.is_file():
        return _error("Document path is not a regular workspace file")
    stat = source.stat()
    if stat.st_size >= MAX_DOCUMENT_BYTES:
        return _error(
            f"Document exceeds 10 MB limit ({stat.st_size} bytes). "
            "All PDF and DOCX files must be less than 10 MB."
        )
    mime = detect_document_mime(source)
    if mime not in SUPPORTED_DOCUMENT_MIMES:
        return _error(f"Unsupported document type: {mime or 'unknown'}")
    try:
        requested_pages = parse_document_pages(pages) if pages and mime == PDF_MIME else None
        return _convert_and_slice(
            source,
            root,
            str(session_id or "default"),
            mime,
            cancel_event or threading.Event(),
            requested_pages=requested_pages,
            clamp_pages=False,
            offset=_validated_offset(offset),
            limit=_validated_limit(limit),
        )
    except Exception as exc:
        return _error(str(exc) or type(exc).__name__)


def detect_document_mime(path: Path) -> str:
    """Detect PDF signature or use the supported document extension fallback."""
    try:
        with path.open("rb") as stream:
            sample = stream.read(16)
    except OSError:
        return ""
    if sample.startswith(b"%PDF-") or path.suffix.lower() == ".pdf":
        return PDF_MIME
    return DOCX_MIME if path.suffix.lower() == ".docx" else ""


async def read_document(
    attachment: dict,
    *,
    workspace: Path,
    session_id: str,
) -> DocumentReadResult:
    """Convert one validated document without blocking the Agent event loop."""
    normalized = normalize_document_attachments([attachment])[0]
    cancel_event = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        _read_document_sync,
        normalized,
        Path(workspace).resolve(),
        str(session_id),
        cancel_event,
    ))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await task
        except (_DocumentInterrupted, Exception):
            pass
        raise


def render_document_hints(
    items: list[dict],
    documents: list[dict],
    text_by_source: dict[str, str],
) -> str:
    """Render document terminal states as model-visible source-user hints."""
    document_by_id = {
        str(document.get("id")): document
        for document in documents
    }
    blocks = []
    for item in items:
        source_id = str(item.get("source_id") or "")
        document = document_by_id.get(source_id, {})
        filename = str(item.get("filename") or document.get("filename") or "document")
        path = str(document.get("path") or filename)
        if item.get("status") == "completed":
            body = text_by_source.get(source_id, "")
        elif item.get("status") == "error":
            body = f"Document read failed: {item.get('error') or 'unknown error'}"
        else:
            continue
        blocks.append(
            f'<document_read filename="{html.escape(filename, quote=True)}" '
            f'path="{html.escape(path, quote=True)}">\n'
            f"{body}\n"
            "</document_read>"
        )
    return "\n\n".join(blocks)


def _read_document_sync(
    attachment: dict,
    workspace: Path,
    session_id: str,
    cancel_event: threading.Event,
) -> DocumentReadResult:
    _check_cancelled(cancel_event)
    source = WorkspacePathPolicy(workspace).validate_model_read(attachment["path"])
    try:
        source.relative_to(workspace)
    except ValueError:
        return _error("Document path escapes workspace")
    if source.is_symlink() or not source.is_file():
        return _error("Document path is not a regular workspace file")
    stat = source.stat()
    if stat.st_size >= MAX_DOCUMENT_BYTES:
        return _error(
            f"Document exceeds 10 MB limit ({stat.st_size} bytes). "
            "All PDF and DOCX files must be less than 10 MB."
        )
    if stat.st_size != attachment["size"] or stat.st_mtime_ns != attachment["mtime_ns"]:
        return _error("Document changed after it was attached; attach it again")
    mime = detect_document_mime(source)
    if mime != attachment["mime"] or mime not in SUPPORTED_DOCUMENT_MIMES:
        return _error("Document signature no longer matches its attached MIME type")

    result = _convert_and_slice(
        source,
        workspace,
        session_id,
        mime,
        cancel_event,
        requested_pages=None,
        clamp_pages=True,
        offset=1,
        limit=DOCUMENT_DEFAULT_READ_LIMIT,
    )
    if result.status != "completed":
        return result
    text = result.text
    if result.more:
        next_offset = 1 + result.read_lines
        text += (
            f"\n\n(Showing lines 1-{result.read_lines} of {result.total_lines}. "
            f"Use read_file on {attachment['path']} with offset={next_offset} "
            "to continue.)"
        )
    if result.total_pages and result.read_pages and result.read_pages < result.total_pages:
        next_page = result.read_pages + 1
        next_end = min(result.total_pages, result.read_pages + DOCUMENT_MAX_PAGES)
        text += (
            f"\n\n(Note: this PDF has {result.total_pages} pages, but only the first "
            f"{result.read_pages} were read automatically. To read more, use read_file "
            f'on {attachment["path"]} with pages="{next_page}-{next_end}" '
            f"(max {DOCUMENT_MAX_PAGES} pages per call).)"
        )
    return DocumentReadResult(
        text=text,
        more=result.more,
        total_lines=result.total_lines,
        read_lines=result.read_lines,
        total_pages=result.total_pages,
        read_pages=result.read_pages,
    )


def _convert_and_slice(
    source: Path,
    workspace: Path,
    session_id: str,
    mime: str,
    cancel_event: threading.Event,
    *,
    requested_pages: DocumentPageRange | None,
    clamp_pages: bool,
    offset: int,
    limit: int,
) -> DocumentReadResult:
    """Convert one source revision, cache it, then apply line pagination."""
    _check_cancelled(cancel_event)
    stat = source.stat()
    cache_dir = (
        prepare_user_storage(workspace).workspace_cache
        / "documents"
        / session_id
    )
    total_pages = None
    read_pages = None
    effective_pages = requested_pages
    if mime == PDF_MIME:
        total_pages = _pdf_page_count(source, cancel_event)
        if requested_pages and total_pages and requested_pages.end > total_pages:
            return _error(
                f"Page range {requested_pages.label} exceeds document page count "
                f"({total_pages})"
            )
        if requested_pages is None and total_pages and total_pages > DOCUMENT_MAX_PAGES:
            if not clamp_pages:
                return _error(
                    f"PDF has {total_pages} pages. This is a backend pagination limit, "
                    f"not a problem with the file: read at most {DOCUMENT_MAX_PAGES} "
                    f'pages per call. Call read_file with pages="1-{DOCUMENT_MAX_PAGES}" '
                    "to read the first section, then continue with later ranges."
                )
            effective_pages = DocumentPageRange(1, DOCUMENT_MAX_PAGES, f"1-{DOCUMENT_MAX_PAGES}")
        elif requested_pages is None:
            last = total_pages or DOCUMENT_MAX_PAGES
            effective_pages = DocumentPageRange(1, last, f"1-{last}")
        read_pages = effective_pages.end - effective_pages.start + 1
        if total_pages:
            read_pages = min(read_pages, total_pages - effective_pages.start + 1)

    page_label = effective_pages.label if effective_pages and mime == PDF_MIME else "all"
    path_key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    page_key = hashlib.sha256(page_label.encode("utf-8")).hexdigest()[:8]
    revision_key = hashlib.sha256(
        f"{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
    ).hexdigest()[:16]
    cache_file = cache_dir / f"{path_key}-{page_key}-{revision_key}.md"
    try:
        cached = cache_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        cached = ""
    except OSError as exc:
        return _error(f"Document cache read failed: {exc}")
    _check_cancelled(cancel_event)
    if not cached:
        try:
            if mime == DOCX_MIME:
                converted = _extract_docx(source, cancel_event)
            else:
                converted = _extract_pdf(
                    source,
                    cache_dir,
                    cancel_event,
                    effective_pages.start,
                    effective_pages.end,
                )
        except _DocumentInterrupted:
            raise
        except Exception as exc:
            return _error(str(exc) or type(exc).__name__)
        if not converted.strip():
            return _error(
                "Could not extract any text from this document. It is likely "
                "scanned or image-only; provide a text-based PDF/DOCX instead."
            )
        cache_dir.mkdir(parents=True, exist_ok=True)
        _check_cancelled(cancel_event)
        temporary = cache_dir / f".{path_key}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(converted, encoding="utf-8")
            os.replace(temporary, cache_file)
            _prune_stale_sidecars(cache_dir, path_key, page_key, cache_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        cached = converted

    return _slice_converted_document(
        cached,
        offset=offset,
        limit=limit,
        total_pages=total_pages,
        read_pages=read_pages,
    )


def _slice_converted_document(
    converted: str,
    *,
    offset: int,
    limit: int,
    total_pages: int | None,
    read_pages: int | None,
) -> DocumentReadResult:
    lines = converted.splitlines()
    if offset > len(lines) and not (not lines and offset == 1):
        return _error(
            f"Offset {offset} is out of range for this document ({len(lines)} lines)"
        )
    start = max(offset - 1, 0)
    visible = lines[start:start + limit]
    more = start + len(visible) < len(lines)
    text = "\n".join(visible)
    if len(text) > DOCUMENT_MAX_TEXT_CHARS:
        text = text[:DOCUMENT_MAX_TEXT_CHARS]
        more = True
    return DocumentReadResult(
        text=text,
        more=more,
        total_lines=len(lines),
        read_lines=len(visible),
        total_pages=total_pages,
        read_pages=read_pages,
    )


def _validated_offset(value: int | None) -> int:
    if value is None or value == 0:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be a non-negative integer")
    return value


def _validated_limit(value: int | None) -> int:
    if value is None:
        return DOCUMENT_DEFAULT_READ_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("limit must be a non-negative integer")
    return value


def _prune_stale_sidecars(
    cache_dir: Path,
    path_key: str,
    page_key: str,
    keep: Path,
) -> None:
    """Keep only the current source revision for this path/page cache family."""
    for candidate in cache_dir.glob(f"{path_key}-{page_key}-*.md"):
        if candidate == keep:
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def _extract_docx(path: Path, cancel_event: threading.Event) -> str:
    _check_cancelled(cancel_event)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000 or sum(info.file_size for info in infos) > _MAX_DOCX_TOTAL_BYTES:
                raise ValueError("DOCX expanded content exceeds the safe extraction limit")
            names = {info.filename.replace("\\", "/") for info in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise ValueError("This DOCX is not a valid Office Open XML document")
            info = next(
                item for item in infos
                if item.filename.replace("\\", "/") == "word/document.xml"
            )
            if info.file_size > _MAX_DOCX_XML_BYTES:
                raise ValueError("DOCX document.xml exceeds the safe extraction limit")
            payload = archive.read(info)
    except zipfile.BadZipFile as exc:
        raise ValueError(
            "This DOCX is not a valid Office Open XML document"
        ) from exc
    if b"<!DOCTYPE" in payload.upper():
        raise ValueError("DOCX document.xml contains a forbidden document type")
    _check_cancelled(cancel_event)
    root = ET.fromstring(payload)
    paragraphs = []
    for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        _check_cancelled(cancel_event)
        chunks = []
        for node in paragraph.iter():
            local = node.tag.rsplit("}", 1)[-1]
            if local == "t" and node.text:
                chunks.append(node.text)
            elif local == "tab":
                chunks.append("\t")
            elif local in {"br", "cr"}:
                chunks.append("\n")
        text = "".join(chunks).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _extract_pdf(
    path: Path,
    cache_dir: Path,
    cancel_event: threading.Event,
    start_page: int,
    end_page: int,
) -> str:
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError(
            "PDF conversion requires the optional system 'pdftotext' executable"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = cache_dir / f".pdf-{uuid.uuid4().hex}.txt"
    error_file = cache_dir / f".pdf-{uuid.uuid4().hex}.err"
    try:
        with error_file.open("wb") as errors:
            process = subprocess.Popen(
                [
                    executable,
                    "-f", str(start_page),
                    "-l", str(end_page),
                    "-layout",
                    str(path),
                    str(output),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                env=build_sanitized_subprocess_env(),
            )
            deadline = time.monotonic() + DOCUMENT_CONVERT_TIMEOUT_SECONDS
            while process.poll() is None:
                if cancel_event.wait(0.02):
                    _stop_process(process)
                    raise _DocumentInterrupted
                if time.monotonic() >= deadline:
                    _stop_process(process)
                    raise RuntimeError(
                        f"Document convert timeout after "
                        f"{DOCUMENT_CONVERT_TIMEOUT_SECONDS}s"
                    )
            returncode = process.returncode
        if returncode != 0:
            detail = error_file.read_text(
                encoding="utf-8",
                errors="replace",
            )[:1000].strip()
            raise RuntimeError(f"PDF conversion failed: {detail or returncode}")
        _check_cancelled(cancel_event)
        with output.open("rb") as stream:
            payload = stream.read(_MAX_CONVERTED_BYTES + 1)
        if len(payload) > _MAX_CONVERTED_BYTES:
            raise RuntimeError("Converted PDF text exceeds the 4 MB safety limit")
        return payload.decode("utf-8", errors="replace")
    finally:
        for temporary in (output, error_file):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _pdf_page_count(path: Path, cancel_event: threading.Event) -> int | None:
    _check_cancelled(cancel_event)
    executable = shutil.which("pdfinfo")
    if not executable:
        return None
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [executable, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=build_sanitized_subprocess_env(),
        )
        deadline = time.monotonic() + 10
        while process.poll() is None:
            if cancel_event.wait(0.02):
                _stop_process(process)
                raise _DocumentInterrupted
            if time.monotonic() >= deadline:
                _stop_process(process)
                return None
        if process.returncode != 0:
            return None
        output.seek(0)
        payload = output.read(64 * 1024 + 1)
    if len(payload) > 64 * 1024:
        return None
    for line in payload.decode("utf-8", errors="replace").splitlines():
        if line.lower().startswith("pages:"):
            try:
                count = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
            return count if count > 0 else None
    return None


class _DocumentInterrupted(Exception):
    """Internal cooperative stop used by the conversion worker."""


def _check_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise _DocumentInterrupted


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _error(message: str) -> DocumentReadResult:
    return DocumentReadResult(text="", status="error", error=str(message)[:4000])
