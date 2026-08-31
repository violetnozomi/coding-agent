"""Validated inline attachments shared by tools, Session, and providers."""
from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

SUPPORTED_IMAGE_MIMES = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
SUPPORTED_DOCUMENT_MIMES = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
})
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\r\n]+)$"
)


def sniff_image_mime(sample: bytes) -> str:
    """Return the supported image MIME identified by a bounded file signature."""
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(sample) >= 12 and sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return "image/webp"
    return ""


def make_image_attachment(data: bytes, mime: str, *, filename: str = "") -> dict:
    """Build one bounded InfCode-style inline FilePart payload."""
    if mime not in SUPPORTED_IMAGE_MIMES:
        raise ValueError(f"Unsupported image MIME type: {mime}")
    if len(data) >= MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image size must be less than {MAX_IMAGE_BYTES // 1024 // 1024} MB "
            f"({len(data)} bytes)."
        )
    result = {
        "type": "file",
        "mime": mime,
        "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }
    if filename:
        result["filename"] = Path(filename).name[:500]
    return result


def normalize_attachments(value: Any) -> list[dict]:
    """Return safe bounded data-URL attachments; reject remote and malformed data."""
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("attachments must contain at most 4 files")
    result = []
    total_bytes = 0
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "file":
            raise ValueError("attachment must be a file object")
        mime = item.get("mime")
        url = item.get("url")
        if not isinstance(mime, str) or mime not in SUPPORTED_IMAGE_MIMES:
            raise ValueError(f"Unsupported attachment MIME type: {mime}")
        if not isinstance(url, str):
            raise ValueError("attachment URL must be a data URL")
        match = _DATA_URL_RE.fullmatch(url)
        if match is None or match.group("mime") != mime:
            raise ValueError("attachment must use a matching base64 data URL")
        try:
            decoded = base64.b64decode(match.group("data"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("attachment contains invalid base64 data") from exc
        if len(decoded) >= MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image size must be less than {MAX_IMAGE_BYTES // 1024 // 1024} MB "
                f"({len(decoded)} bytes)."
            )
        total_bytes += len(decoded)
        if total_bytes >= MAX_IMAGE_BYTES:
            raise ValueError(
                f"Combined image size must be less than "
                f"{MAX_IMAGE_BYTES // 1024 // 1024} MB ({total_bytes} bytes)."
            )
        clean = {"type": "file", "mime": mime, "url": url}
        filename = item.get("filename")
        if isinstance(filename, str) and filename:
            clean["filename"] = Path(filename).name[:500]
        result.append(clean)
    return result


def make_document_attachment(
    path: str,
    mime: str,
    *,
    filename: str,
    size: int,
    mtime_ns: int,
) -> dict:
    """Build one workspace-relative PDF/DOCX FilePart payload."""
    return normalize_document_attachments([{
        "type": "file",
        "mime": mime,
        "path": path,
        "filename": filename,
        "size": size,
        "mtime_ns": mtime_ns,
    }])[0]


def normalize_document_attachments(value: Any) -> list[dict]:
    """Validate bounded workspace-relative document FilePart payloads."""
    if value in (None, []):
        return []
    if not isinstance(value, list) or len(value) > 4:
        raise ValueError("document attachments must contain at most 4 files")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "file":
            raise ValueError("document attachment must be a file object")
        mime = item.get("mime")
        path = item.get("path")
        filename = item.get("filename")
        size = item.get("size")
        mtime_ns = item.get("mtime_ns")
        candidate = PurePosixPath(path) if isinstance(path, str) else None
        if mime not in SUPPORTED_DOCUMENT_MIMES:
            raise ValueError(f"Unsupported document MIME type: {mime}")
        if (
            candidate is None
            or candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in path
        ):
            raise ValueError("document path must be workspace-relative")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename:
            raise ValueError("document filename must be a basename")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size >= MAX_DOCUMENT_BYTES
        ):
            raise ValueError("Document size must be less than 10 MB")
        if not isinstance(mtime_ns, int) or isinstance(mtime_ns, bool) or mtime_ns < 0:
            raise ValueError("document mtime_ns must be a non-negative integer")
        result.append({
            "type": "file",
            "mime": mime,
            "path": candidate.as_posix(),
            "filename": filename[:500],
            "size": size,
            "mtime_ns": mtime_ns,
        })
    return result


def normalize_user_file_parts(value: Any) -> list[dict]:
    """Validate mixed image/document user FileParts while retaining order."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("file parts must be a list")
    images = [item for item in value if item.get("mime") not in SUPPORTED_DOCUMENT_MIMES]
    documents = [item for item in value if item.get("mime") in SUPPORTED_DOCUMENT_MIMES]
    normalized_images = iter(normalize_attachments(images))
    normalized_documents = iter(normalize_document_attachments(documents))
    return [
        next(normalized_documents)
        if item.get("mime") in SUPPORTED_DOCUMENT_MIMES
        else next(normalized_images)
        for item in value
    ]


def attachment_base64(attachment: dict) -> str:
    """Return the already-validated base64 body of a normalized attachment."""
    return str(attachment["url"]).split(",", 1)[1]


def openai_attachment_message(attachments: list[dict]) -> dict:
    """Create the separate user media turn required by OpenAI-compatible tools."""
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "The following images were returned by the preceding tool result.",
            },
            *[
                {"type": "image_url", "image_url": {"url": item["url"]}}
                for item in attachments
            ],
        ],
    }


def openai_chat_messages(messages: list[dict]) -> list[dict]:
    """Project user media and extract tool media after each complete result segment."""
    result: list[dict] = []
    pending: list[dict] = []

    def flush() -> None:
        if pending:
            result.append(openai_attachment_message(list(pending)))
            pending.clear()

    for message in messages:
        if message.get("role") != "tool":
            flush()
        clean = {
            key: value
            for key, value in message.items()
            if key not in {"_nz_attachments", "_nz_user_attachments"}
        }
        if message.get("role") == "user":
            user_files = normalize_attachments(message.get("_nz_user_attachments"))
            if user_files:
                content = clean.get("content", "")
                if isinstance(content, str):
                    content_parts = (
                        [{"type": "text", "text": content}]
                        if content
                        else []
                    )
                elif isinstance(content, list):
                    content_parts = list(content)
                else:
                    content_parts = [{"type": "text", "text": str(content)}]
                clean["content"] = [
                    *content_parts,
                    *[
                        {"type": "image_url", "image_url": {"url": item["url"]}}
                        for item in user_files
                    ],
                ]
        result.append(clean)
        if message.get("role") == "tool":
            pending.extend(normalize_attachments(message.get("_nz_attachments")))
    flush()
    return result
