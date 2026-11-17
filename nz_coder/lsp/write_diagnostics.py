"""Best-effort LSP diagnostics for files changed by a committed write batch."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast
import time

from .types import DiagnosticReport, LspOperation

from nz_coder.runtime.core.run_settings import current_run_settings

from .manager import get_client_for_file

_SEVERITY_NAMES = {
    1: "error",
    2: "warning",
    3: "information",
    4: "hint",
}


def _safe_targets(paths: Iterable[str], workspace: Path) -> list[tuple[str, Path]]:
    root = workspace.resolve()
    targets: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for raw_path in paths:
        value = str(raw_path or "").strip()
        if not value:
            continue
        candidate = Path(value)
        target = (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        if target in seen:
            continue
        seen.add(target)
        targets.append((relative.as_posix(), target))
    return targets


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, object], value)


def _format_diagnostic(path: str, item: Mapping[str, object]) -> str:
    range_data = _mapping(item.get("range"))
    start = _mapping(range_data.get("start"))
    try:
        line = max(0, int(str(start.get("line", 0)))) + 1
    except (TypeError, ValueError):
        line = 1
    try:
        character = max(0, int(str(start.get("character", 0)))) + 1
    except (TypeError, ValueError):
        character = 1
    try:
        severity_value = int(str(item.get("severity", 0) or 0))
    except (TypeError, ValueError):
        severity_value = 0
    severity = _SEVERITY_NAMES.get(severity_value, "diagnostic")
    message = " ".join(str(item.get("message") or "").split())
    source = str(item.get("source") or "").strip()
    code = str(item.get("code") or "").strip()
    origin = "/".join(part for part in (source, code) if part)
    suffix = f" ({origin})" if origin else ""
    return f"- {path}:{line}:{character} [{severity}] {message}{suffix}"


def collect_write_diagnostics(
    paths: Iterable[str], workspace: Path, *, operation: LspOperation | None = None
) -> dict:
    """一个总期限收集已提交文件；未观察与空诊断明确区分。"""
    settings = current_run_settings()
    enabled = settings.lsp_enabled and settings.lsp_write_diagnostics_enabled
    batch = {"enabled": enabled, "reports": [], "skipped": 0}
    if not enabled:
        return batch
    operation = operation or LspOperation(
        None, time.monotonic() + settings.lsp_diagnostic_wait
    )
    targets = _safe_targets(paths, workspace)
    batch["skipped"] = max(0, len(targets) - settings.lsp_write_diagnostic_max_files)
    targets = targets[: settings.lsp_write_diagnostic_max_files]
    from nz_coder.foundation.workspace_file_access import (
        WorkspaceFileAccess,
        WorkspaceFileIdentity,
    )
    from .client import LSPCancelledError, LSPTimeoutError

    access = WorkspaceFileAccess(workspace)
    for index, (relative, target) in enumerate(targets):
        if operation.cancelled() or operation.remaining() <= 0:
            batch["skipped"] += len(targets) - index
            break
        try:
            client = get_client_for_file(target, workspace, operation=operation)
            if client is None:
                result = DiagnosticReport(
                    relative,
                    "unavailable",
                    "not_observed",
                    reason_code="lsp_unavailable",
                )
            else:
                try:
                    result = client.diagnostics(
                        target,
                        reader=lambda name=relative: access.read_text_with_identity(
                            name, errors="replace"
                        ),
                        operation=operation,
                    )
                except FileNotFoundError:
                    client.close_document(target, operation=operation)
                    result = DiagnosticReport(
                        relative,
                        "unavailable",
                        "not_observed",
                        source_identity=WorkspaceFileIdentity.missing(),
                        reason_code="source_deleted",
                    )
        except LSPCancelledError:
            result = DiagnosticReport(
                relative, "cancelled", "not_observed", reason_code="cancelled"
            )
        except LSPTimeoutError:
            result = DiagnosticReport(
                relative,
                "timeout",
                "not_observed",
                reason_code="initializing_or_deadline",
            )
        except Exception:
            result = DiagnosticReport(
                relative, "unavailable", "not_observed", reason_code="lsp_unavailable"
            )
        item = result.to_dict()
        item["path"] = relative
        batch["reports"].append(item)
    return batch


def format_write_diagnostics(batch: dict) -> str:
    if not batch["enabled"]:
        return ""
    rows = []
    for report in batch["reports"]:
        path = report["path"]
        rows.append(
            f"- {path}: {report['status']}/{report['freshness']} "
            f"(analysis_environment={report['analysis_environment']})"
        )
        for item in report["items"]:
            rows.append(_format_diagnostic(path, item))
        if report["reason_code"]:
            rows.append(f"  reason: {report['reason_code']}")
        if report["truncated"]:
            rows.append("  diagnostics truncated; omitted items may contain errors")
    if batch["skipped"]:
        rows.append(f"- skipped diagnostics for {batch['skipped']} changed file(s)")
    if not rows:
        return ""
    heading = "<lsp-diagnostics>\nObservations after the write transaction committed:\n"
    closing = "\n</lsp-diagnostics>"
    limit = max(
        len(heading) + len(closing) + 90, current_run_settings().lsp_max_output_chars
    )
    selected = []
    for row in rows:
        if (
            len(heading)
            + len(closing)
            + sum(len(value) + 1 for value in selected)
            + len(row)
            + 90
            > limit
        ):
            selected.append(
                "- output truncated; this is not a complete diagnostic check"
            )
            break
        selected.append(row)
    return heading + "\n".join(selected) + closing
