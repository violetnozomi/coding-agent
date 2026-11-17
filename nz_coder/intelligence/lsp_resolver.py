"""Budgeted LSP definition resolver for unresolved repository call sites."""
from __future__ import annotations

import time
import re
import unicodedata
from pathlib import Path

from nz_coder.intelligence.code_index import CallResolutionRequest, ResolvedCallLocation
from nz_coder.lsp.client import _validated_timeout, path_to_uri, uri_to_path
from nz_coder.lsp.manager import get_client_for_file
from nz_coder.lsp.types import LspOperation
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess


class _StaleCallSource(ValueError):
    """输入代次变化只使本次定位失效，不污染共享文档。"""


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
        if request.selection is None or not request.content_hash:
            return None
        operation = LspOperation(None, time.monotonic() + self.request_timeout)
        source = (self.workspace / request.file_path).resolve()
        source.relative_to(self.workspace)
        if not source.is_file():
            return None
        access = WorkspaceFileAccess(self.workspace)
        content, identity = access.read_text_with_identity(request.file_path, errors="replace")
        if identity.content_hash != request.content_hash:
            return None
        selection = request.selection
        codecs = {"utf-8": ("utf-8",1),"utf-16":("utf-16-le",2),"utf-32":("utf-32-le",4)}
        if selection.get('encoding') not in codecs:
            return None
        client = get_client_for_file(source,self.workspace,operation=operation)
        if client is None:
            return None
        captured = []
        def read_source():
            content,identity = access.read_text_with_identity(request.file_path,errors='replace')
            if identity.content_hash != request.content_hash:
                raise _StaleCallSource('Call source changed')
            lines = re.split(r'\r\n|\r|\n',content)
            start,end = selection['start'],selection['end']
            row = start['line']
            if row != end['line'] or not 0 <= row < len(lines):
                raise _StaleCallSource('Call selection no longer applies')
            codec,width = codecs[selection['encoding']]
            line = lines[row].encode(codec)
            first,last = start['character']*width,end['character']*width
            if not 0 <= first < last <= len(line):
                raise _StaleCallSource('Call selection is outside source')
            prefix = line[:first].decode(codec)
            name = line[first:last].decode(codec)
            if name != request.raw_name and unicodedata.normalize('NFKC',name) != request.raw_name:
                raise _StaleCallSource('Call name no longer matches')
            captured[:] = [identity,row,prefix]
            return content,identity
        try:
            version = client.open_document(source,reader=read_source,operation=operation)
        except _StaleCallSource:
            return None
        identity,row,prefix = captured
        target_codec,target_width = codecs[client.position_encoding]
        column = len(prefix.encode(target_codec))//target_width
        value = client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": path_to_uri(source)},
                "position": {"line": row, "character": column},
            },
            operation=operation,
        )
        _, current = access.read_text_with_identity(request.file_path, errors="replace")
        if current.content_hash != identity.content_hash or not client.document_matches(source,version,identity):
            return None
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
