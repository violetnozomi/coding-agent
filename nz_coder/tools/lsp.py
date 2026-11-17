"""Registered LSP tool for semantic navigation and diagnostics."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.lsp import (
    LSPError,
    available_server_summary,
    client_startup_error,
    get_client_for_file,
)
from nz_coder.lsp.client import (
    path_to_uri,
    uri_to_path,
    LSPTimeoutError,
    LSPCancelledError,
)
from nz_coder.lsp.types import LspOperation, DiagnosticReport
from nz_coder.protocol.public_error import (
    format_public_error,
    PublicInputError,
    PublicError,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.tools import register

_OPERATIONS = {
    "goToDefinition",
    "findReferences",
    "hover",
    "documentSymbol",
    "workspaceSymbol",
    "goToImplementation",
    "prepareCallHierarchy",
    "incomingCalls",
    "outgoingCalls",
    "diagnostics",
}


def _safe_path(file_path: str) -> Path:
    return WorkspacePathPolicy(current_workdir()).validate_model_read(file_path)


def _position(line: int, character: int) -> dict:
    if line < 1 or character < 1:
        raise PublicInputError("line and character must be 1-based positive integers")
    return {"line": line - 1, "character": character - 1}


def _normalize_result(value: Any, workspace: Path) -> Any:
    if isinstance(value, list):
        return [_normalize_result(item, workspace) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        key: _normalize_result(item, workspace) for key, item in value.items()
    }
    uri = normalized.get("uri")
    if isinstance(uri, str):
        path = uri_to_path(uri)
        if path is not None:
            try:
                relative = path.resolve().relative_to(workspace)
                normalized["path"] = (
                    relative.as_posix()
                    if WorkspacePathPolicy(workspace).is_model_visible(path)
                    else "private-workspace"
                )
            except ValueError:
                normalized["path"] = "outside-workspace"
            normalized.pop("uri", None)
    target_uri = normalized.get("targetUri")
    if isinstance(target_uri, str):
        path = uri_to_path(target_uri)
        if path is not None:
            try:
                relative = path.resolve().relative_to(workspace)
                normalized["targetPath"] = (
                    relative.as_posix()
                    if WorkspacePathPolicy(workspace).is_model_visible(path)
                    else "private-workspace"
                )
            except ValueError:
                normalized["targetPath"] = "outside-workspace"
            normalized.pop("targetUri", None)
    return normalized


def _request_for_operation(
    client,
    operation: str,
    target: Path,
    line: int,
    character: int,
    query: str,
    call_operation: LspOperation,
) -> Any:
    def request(method, params):
        return client.request(method, params, operation=call_operation)

    uri = path_to_uri(target)
    position = _position(line, character)
    document = {"textDocument": {"uri": uri}}
    if operation == "goToDefinition":
        return request(
            "textDocument/definition",
            {**document, "position": position},
        )
    if operation == "findReferences":
        return request(
            "textDocument/references",
            {
                **document,
                "position": position,
                "context": {"includeDeclaration": True},
            },
        )
    if operation == "hover":
        return request(
            "textDocument/hover",
            {**document, "position": position},
        )
    if operation == "documentSymbol":
        return request("textDocument/documentSymbol", document)
    if operation == "workspaceSymbol":
        return request("workspace/symbol", {"query": query or ""})
    if operation == "goToImplementation":
        return request(
            "textDocument/implementation",
            {**document, "position": position},
        )
    if operation == "prepareCallHierarchy":
        return request(
            "textDocument/prepareCallHierarchy",
            {**document, "position": position},
        )
    if operation in {"incomingCalls", "outgoingCalls"}:
        items = (
            request(
                "textDocument/prepareCallHierarchy",
                {**document, "position": position},
            )
            or []
        )
        if not items:
            return []
        method = (
            "callHierarchy/incomingCalls"
            if operation == "incomingCalls"
            else "callHierarchy/outgoingCalls"
        )
        return request(method, {"item": items[0]})
    raise PublicInputError(f"Unsupported LSP operation: {operation}")


def _bounded_output(payload: dict, maximum: int, *, limit: int | None = None) -> str:
    """只裁完整条目，序列化后的 JSON 不再切字符。"""
    diagnostic = "status" in payload and "freshness" in payload
    items = payload["items"]
    if limit is not None and len(items) > limit:
        payload["truncated"] = True
        if diagnostic and payload["status"] == "ready":
            payload["status"] = "partial"
        items = items[:limit]
    payload["items"] = []
    minimal = {
        "items": [],
        "truncated": True,
        "total_if_known": payload.get("total_if_known"),
        "reason_code": "output_limit",
    }
    if diagnostic:
        minimal = {
            **payload,
            "items": [],
            "truncated": True,
            "reason_code": payload.get("reason_code") or "output_limit",
        }
        if minimal["status"] == "ready":
            minimal["status"] = "partial"
    floor = json.dumps(minimal, ensure_ascii=False)
    if maximum < len(floor):
        return floor
    if len(json.dumps(payload, ensure_ascii=False)) > maximum:
        return floor
    for item in items:
        candidate = {**payload, "items": [*payload["items"], item]}
        if len(json.dumps(candidate, ensure_ascii=False)) > maximum:
            payload["truncated"] = True
            if diagnostic and payload["status"] == "ready":
                payload["status"] = "partial"
            payload["reason_code"] = payload.get("reason_code") or "output_limit"
            # 加上标记后仍须完整地满足有效预算。
            while (
                payload["items"]
                and len(json.dumps(payload, ensure_ascii=False)) > maximum
            ):
                payload["items"].pop()
            if len(json.dumps(payload, ensure_ascii=False)) > maximum:
                return floor
            location = item.get("location", item) if isinstance(item, dict) else {}
            hint = {
                "summary": "Item omitted; narrow the query or inspect this source.",
                "path": location.get(
                    "path", location.get("targetPath", payload.get("source_path", ""))
                ),
            }
            candidate = {**payload, "items": [*payload["items"], hint]}
            if len(json.dumps(candidate, ensure_ascii=False)) <= maximum:
                payload = candidate
            break
        payload = candidate
    return json.dumps(payload, ensure_ascii=False)


def _position_from_ref(ref, text, encoding):
    selection = ref.get("selection")
    if not isinstance(selection, dict) or not isinstance(selection.get("start"), dict):
        raise PublicInputError("symbol_ref has no proven name selection")
    start = selection["start"]
    row = start["line"]
    column = start["character"]
    lines = re.split(r"\r\n|\r|\n", text)
    if row < 0 or row >= len(lines):
        raise PublicInputError("symbol_ref selection is stale")
    source_encoding = selection.get("encoding", ref.get("position_encoding", "utf-8"))
    line = lines[row]
    if source_encoding == "utf-8":
        prefix = line.encode("utf-8")[:column].decode("utf-8")
    elif source_encoding == "utf-16":
        prefix = line.encode("utf-16-le")[: column * 2].decode("utf-16-le")
    else:
        prefix = line[:column]
    offset = (
        len(prefix.encode("utf-16-le")) // 2
        if encoding == "utf-16"
        else (len(prefix.encode("utf-8")) if encoding == "utf-8" else len(prefix))
    )
    return row + 1, offset + 1


def lsp(
    operation: str,
    file_path: str = "",
    line: int | None = None,
    character: int | None = None,
    query: str = "",
    symbol_ref: dict | None = None,
    limit: int | None = None,
    result_path: str = "",
) -> str:
    """在当前准备环境中运行一次有界、可取消的语义查询。"""
    try:
        if operation not in _OPERATIONS:
            raise PublicInputError("Unknown LSP operation")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise PublicInputError("limit must be a positive integer")
        if result_path and operation in {"hover", "diagnostics"}:
            raise PublicInputError("result_path does not apply to this operation")
        if symbol_ref is not None:
            if (
                file_path
                or line is not None
                or character is not None
                or operation
                not in {
                    "goToDefinition",
                    "findReferences",
                    "hover",
                    "goToImplementation",
                    "prepareCallHierarchy",
                    "incomingCalls",
                    "outgoingCalls",
                }
            ):
                raise PublicInputError(
                    "symbol_ref and explicit position are mutually exclusive"
                )
            if (
                not isinstance(symbol_ref, dict)
                or not symbol_ref.get("content_hash")
                or not symbol_ref.get("symbol_id")
            ):
                raise PublicInputError("symbol_ref requires symbol_id and content_hash")
            file_path = symbol_ref["file_path"]
        line = 1 if line is None else line
        character = 1 if character is None else character
        target = _safe_path(file_path)
        workspace = current_workdir().resolve()
        relative = target.relative_to(workspace).as_posix()
        if result_path:
            prefix = Path(result_path)
            if prefix.is_absolute() or ".." in prefix.parts:
                raise PublicInputError(
                    "result_path must be a workspace-relative prefix"
                )
            result_path = prefix.as_posix().rstrip("/")
        from nz_coder.foundation.workspace_trust import (
            active_config_snapshot,
            current_config_snapshot,
        )
        from nz_coder.foundation.cancellation import CancellationView
        from nz_coder.tools import current_tool_cancel_event
        from nz_coder.runtime.core.run_context import current_run_context

        settings = current_run_settings()
        snapshot = active_config_snapshot(workspace)
        enabled = (
            snapshot.get_bool("NZ_LSP_ENABLED", True)
            if snapshot
            else settings.lsp_enabled
        )
        if not enabled:
            return "Error: LSP support is disabled by NZ_LSP_ENABLED."
        snapshot = snapshot or current_config_snapshot(workspace)
        run = current_run_context()
        call = LspOperation(
            CancellationView(
                current_tool_cancel_event(), run.cancel_scope if run else None
            ),
            time.monotonic()
            + settings.lsp_initialize_timeout
            + settings.lsp_request_timeout,
        )
        client = get_client_for_file(
            target, workspace, config_snapshot=snapshot, operation=call
        )
        if client is None:
            if operation == "diagnostics":
                payload = DiagnosticReport(
                    relative,
                    "unavailable",
                    "not_observed",
                    reason_code="lsp_unavailable",
                ).to_dict()
                payload["total_if_known"] = None
                return _bounded_output(
                    payload, settings.lsp_max_output_chars, limit=limit
                )
            error = client_startup_error(target, workspace, config_snapshot=snapshot)
            return (
                "Error: LSP server failed to initialize: " + error
                if error
                else "Error: " + available_server_summary(target)
            )
        access = WorkspaceFileAccess(workspace)
        captured = []

        def read_source():
            text, identity = access.read_text_with_identity(relative, errors="replace")
            if symbol_ref is not None:
                if identity.content_hash != symbol_ref["content_hash"]:
                    raise PublicInputError(
                        "symbol_ref is stale; obtain a new reference"
                    )
                from nz_coder.intelligence.service import workspace_repo_intelligence

                service = workspace_repo_intelligence(workspace)
                actual = service.index.symbol_ref(symbol_ref["symbol_id"])
                if (
                    actual is None
                    or actual["file_path"] != relative
                    or actual["symbol_id"] != symbol_ref["symbol_id"]
                    or actual["content_hash"] != identity.content_hash
                    or actual["selection"] != symbol_ref.get("selection")
                ):
                    raise PublicInputError(
                        "symbol_ref selection is stale; obtain a new reference"
                    )
            captured[:] = [text, identity]
            return text, identity

        if operation == "diagnostics":
            payload = client.diagnostics(
                target, reader=read_source, operation=call
            ).to_dict()
            payload["path"] = relative
            payload["total_if_known"] = (
                None if payload["truncated"] else len(payload["items"])
            )
        else:
            version = client.open_document(target, reader=read_source, operation=call)
            text, identity = captured
            if symbol_ref is not None:
                line, character = _position_from_ref(
                    symbol_ref, text, client.position_encoding
                )
            result = _request_for_operation(
                client, operation, target, line, character, query, call
            )
            if symbol_ref is not None:
                _fresh, stamp = access.read_text_with_identity(
                    relative, errors="replace"
                )
                if (
                    stamp.content_hash != identity.content_hash
                    or not client.document_matches(target, version, identity)
                ):
                    raise PublicInputError(
                        "symbol_ref query superseded by a source change"
                    )
            normalized = _normalize_result(result, workspace)
            items = (
                normalized
                if isinstance(normalized, list)
                else ([] if normalized is None else [normalized])
            )
            if result_path:

                def matches(item):
                    if not isinstance(item, dict):
                        return False
                    location = item.get(
                        "location", item.get("from", item.get("to", item))
                    )
                    path = (
                        location.get("path", location.get("targetPath", ""))
                        if isinstance(location, dict)
                        else ""
                    )
                    if operation == "documentSymbol" and not path:
                        path = relative
                    return path == result_path or path.startswith(result_path + "/")

                items = [item for item in items if matches(item)]
            payload = {
                "items": items,
                "truncated": False,
                "total_if_known": len(items),
                "source_path": relative,
            }
        return _bounded_output(payload, settings.lsp_max_output_chars, limit=limit)
    except LSPCancelledError:
        if operation == "diagnostics":
            payload = DiagnosticReport(
                relative, "cancelled", "not_observed", reason_code="cancelled"
            ).to_dict()
            payload["total_if_known"] = None
            return _bounded_output(payload, settings.lsp_max_output_chars, limit=limit)
        return format_public_error(
            PublicError("lsp_cancelled", "LSP operation cancelled.")
        )
    except LSPTimeoutError:
        if operation == "diagnostics":
            payload = DiagnosticReport(
                relative, "timeout", "not_observed", reason_code="deadline_exceeded"
            ).to_dict()
            payload["total_if_known"] = None
            return _bounded_output(payload, settings.lsp_max_output_chars, limit=limit)
        return format_public_error(
            PublicError(
                "lsp_timeout",
                "LSP operation timed out; inspect /lsp status.",
                retryable=True,
            )
        )
    except (LSPError, OSError, ValueError) as exc:
        return format_public_error(exc)
    except Exception as exc:
        return format_public_error(exc, context="Unexpected LSP failure: ")


register(
    name="lsp",
    description=(
        "Use an installed language server for semantic code navigation and "
        "diagnostics. Supports definitions, references, hover/type info, "
        "document/workspace symbols, implementations, call hierarchy, and "
        "diagnostics. Paths must stay inside the workspace."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": sorted(_OPERATIONS),
                "description": "Semantic operation to perform.",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute or workspace-relative source file.",
            },
            "symbol_ref": {
                "type": "object",
                "description": "Exact reference returned by repo_context symbol_ref; mutually exclusive with file_path.",
            },
            "limit": {"type": "integer", "minimum": 1},
            "result_path": {
                "type": "string",
                "description": "Explicit workspace-relative result path prefix; not the query anchor.",
            },
            "line": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based line number.",
            },
            "character": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based character offset.",
            },
            "query": {
                "type": "string",
                "description": "Query for workspaceSymbol; ignored otherwise.",
            },
        },
        "required": ["operation"],
    },
    handler=lsp,
    execution="read",
)
