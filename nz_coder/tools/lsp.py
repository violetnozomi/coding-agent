"""Registered LSP tool for semantic navigation and diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.lsp import (
    LSPError,
    available_server_summary,
    client_startup_error,
    get_client_for_file,
)
from nz_coder.lsp.client import path_to_uri, uri_to_path
from nz_coder.protocol.public_error import format_public_error
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
        raise ValueError("line and character must be 1-based positive integers")
    return {"line": line - 1, "character": character - 1}


def _normalize_result(value: Any, workspace: Path) -> Any:
    if isinstance(value, list):
        return [_normalize_result(item, workspace) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {
        key: _normalize_result(item, workspace)
        for key, item in value.items()
    }
    uri = normalized.get("uri")
    if isinstance(uri, str):
        path = uri_to_path(uri)
        if path is not None:
            try:
                normalized["path"] = str(path.resolve().relative_to(workspace))
            except ValueError:
                normalized["path"] = str(path.resolve())
            normalized.pop("uri", None)
    target_uri = normalized.get("targetUri")
    if isinstance(target_uri, str):
        path = uri_to_path(target_uri)
        if path is not None:
            try:
                normalized["targetPath"] = str(path.resolve().relative_to(workspace))
            except ValueError:
                normalized["targetPath"] = str(path.resolve())
            normalized.pop("targetUri", None)
    return normalized


def _request_for_operation(
    client,
    operation: str,
    target: Path,
    line: int,
    character: int,
    query: str,
) -> Any:
    uri = path_to_uri(target)
    position = _position(line, character)
    document = {"textDocument": {"uri": uri}}
    if operation == "goToDefinition":
        return client.request(
            "textDocument/definition",
            {**document, "position": position},
        )
    if operation == "findReferences":
        return client.request(
            "textDocument/references",
            {
                **document,
                "position": position,
                "context": {"includeDeclaration": True},
            },
        )
    if operation == "hover":
        return client.request(
            "textDocument/hover",
            {**document, "position": position},
        )
    if operation == "documentSymbol":
        return client.request("textDocument/documentSymbol", document)
    if operation == "workspaceSymbol":
        return client.request("workspace/symbol", {"query": query or ""})
    if operation == "goToImplementation":
        return client.request(
            "textDocument/implementation",
            {**document, "position": position},
        )
    if operation == "prepareCallHierarchy":
        return client.request(
            "textDocument/prepareCallHierarchy",
            {**document, "position": position},
        )
    if operation in {"incomingCalls", "outgoingCalls"}:
        items = client.request(
            "textDocument/prepareCallHierarchy",
            {**document, "position": position},
        ) or []
        if not items:
            return []
        method = (
            "callHierarchy/incomingCalls"
            if operation == "incomingCalls"
            else "callHierarchy/outgoingCalls"
        )
        return client.request(method, {"item": items[0]})
    if operation == "diagnostics":
        return client.diagnostics(target)
    raise ValueError(f"Unsupported LSP operation: {operation}")


def lsp(
    operation: str,
    file_path: str,
    line: int = 1,
    character: int = 1,
    query: str = "",
) -> str:
    """Run one semantic code operation through an installed language server."""
    try:
        if operation not in _OPERATIONS:
            choices = ", ".join(sorted(_OPERATIONS))
            return f"Error: Unknown LSP operation '{operation}'. Expected: {choices}"
        target = _safe_path(file_path)
        if not target.is_file():
            return f"Error: File not found: {file_path}"
        workspace = current_workdir().resolve()
        from nz_coder.foundation.workspace_trust import (
            active_config_snapshot,
            current_config_snapshot,
        )

        config_snapshot = active_config_snapshot(workspace)
        settings = current_run_settings()
        enabled = (
            config_snapshot.get_bool("NZ_LSP_ENABLED", True)
            if config_snapshot is not None
            else settings.lsp_enabled
        )
        if not enabled:
            return "Error: LSP support is disabled by NZ_LSP_ENABLED."
        config_snapshot = config_snapshot or current_config_snapshot(workspace)
        client = get_client_for_file(
            target, workspace, config_snapshot=config_snapshot,
        )
        if client is None:
            startup_error = client_startup_error(
                target, workspace, config_snapshot=config_snapshot,
            )
            if startup_error:
                return f"Error: LSP server failed to initialize: {startup_error}"
            return "Error: " + available_server_summary(target)
        if operation != "diagnostics":
            client.open_document(target)
        result = _request_for_operation(
            client,
            operation,
            target,
            line,
            character,
            query,
        )
        normalized = _normalize_result(result, workspace)
        if normalized in (None, [], {}):
            return f"No results found for {operation}"
        output = json.dumps(normalized, indent=2, ensure_ascii=False)
        if len(output) > settings.lsp_max_output_chars:
            output = (
                output[:settings.lsp_max_output_chars]
                + "\n... [LSP result truncated]"
            )
        return output
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
        "required": ["operation", "file_path"],
    },
    handler=lsp,
    execution="read",
)
