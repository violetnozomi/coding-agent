"""Budgeted LSP definition resolver for unresolved repository call sites."""
from __future__ import annotations

from pathlib import Path

from nz_coder.intelligence.code_index import CallResolutionRequest, ResolvedCallLocation
from nz_coder.lsp.client import _validated_timeout, path_to_uri, uri_to_path
from nz_coder.lsp.manager import get_client_for_file


class LspCallTargetResolver:
    def __init__(self, workspace: Path, *, request_timeout: float = 1.0) -> None:
        self.workspace = Path(workspace).resolve()
        self.request_timeout = _validated_timeout(request_timeout)

    @staticmethod
    def _first_location(value) -> dict | None:
        if isinstance(value, list):
            value = value[0] if value else None
        return value if isinstance(value, dict) else None

    def resolve(self, request: CallResolutionRequest) -> ResolvedCallLocation | None:
        source = (self.workspace / request.file_path).resolve()
        source.relative_to(self.workspace)
        if not source.is_file():
            return None
        client = get_client_for_file(source, self.workspace)
        if client is None:
            return None
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        raw_line = lines[request.line - 1] if 0 < request.line <= len(lines) else ""
        column = raw_line.find(request.raw_name)
        client.open_document(source)
        value = client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": path_to_uri(source)},
                "position": {"line": max(0, request.line - 1), "character": max(0, column)},
            },
            timeout=self.request_timeout,
        )
        location = self._first_location(value)
        if location is None:
            return None
        uri = location.get("targetUri") or location.get("uri")
        target = uri_to_path(uri) if isinstance(uri, str) else None
        if target is None:
            return None
        try:
            relative = target.resolve().relative_to(self.workspace).as_posix()
        except ValueError:
            return None
        range_value = location.get("targetSelectionRange") or location.get("range") or {}
        start = range_value.get("start") if isinstance(range_value, dict) else {}
        line = int(start.get("line") or 0) + 1 if isinstance(start, dict) else 1
        return ResolvedCallLocation(
            relative, line, name=request.raw_name, confidence=0.95,
            source="lsp-definition",
        )


__all__ = ["LspCallTargetResolver"]
