"""InfCode-aligned PDF/DOCX user-turn document preflight tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import threading
import shutil
import zipfile

import pytest

from nz_coder.foundation import config
from nz_coder.protocol.attachments import make_document_attachment
from nz_coder.state.context import prompt_budget
from nz_coder.capabilities.documents import DOCX_MIME, PDF_MIME, DocumentReadResult, read_document
from nz_coder.protocol.message_schema import (
    PARTS_KEY,
    attach_file_parts,
    attach_message_identity,
    message_records,
)
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.state.input_expansion import (
    resolve_and_apply_budget,
    tag_file_attachments,
)


def _write_docx(path, paragraphs=("Title", "First paragraph", "Second paragraph")):
    body = "".join(
        "<w:p><w:r><w:t>" + text + "</w:t></w:r></w:p>"
        for text in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml)


def _write_pdf(path):
    stream = b"BT /F1 12 Tf 72 720 Td (Hello PDF document) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii"))
        payload.extend(body + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(bytes(payload))


def _document_part(path, workspace, mime=DOCX_MIME):
    stat = path.stat()
    return make_document_attachment(
        path.relative_to(workspace).as_posix(),
        mime,
        filename=path.name,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _user_with_document(path, workspace, message_id="msg-document-user"):
    message = {"role": "user", "content": "Summarize this document."}
    attach_message_identity(message, message_id, session_id="session-document")
    attach_file_parts(message, [_document_part(path, workspace)])
    return message


def _assistant(message_id="msg-document-owner"):
    message = {"role": "assistant", "content": ""}
    attach_message_identity(message, message_id, session_id="session-document")
    return message


def _harness(reader) -> AgentLoop:
    agent = object.__new__(AgentLoop)
    agent.document_reader = reader
    agent.workdir = None
    agent.session_id = "session-document"
    agent.tracer = SimpleNamespace(log=lambda *args, **kwargs: None)
    agent._emit_session_event = lambda *args, **kwargs: None
    agent._checkpoint_messages = lambda *args, **kwargs: None
    return agent


def test_terminal_attachment_routes_docx_to_durable_filepart(tmp_path):
    document = tmp_path / "report.docx"
    _write_docx(document)
    message = {"role": "user", "content": "review"}

    count = tag_file_attachments(
        message,
        "review",
        [SimpleNamespace(path="report.docx", size=document.stat().st_size)],
        workspace=tmp_path,
        session_id="session-document",
    )
    resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=64_000, output_tokens=8_000),
        tmp_path,
    )

    assert count == 1
    assert message["_nz_input_expansions"][0]["kind"] == "document"
    assert "document_read preflight" in message["content"]
    part = next(item for item in message[PARTS_KEY] if item["type"] == "file")
    assert part["mime"] == DOCX_MIME
    assert part["path"] == "report.docx"
    assert "url" not in part


def test_docx_reader_extracts_text_and_reuses_sidecar(tmp_path, monkeypatch):
    document = tmp_path / "report.docx"
    _write_docx(document)
    part = _document_part(document, tmp_path)

    first = asyncio.run(read_document(
        part,
        workspace=tmp_path,
        session_id="session-document",
    ))
    monkeypatch.setattr(
        "nz_coder.capabilities.documents._extract_docx",
        lambda _path: (_ for _ in ()).throw(AssertionError("cache was not reused")),
    )
    second = asyncio.run(read_document(
        part,
        workspace=tmp_path,
        session_id="session-document",
    ))

    assert first.status == second.status == "completed"
    assert "Title\n\nFirst paragraph" in first.text
    assert second.text == first.text


def test_invalid_docx_and_missing_pdf_converter_are_explicit_errors(
    tmp_path,
    monkeypatch,
):
    invalid = tmp_path / "broken.docx"
    invalid.write_bytes(b"not a zip")
    invalid_result = asyncio.run(read_document(
        _document_part(invalid, tmp_path),
        workspace=tmp_path,
        session_id="session-document",
    ))

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.setattr("nz_coder.capabilities.documents.shutil.which", lambda _name: None)
    pdf_result = asyncio.run(read_document(
        _document_part(pdf, tmp_path, PDF_MIME),
        workspace=tmp_path,
        session_id="session-document",
    ))

    assert invalid_result.status == "error"
    assert "valid Office Open XML" in invalid_result.error
    assert pdf_result.status == "error"
    assert "pdftotext" in pdf_result.error


def test_pdf_reader_uses_optional_system_converter(tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext is not installed")
    pdf = tmp_path / "paper.pdf"
    _write_pdf(pdf)

    result = asyncio.run(read_document(
        _document_part(pdf, tmp_path, PDF_MIME),
        workspace=tmp_path,
        session_id="session-pdf",
    ))

    assert result.status == "completed"
    assert "Hello PDF document" in result.text


def test_document_reader_rejects_changed_source(tmp_path):
    document = tmp_path / "report.docx"
    _write_docx(document)
    part = _document_part(document, tmp_path)
    document.write_bytes(document.read_bytes() + b"changed")

    result = asyncio.run(read_document(
        part,
        workspace=tmp_path,
        session_id="session-document",
    ))

    assert result.status == "error"
    assert "changed after it was attached" in result.error


def test_document_reader_cancellation_settles_worker_without_cache(
    tmp_path,
    monkeypatch,
):
    document = tmp_path / "report.docx"
    _write_docx(document)
    part = _document_part(document, tmp_path)
    started = threading.Event()

    def slow_extract(_path, cancel_event):
        started.set()
        cancel_event.wait(2)
        raise RuntimeError("worker stopped")

    monkeypatch.setattr("nz_coder.capabilities.documents._extract_docx", slow_extract)

    async def scenario():
        task = asyncio.create_task(read_document(
            part,
            workspace=tmp_path,
            session_id="session-document-cancel",
        ))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    cache = (
        tmp_path / ".nz-coder" / "sessions" / "session-document-cancel"
        / "documents" / ".cache"
    )
    assert not cache.exists() or not list(cache.glob("*.md"))


def test_document_preflight_is_durable_idempotent_and_reinjected(tmp_path):
    document = tmp_path / "report.docx"
    _write_docx(document)
    calls = 0

    async def reader(_part, *, workspace, session_id):
        nonlocal calls
        calls += 1
        assert workspace == tmp_path
        assert session_id == "session-document"
        return DocumentReadResult("# Report\n\nImportant result.")

    agent = _harness(reader)
    agent.workdir = tmp_path
    source = {"role": "user", "content": "Summarize this document."}
    tag_file_attachments(
        source,
        "Summarize this document.",
        [SimpleNamespace(path="report.docx", size=document.stat().st_size)],
        workspace=tmp_path,
        session_id="session-document",
    )
    owner = _assistant()
    messages = [source, owner]

    assert asyncio.run(agent._prepare_user_documents(messages, owner)) == "converted"
    part = next(
        item for item in owner[PARTS_KEY]
        if item.get("metadata", {}).get("document_read")
    )
    detail = part["metadata"]["document_read"]
    assert detail["status"] == "completed"
    assert detail["source_message_id"] == source["_nz_message_id"]
    assert '<document_read filename="report.docx" path="report.docx">' in part["text"]
    assert "Important result" in part["text"]

    agent.model_capabilities = ModelCapabilities(provider="test", model_id="text")
    projected = agent._sanitize_messages(messages)
    assert "Important result" in projected[0]["content"]
    assert "queued for document_read preflight" not in projected[0]["content"]
    assert "_nz_user_attachments" not in projected[0]

    second_owner = _assistant("msg-document-owner-2")
    messages.append(second_owner)
    assert asyncio.run(
        agent._prepare_user_documents(messages, second_owner)
    ) == "skipped"
    assert calls == 1


def test_document_preflight_is_independent_of_vision_capability(tmp_path):
    document = tmp_path / "report.docx"
    _write_docx(document)

    async def reader(_part, **_kwargs):
        return DocumentReadResult("Document text")

    agent = _harness(reader)
    agent.workdir = tmp_path
    agent.model_capabilities = ModelCapabilities(
        provider="test",
        model_id="vision",
        supports_image_input=True,
    )
    source = {"role": "user", "content": "Summarize this document."}
    tag_file_attachments(
        source,
        "Summarize this document.",
        [SimpleNamespace(path="report.docx", size=document.stat().st_size)],
        workspace=tmp_path,
        session_id="session-document",
    )
    owner = _assistant()

    assert asyncio.run(
        agent._prepare_user_documents([source, owner], owner)
    ) == "converted"


def test_document_preflight_persists_interrupted_terminal_part(tmp_path):
    first = tmp_path / "one.docx"
    second = tmp_path / "two.docx"
    _write_docx(first, ("First",))
    _write_docx(second, ("Second",))
    started_second = asyncio.Event()

    async def reader(part, **_kwargs):
        if part["filename"] == "one.docx":
            return DocumentReadResult("First document text")
        started_second.set()
        await asyncio.Event().wait()
        return DocumentReadResult("unreachable")

    source = {"role": "user", "content": "Read both."}
    attach_message_identity(source, "msg-doc-cancel", session_id="session-document")
    attach_file_parts(source, [
        _document_part(first, tmp_path),
        _document_part(second, tmp_path),
    ])
    owner = _assistant("msg-doc-cancel-owner")
    agent = _harness(reader)
    agent.workdir = tmp_path

    async def scenario():
        task = asyncio.create_task(
            agent._prepare_user_documents([source, owner], owner)
        )
        await started_second.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    part = next(
        item for item in owner[PARTS_KEY]
        if item.get("metadata", {}).get("document_read")
    )
    detail = part["metadata"]["document_read"]
    assert detail["status"] == "interrupted"
    assert [item["status"] for item in detail["items"]] == ["completed", "error"]
    assert "First document text" in part["text"]
    assert "Second" not in part["text"]


def test_document_filepart_and_metadata_survive_session_projection(tmp_path):
    document = tmp_path / "report.docx"
    _write_docx(document)
    source = _user_with_document(document, tmp_path)
    owner = _assistant()
    document_part = next(item for item in source[PARTS_KEY] if item["type"] == "file")
    owner[PARTS_KEY].append({
        "id": "part-document-read",
        "message_id": owner["_nz_message_id"],
        "type": "text",
        "text": '<document_read filename="report.docx" path="report.docx">ok</document_read>',
        "metadata": {"document_read": {
            "status": "completed",
            "source_message_id": source["_nz_message_id"],
            "items": [{
                "source_id": document_part["id"],
                "filename": "report.docx",
                "status": "completed",
            }],
        }},
    })

    records = message_records([source, owner], "session-document")

    file_record = next(item for item in records[0]["parts"] if item["type"] == "file")
    assert file_record["path"] == "report.docx"
    metadata = records[1]["parts"][0]["metadata"]["document_read"]
    assert metadata["status"] == "completed"


def test_agent_run_converts_document_before_main_request(tmp_path, monkeypatch):
    document = tmp_path / "report.docx"
    _write_docx(document, ("Release Report", "All checks passed."))
    requests = []

    class Provider:
        name = "test"

        def capabilities(self, model_id):
            return ModelCapabilities(provider="test", model_id=model_id)

        def create_client(self):
            return object()

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(
                content="The release report says all checks passed.",
                tool_calls=[],
                reasoning_content=None,
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        provider=Provider(),
        client=object(),
        trace_enabled=False,
        session_id="session-document",
    )
    source = {"role": "user", "content": "Summarize this document."}
    tag_file_attachments(
        source,
        "Summarize this document.",
        [SimpleNamespace(path="report.docx", size=document.stat().st_size)],
        workspace=tmp_path,
        session_id="session-document",
    )
    messages = [source]

    result = asyncio.run(agent.run(messages, stream=False))

    assert result["status"] == "completed"
    user_request = next(
        message for message in requests[0]["messages"] if message["role"] == "user"
    )
    assert '<document_read filename="report.docx" path="report.docx">' in user_request["content"]
    assert "All checks passed." in user_request["content"]
    assert "queued for document_read preflight" not in user_request["content"]
    assert "PK" not in user_request["content"]
    owner = next(message for message in messages if message["role"] == "assistant")
    assert any(
        part.get("metadata", {}).get("document_read", {}).get("status") == "completed"
        for part in owner[PARTS_KEY]
    )
    agent.close()
