"""Provider-neutral projection of durable Session messages onto wire messages."""
from __future__ import annotations

import math
import time

from nz_coder.foundation import config
from nz_coder.protocol.attachments import normalize_attachments
from nz_coder.state.context import estimate_tokens
from nz_coder.protocol.message_schema import (
    MESSAGE_ID_KEY,
    PARTS_KEY,
    PROVIDER_EXTRA_KEY,
    PROVIDER_REASONING_KEY,
    cleanup_incomplete_tool_history,
)
from nz_coder.runtime.conversation.continuation_context import project_continuation_messages
from nz_coder.state.input_expansion import render_expanded_message


def project_provider_messages(
    messages: list,
    *,
    capabilities=None,
    include_attachments: bool = True,
    projection_stats: dict | None = None,
) -> list:
    """Normalize Session history without exposing durable NZ-only fields."""
    messages = project_continuation_messages(messages)
    empty_tool_assistant_ordinals = _empty_tool_assistant_ordinals(messages)
    calls_before, results_before = _tool_envelope_counts(messages)
    messages = cleanup_incomplete_tool_history(messages)
    calls_after, results_after = _tool_envelope_counts(messages)
    observed_by_provider = _provider_observation_suffix(messages)
    stats = {
        "orphan_tool_calls_removed": max(0, calls_before - calls_after),
        "orphan_tool_results_removed": max(0, results_before - results_after),
        "empty_assistant_placeholders": 0,
        "replaced_tool_results": 0,
        "acknowledged_write_results_compacted": 0,
        "acknowledged_write_tokens_saved": 0,
        "superseded_file_reads": 0,
        "superseded_file_read_tokens_saved": 0,
        "superseded_verification_failures": 0,
        "superseded_verification_failure_tokens_saved": 0,
        "tool_result_tokens_before": 0,
        "tool_result_tokens_after": 0,
        "tool_result_tokens_saved": 0,
    }
    now = time.time()
    preserve_reasoning = (
        capabilities.preserve_reasoning_content
        if capabilities is not None
        else getattr(config, "PASS_REASONING_CONTENT", True)
    )
    strip_extra = {"provider_extra", "reasoning_content"}
    supports_images = bool(
        getattr(capabilities, "supports_image_input", False)
    )
    attachment_by_call: dict[str, list[dict]] = {}
    user_attachment_by_message: dict[str, list[dict]] = {}
    image_description_by_message: dict[str, list[str]] = {}
    document_read_by_message: dict[str, list[str]] = {}
    latest_write_generation: dict[str, int] = {}
    latest_pass_generation: dict[str, int] = {}
    latest_acceptance_generation = -1

    for owner in messages:
        if not isinstance(owner, dict):
            continue
        if owner.get("role") == "tool":
            generation = _safe_generation(owner.get("_nz_mutation_generation"))
            if owner.get("_nz_evidence_kind") == "file_write":
                for resource in owner.get("_nz_mutated_resources") or []:
                    path = str(resource or "")
                    if path:
                        latest_write_generation[path] = max(
                            latest_write_generation.get(path, -1), generation,
                        )
            if (
                owner.get("_nz_evidence_kind") == "verification"
                and owner.get("_nz_verification_passed") is True
            ):
                resource = str(owner.get("_nz_resource") or "")
                if resource:
                    latest_pass_generation[resource] = max(
                        latest_pass_generation.get(resource, -1), generation,
                    )
                if resource == "acceptance":
                    latest_acceptance_generation = max(
                        latest_acceptance_generation, generation,
                    )
        for part in owner.get(PARTS_KEY, []):
            if not isinstance(part, dict):
                continue
            metadata = part.get("metadata")
            image_description = (
                metadata.get("image_describe")
                if isinstance(metadata, dict)
                else None
            )
            if (
                not supports_images
                and part.get("type") == "text"
                and isinstance(image_description, dict)
                and image_description.get("status") == "completed"
                and isinstance(image_description.get("source_message_id"), str)
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                image_description_by_message.setdefault(
                    image_description["source_message_id"], []
                ).append(part["text"])
            document_read = (
                metadata.get("document_read")
                if isinstance(metadata, dict)
                else None
            )
            if (
                part.get("type") == "text"
                and isinstance(document_read, dict)
                and document_read.get("status") in {"completed", "error"}
                and isinstance(document_read.get("source_message_id"), str)
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                document_read_by_message.setdefault(
                    document_read["source_message_id"], []
                ).append(part["text"])
            if part.get("type") == "file":
                files = _safe_attachments([part])
                message_id = owner.get(MESSAGE_ID_KEY)
                if isinstance(message_id, str) and files:
                    user_attachment_by_message.setdefault(message_id, []).extend(files)
                continue
            if part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") != "completed":
                continue
            files = _safe_attachments(state.get("attachments"))
            call_id = part.get("call_id")
            if isinstance(call_id, str) and call_id and files:
                attachment_by_call[call_id] = files

    base: list[dict] = []
    assistant_ordinal = 0
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        strip_keys = {
            key for key in message if key.startswith("_nz_")
        } | {"_timestamp"} | strip_extra
        clean = {key: value for key, value in message.items() if key not in strip_keys}
        private_provider_extra = message.get(PROVIDER_EXTRA_KEY)
        if not isinstance(private_provider_extra, dict):
            private_provider_extra = message.get("provider_extra")
        if isinstance(private_provider_extra, dict) and private_provider_extra:
            clean["provider_extra"] = dict(private_provider_extra)
        if role == "tool":
            replacement = _superseded_tool_marker(
                message,
                latest_write_generation=latest_write_generation,
                latest_pass_generation=latest_pass_generation,
                latest_acceptance_generation=latest_acceptance_generation,
            )
            reason = ""
            if replacement.startswith("[Earlier read of "):
                reason = "superseded_file_reads"
            elif replacement.startswith("[Earlier "):
                reason = "superseded_verification_failures"
            if not replacement and observed_by_provider[message_index]:
                replacement = _acknowledged_write_marker(message)
                if replacement:
                    reason = "acknowledged_write_results_compacted"
            if replacement:
                clean["content"] = replacement
                _record_projection_replacement(
                    stats,
                    reason=reason,
                    before=message.get("content", ""),
                    after=replacement,
                )
        message_id = str(message.get(MESSAGE_ID_KEY) or "")
        if role == "user" and isinstance(clean.get("content"), str):
            descriptions = image_description_by_message.get(message_id, [])
            if descriptions:
                clean["content"] = "\n\n".join(
                    [clean["content"], *descriptions]
                ).strip()
            documents = document_read_by_message.get(message_id, [])
            if documents:
                clean["content"] = _content_without_document_expansions(
                    message,
                    clean["content"],
                )
                clean["content"] = "\n\n".join(
                    [clean["content"], *documents]
                ).strip()
        if (
            include_attachments
            and role == "tool"
            and not message.get("_nz_tool_compacted_at")
            and supports_images
        ):
            attachments = _safe_attachments(
                message.get("_nz_attachments")
                or attachment_by_call.get(str(message.get("tool_call_id") or ""))
            )
            if attachments:
                clean["_nz_attachments"] = attachments
        if include_attachments and role == "user" and supports_images:
            attachments = _safe_attachments(
                message.get("_nz_user_attachments")
                or user_attachment_by_message.get(message_id)
            )
            if attachments:
                clean["_nz_user_attachments"] = attachments
        if role == "assistant":
            if clean.get("content") is None:
                clean["content"] = ""
            if (
                assistant_ordinal in empty_tool_assistant_ordinals
                and not str(clean.get("content") or "").strip()
                and not clean.get("tool_calls")
            ):
                # InfCodeX/Kimi compatibility: retain an interrupted
                # assistant slot without persisting fabricated visible text.
                # Ordinary empty shells for the currently-running turn are
                # still removed below because they never had tool calls.
                clean["content"] = "..."
                stats["empty_assistant_placeholders"] += 1
            private_reasoning = message.get(PROVIDER_REASONING_KEY)
            legacy_reasoning = message.get("reasoning_content")
            if preserve_reasoning:
                clean["reasoning_content"] = str(
                    private_reasoning
                    if isinstance(private_reasoning, str)
                    else legacy_reasoning
                    if isinstance(legacy_reasoning, str)
                    else ""
                )
            clean["_timestamp"] = message.get("_timestamp", now)
            assistant_ordinal += 1
        base.append(clean)

    valid_tool_ids = {
        str(call.get("id") or call.get("tool_call_id"))
        for message in base
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if call.get("id") or call.get("tool_call_id")
    }
    result: list[dict] = []
    for message in base:
        role = message.get("role", "")
        if role == "assistant":
            content = message.get("content", "")
            if isinstance(content, str) and not content.strip() and not message.get("tool_calls"):
                continue
            result.append({key: value for key, value in message.items() if key != "_timestamp"})
            continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id and tool_call_id not in valid_tool_ids:
                continue
            result.append(message)
            continue
        if role == "user" and result and result[-1].get("role") == "user":
            if not _merge_adjacent_users(result, message):
                result.append(message)
            continue
        result.append(message)
    if projection_stats is not None:
        projection_stats.update(stats)
    return result


def _tool_envelope_counts(messages: list) -> tuple[int, int]:
    """Count protocol envelopes for cleanup observability."""
    calls = 0
    results = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                calls += len(tool_calls)
        elif message.get("role") == "tool":
            results += 1
    return calls, results


def _empty_tool_assistant_ordinals(messages: list) -> set[int]:
    """Locate assistant turns that only become empty after orphan cleanup."""
    ordinals: set[int] = set()
    assistant_ordinal = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls")
        if (
            isinstance(calls, list)
            and bool(calls)
            and not str(message.get("content") or "").strip()
        ):
            ordinals.add(assistant_ordinal)
        assistant_ordinal += 1
    return ordinals


def _provider_observation_suffix(messages: list) -> list[bool]:
    """Mark results followed by a real provider-authored assistant response.

    Runtime-synthesized assistant tool calls do not prove that the model has
    seen the preceding result, so only messages carrying provider usage count
    as an observation boundary.
    """
    observed = [False] * len(messages)
    provider_response_seen = False
    for index in range(len(messages) - 1, -1, -1):
        observed[index] = provider_response_seen
        message = messages[index]
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and isinstance(message.get("_nz_usage"), dict)
        ):
            provider_response_seen = True
    return observed


def _acknowledged_write_marker(message: dict) -> str:
    """Return a provider-only receipt for an already-observed successful write."""
    if message.get("_nz_evidence_kind") != "file_write":
        return ""
    resources = [
        str(resource).strip()
        for resource in message.get("_nz_mutated_resources") or []
        if str(resource).strip()
    ]
    target = ", ".join(resources) if resources else "workspace files"
    generation = _safe_generation(message.get("_nz_mutation_generation"))
    return (
        "[Successful write result omitted after the model observed it: "
        f"{target} (mutation generation {generation}). The full result remains "
        "in the durable session; the current workspace is the source of truth.]"
    )


def _record_projection_replacement(
    stats: dict,
    *,
    reason: str,
    before: object,
    after: str,
) -> None:
    """Accumulate auditable savings without altering the public return type."""
    before_tokens = estimate_tokens(before)
    after_tokens = estimate_tokens(after)
    stats["replaced_tool_results"] += 1
    if reason:
        stats[reason] += 1
    stats["tool_result_tokens_before"] += before_tokens
    stats["tool_result_tokens_after"] += after_tokens
    saved_tokens = max(0, before_tokens - after_tokens)
    stats["tool_result_tokens_saved"] += saved_tokens
    reason_token_keys = {
        "acknowledged_write_results_compacted": (
            "acknowledged_write_tokens_saved"
        ),
        "superseded_file_reads": "superseded_file_read_tokens_saved",
        "superseded_verification_failures": (
            "superseded_verification_failure_tokens_saved"
        ),
    }
    if reason in reason_token_keys:
        stats[reason_token_keys[reason]] += saved_tokens


def _superseded_tool_marker(
    message: dict,
    *,
    latest_write_generation: dict[str, int],
    latest_pass_generation: dict[str, int],
    latest_acceptance_generation: int,
) -> str:
    kind = str(message.get("_nz_evidence_kind") or "")
    resource = str(message.get("_nz_resource") or "")
    generation = _safe_generation(message.get("_nz_mutation_generation"))
    if kind == "file_read" and resource:
        changed_generation = latest_write_generation.get(resource, -1)
        if changed_generation > generation:
            return (
                f"[Earlier read of {resource} omitted: file changed in mutation "
                f"generation {changed_generation}.]"
            )
    if (
        kind == "verification"
        and resource
        and message.get("_nz_verification_passed") is False
    ):
        passed_generation = max(
            latest_pass_generation.get(resource, -1),
            latest_acceptance_generation,
        )
        if passed_generation > generation:
            return (
                f"[Earlier {resource} verification failure from generation "
                f"{generation} omitted; superseded by a passing generation "
                f"{passed_generation} result.]"
            )
    return ""


def _safe_attachments(value) -> list[dict]:
    try:
        return normalize_attachments(value)
    except ValueError:
        return []


def _safe_generation(value) -> int:
    """Read an untrusted persisted mutation generation without raising."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            return 0
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _content_without_document_expansions(message: dict, content: str) -> str:
    expansions = message.get("_nz_input_expansions")
    user_text = message.get("_nz_user_text")
    if not isinstance(expansions, list) or not isinstance(user_text, str):
        return content
    projected = {
        "content": content,
        "_nz_user_text": user_text,
        "_nz_input_expansions": [
            item for item in expansions
            if not isinstance(item, dict) or item.get("kind") != "document"
        ],
    }
    render_expanded_message(projected)
    return str(projected["content"])


def _merge_adjacent_users(result: list[dict], current: dict) -> bool:
    previous = result[-1]
    previous_content = previous.get("content", "")
    current_content = current.get("content", "")
    if not isinstance(previous_content, str) or not isinstance(current_content, str):
        return False
    try:
        attachments = normalize_attachments([
            *previous.get("_nz_user_attachments", []),
            *current.get("_nz_user_attachments", []),
        ])
    except ValueError:
        return False
    merged = dict(previous, content=previous_content + "\n\n" + current_content)
    if attachments:
        merged["_nz_user_attachments"] = attachments
    result[-1] = merged
    return True
