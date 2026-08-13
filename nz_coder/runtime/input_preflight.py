"""Durable image and document preflight for provider-neutral Agent turns."""
from __future__ import annotations

import asyncio
import time
import uuid

from nz_coder.attachments import normalize_attachments
from nz_coder.documents import render_document_hints
from nz_coder.message_schema import (
    MESSAGE_ID_KEY,
    PARTS_KEY,
    is_synthetic_user_message,
    upsert_message_part,
)
from nz_coder.vision import (
    ProviderImageDescriber,
    describe_images,
    render_image_descriptions,
)


class ProductionInputPreflight:
    """Convert unsupported input media before the main coding request."""

    async def prepare_user_images(
        self, host, messages: list, assistant_message: dict,
    ) -> str:
        if bool(getattr(host.model_capabilities, "supports_image_input", False)):
            return "skipped"
        source = self._latest_user(messages, assistant_message)
        if source is None:
            return "skipped"
        file_parts = [
            part for part in source.get(PARTS_KEY, [])
            if isinstance(part, dict)
            and part.get("type") == "file"
            and str(part.get("mime") or "").startswith("image/")
        ]
        if not file_parts:
            return "skipped"
        source_id = source.get(MESSAGE_ID_KEY)
        if not isinstance(source_id, str):
            return "skipped"
        if self._settled_or_clear_stale(messages, source_id, "image_describe", {"completed"}):
            return "skipped"

        attachments = normalize_attachments(file_parts)
        part_id = f"part-{uuid.uuid4().hex}"
        started = time.time()

        def progress(state: dict) -> None:
            normalized = upsert_message_part(assistant_message, {
                "id": part_id,
                "message_id": assistant_message[MESSAGE_ID_KEY],
                "type": "text",
                "text": render_image_descriptions(state["items"]),
                "time": {
                    "start": started,
                    **(
                        {"end": time.time()}
                        if state["status"] in {"completed", "interrupted"}
                        else {}
                    ),
                },
                "metadata": {"image_describe": {
                    "status": state["status"],
                    "source_message_id": source_id,
                    "items": state["items"],
                }},
            })
            self._publish_part(host, messages, assistant_message, normalized)

        prompt = source.get("content") if isinstance(source.get("content"), str) else ""
        await describe_images(
            attachments,
            source_ids=[str(part["id"]) for part in file_parts],
            describe=host.image_describer,
            prompt=prompt,
            on_progress=progress,
        )
        host.tracer.log(
            "image_describe_completed",
            source_message_id=source_id,
            image_count=len(attachments),
        )
        return "described"

    async def prepare_user_documents(
        self, host, messages: list, assistant_message: dict,
    ) -> str:
        source = self._latest_user(messages, assistant_message)
        if source is None:
            return "skipped"
        documents = [
            part for part in source.get(PARTS_KEY, [])
            if isinstance(part, dict)
            and part.get("type") == "file"
            and part.get("mime") in {
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
        ]
        if not documents:
            return "skipped"
        source_id = source.get(MESSAGE_ID_KEY)
        if not isinstance(source_id, str):
            return "skipped"
        if self._settled_or_clear_stale(
            messages, source_id, "document_read", {"completed", "error"},
        ):
            return "skipped"

        part_id = f"part-{uuid.uuid4().hex}"
        started = time.time()
        items = [
            {
                "source_id": str(document["id"]),
                "filename": str(document.get("filename") or "document"),
                "status": "running",
            }
            for document in documents
        ]
        text_by_source: dict[str, str] = {}

        def persist(status: str) -> None:
            visible_items = (
                [item for item in items if item.get("status") == "completed"]
                if status == "interrupted"
                else items
            )
            normalized = upsert_message_part(assistant_message, {
                "id": part_id,
                "message_id": assistant_message[MESSAGE_ID_KEY],
                "type": "text",
                "text": render_document_hints(
                    visible_items, documents, text_by_source,
                ),
                "time": {
                    "start": started,
                    **(
                        {"end": time.time()}
                        if status in {"completed", "error", "interrupted"}
                        else {}
                    ),
                },
                "metadata": {"document_read": {
                    "status": status,
                    "source_message_id": source_id,
                    "items": items,
                }},
            })
            self._publish_part(host, messages, assistant_message, normalized)

        persist("running")
        try:
            for index, document in enumerate(documents):
                try:
                    result = await host.document_reader(
                        document,
                        workspace=host.workdir,
                        session_id=host.session_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    items[index] = {
                        **items[index],
                        "status": "error",
                        "error": str(exc)[:4000] or type(exc).__name__,
                    }
                    continue
                result_status = getattr(result, "status", "error")
                if result_status == "interrupted":
                    raise asyncio.CancelledError
                if result_status == "completed":
                    items[index] = {**items[index], "status": "completed"}
                    text_by_source[items[index]["source_id"]] = str(result.text)
                else:
                    items[index] = {
                        **items[index],
                        "status": "error",
                        "error": str(
                            getattr(result, "error", "Document read failed")
                        )[:4000],
                    }
        except asyncio.CancelledError:
            for index, item in enumerate(items):
                if item.get("status") == "running":
                    items[index] = {
                        **item,
                        "status": "error",
                        "error": "Interrupted before document read finished",
                    }
            persist("interrupted")
            raise
        persist("completed")
        host.tracer.log(
            "document_read_completed",
            source_message_id=source_id,
            document_count=len(documents),
            failed=sum(1 for item in items if item.get("status") == "error"),
        )
        return "converted"

    async def describe_read_results(
        self, host, dispatched: list, messages: list,
    ) -> bool:
        capabilities = getattr(host, "model_capabilities", None)
        if bool(getattr(capabilities, "supports_image_input", False)):
            return False
        prompt = self._last_user_text(messages)
        describer = getattr(host, "image_describer", None)
        if not callable(describer):
            describer = ProviderImageDescriber.configured()
        for _index, tool_call, result in dispatched:
            if result.name != "read_file" or result.dispatch_failed or not result.attachments:
                continue
            try:
                attachments = normalize_attachments(result.attachments)
            except ValueError:
                continue
            call_id = str(tool_call.get("id") or "read")
            source_ids = [
                self._tool_attachment_source_id(call_id, item_index)
                for item_index in range(len(attachments))
            ]
            try:
                state = await describe_images(
                    attachments,
                    source_ids=source_ids,
                    describe=describer,
                    prompt=prompt,
                )
            except asyncio.CancelledError:
                host.tracer.log(
                    "read_image_describe_interrupted",
                    tool_call_id=call_id,
                    image_count=len(attachments),
                )
                return True
            hints = render_image_descriptions(state["items"])
            if hints:
                result.output = "\n\n".join(part for part in (result.output, hints) if part)
            result.metadata = {
                **dict(result.metadata or {}),
                "imageDescribe": {
                    "tag": "image_describe",
                    "data": {"status": state["status"], "items": state["items"]},
                },
            }
            host.tracer.log(
                "read_image_describe_completed",
                tool_call_id=call_id,
                image_count=len(attachments),
                failed=sum(1 for item in state["items"] if item.get("status") == "error"),
            )
        return False

    @staticmethod
    def _latest_user(messages: list, owner: dict) -> dict | None:
        return next(
            (
                message for message in reversed(messages)
                if isinstance(message, dict)
                and message is not owner
                and message.get("role") == "user"
                and not is_synthetic_user_message(message)
            ),
            None,
        )

    @staticmethod
    def _settled_or_clear_stale(
        messages: list, source_id: str, key: str, terminal: set[str],
    ) -> bool:
        stale: list[tuple[dict, str]] = []
        for owner in messages:
            if not isinstance(owner, dict) or owner.get("role") != "assistant":
                continue
            for part in owner.get(PARTS_KEY, []):
                metadata = part.get("metadata") if isinstance(part, dict) else None
                detail = metadata.get(key) if isinstance(metadata, dict) else None
                if not isinstance(detail, dict) or detail.get("source_message_id") != source_id:
                    continue
                if detail.get("status") in terminal:
                    return True
                part_id = part.get("id")
                if isinstance(part_id, str):
                    stale.append((owner, part_id))
        for owner, part_id in stale:
            owner[PARTS_KEY] = [
                part for part in owner.get(PARTS_KEY, [])
                if not isinstance(part, dict) or part.get("id") != part_id
            ]
        return False

    @staticmethod
    def _publish_part(host, messages: list, owner: dict, part: dict) -> None:
        host._emit_session_event("message.part.updated", {
            "message_id": owner[MESSAGE_ID_KEY],
            "part": part,
        })
        host._checkpoint_messages(messages, "running")

    @staticmethod
    def _last_user_text(messages: list) -> str:
        for message in reversed(messages):
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and not is_synthetic_user_message(message)
                and isinstance(message.get("content"), str)
            ):
                return message["content"][:300]
        return ""

    @staticmethod
    def _tool_attachment_source_id(tool_call_id: str, index: int) -> str:
        seed = f"nz-coder-tool-attachment:{tool_call_id}:{max(0, int(index))}"
        return f"part-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex}"
