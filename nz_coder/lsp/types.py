"""LSP 一次调用的期限与不可变诊断快照。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from nz_coder.foundation.workspace_file_access import WorkspaceFileIdentity


@dataclass(frozen=True)
class LspOperation:
    cancellation: object | None
    deadline: float

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def cancelled(self) -> bool:
        return self.cancellation is not None and self.cancellation.is_set()


@dataclass(frozen=True)
class DiagnosticReport:
    path: str
    status: str
    freshness: str
    items_json: str = "[]"
    requested_version: int | None = None
    source_identity: WorkspaceFileIdentity | None = None
    method: str = ""
    analysis_environment: str = "unknown"
    reason_code: str = ""
    truncated: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        result["items"] = json.loads(result.pop("items_json"))
        return result
