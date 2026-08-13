"""Structured observational evidence collected during a single agent run.

RunEvidence is intentionally lightweight in this MVP. It records what the
agent created or modified, what files were expected, what verification ran,
what quality reviews reported, and what limitations or tool failures were
seen. It does not gate or change AgentLoop control flow.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from nz_coder.runtime.child_result import ChildAgentResult
from nz_coder.verification_planner import (
    classify_verification_command,
    classify_verification_segments,
    is_python_probe_command,
    verification_command_key,
    verification_output_failed,
    verification_success_is_reliable,
)
from nz_coder.verification_evidence import (
    VerificationEvidence,
    is_environment_verification_failure,
)


_MAX_FILES = 50
_MAX_RESULTS = 20
_MAX_NOTES = 20
_PREVIEW_CHARS = 800
_BUILD_RESULT_RE = re.compile(r"^-\s+\[(?P<status>[A-Za-z_]+)\]\s+(?P<command>.+?)\s*$")
_LIMITATION_HINTS = (
    "sqlite",
    "in-memory",
    "fallback",
    "follow-up",
    "missing dependency",
    "local dependencies",
    "not implemented",
    "not supported",
)
_GENERIC_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "apply_patch",
    "replace_lines",
    "python_structural_edit",
}
_VERIFICATION_INVALIDATING_TOOLS = _GENERIC_WRITE_TOOLS | {
    "write_files_batch",
    "scaffold_project",
}


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    return (text or "").strip()[:limit]


def _normalize_path(value: str) -> str:
    text = str(value or "").strip()
    return text.replace("\\", "/").lstrip("./")


def _parse_json_object(output: str):
    text = (output or "").strip()
    if not text or len(text) > 200_000:
        return None
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _status_from_output(output: str) -> str:
    lowered = (output or "").lstrip().lower()
    if lowered.startswith("ok"):
        return "passed"
    if lowered.startswith("warn"):
        return "warn"
    if lowered.startswith("fail"):
        return "failed"
    if lowered.startswith("error"):
        return "error"
    if lowered.startswith("denied"):
        return "denied"
    return "unknown"


def _verification_status(output: str, success: bool) -> str:
    if not success:
        parsed = _status_from_output(output)
        return parsed if parsed in {"failed", "error", "denied"} else "failed"
    return "failed" if verification_output_failed(output) else "passed"


def _looks_like_limitation(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _LIMITATION_HINTS)


@dataclass
class RunEvidence:
    run_id: str
    task_mode: str = "unknown"
    project_spec: dict | None = None
    blueprint: dict | None = None
    created_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    actual_output_paths: list[str] = field(default_factory=list)
    verification_results: list[dict] = field(default_factory=list)
    verification_evidence: list[dict] = field(default_factory=list)
    build_results: list[dict] = field(default_factory=list)
    impact_review: dict | None = None
    completeness_review: dict | None = None
    limitations: list[str] = field(default_factory=list)
    tool_failures: list[dict] = field(default_factory=list)
    child_outcomes: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def _append_unique(self, items: list[str], value: str, limit: int) -> None:
        normalized = _normalize_path(value) if "/" in str(value or "") or "\\" in str(value or "") else str(value or "").strip()
        if normalized and normalized not in items and len(items) < limit:
            items.append(normalized)

    def _append_result(self, items: list[dict], payload: dict, limit: int) -> None:
        if payload and payload not in items and len(items) < limit:
            items.append(payload)

    def _write_invalidates_verification(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> bool:
        if tool_name not in _VERIFICATION_INVALIDATING_TOOLS:
            return False
        if bool(tool_input.get("dry_run")):
            return False
        if tool_name != "write_file":
            return True
        path = _normalize_path(str(tool_input.get("path") or ""))
        if "/" in path:
            return True
        lower = path.lower()
        if lower.startswith("test_") or lower.endswith("_test.py"):
            return False
        return not lower.endswith((".md", ".txt", ".rst"))

    def _upsert_verification_result(self, items: list[dict], payload: dict) -> None:
        """Keep the latest result for one stage/command instead of stale failure history."""
        command = verification_command_key(
            str(payload.get("command") or payload.get("tool") or "")
        )
        stage = str(payload.get("stage") or "unknown")
        for index, item in enumerate(items):
            item_command = verification_command_key(
                str(item.get("command") or item.get("tool") or "")
            )
            if str(item.get("stage") or "unknown") == stage and item_command == command:
                items[index] = dict(payload)
                return
        self._append_result(items, dict(payload), _MAX_RESULTS)

    def _add_note(self, text: str) -> None:
        self._append_unique(self.notes, str(text or "").strip(), _MAX_NOTES)

    def add_limitation(self, text: str) -> None:
        self._append_unique(self.limitations, str(text or "").strip(), _MAX_NOTES)

    def _note_or_limitation(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        if _looks_like_limitation(value):
            self.add_limitation(value)
        else:
            self._add_note(value)

    def _failure_target(self, tool_input: dict | None) -> str:
        tool_input = tool_input or {}
        for key in ("path", "project_dir", "target_dir", "project_name"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_path(value)
        return ""

    def _record_tool_failure(self, name: str, tool_input: dict | None, output: str, status: str | None = None) -> None:
        failure = {
            "name": name,
            "status": status or _status_from_output(output),
            "preview": _preview(output),
        }
        target = self._failure_target(tool_input)
        if target:
            failure["target"] = target
        self._append_result(self.tool_failures, failure, _MAX_RESULTS)

    def _record_project_spec(self, payload: dict) -> None:
        self.project_spec = dict(payload)
        for key in ("notes", "constraints"):
            values = payload.get(key, [])
            if isinstance(values, list):
                for item in values[:6]:
                    self._note_or_limitation(str(item))

    def _record_blueprint(self, payload: dict) -> None:
        self.blueprint = dict(payload)
        files = payload.get("files", [])
        if isinstance(files, list):
            for item in files[:_MAX_FILES]:
                if isinstance(item, dict) and item.get("path"):
                    self._append_unique(self.expected_files, str(item["path"]), _MAX_FILES)
        notes = payload.get("notes", [])
        if isinstance(notes, list):
            for item in notes[:6]:
                self._note_or_limitation(str(item))

    def _record_scaffold(self, output: str) -> None:
        collecting = False
        for raw_line in (output or "").splitlines():
            line = raw_line.strip()
            if line.startswith("Files created:"):
                collecting = True
                continue
            if line == "Next steps:":
                break
            if collecting and line.startswith("- "):
                path = line[2:].strip()
                self._append_unique(self.created_files, path, _MAX_FILES)
                self._append_unique(self.actual_output_paths, path, _MAX_FILES)

    def _record_write_batch(self, payload, tool_input: dict | None, output: str) -> None:
        if isinstance(payload, dict):
            for path in payload.get("created", [])[:_MAX_FILES]:
                self._append_unique(self.created_files, path, _MAX_FILES)
                self._append_unique(self.actual_output_paths, path, _MAX_FILES)
            for path in payload.get("updated", [])[:_MAX_FILES]:
                self._append_unique(self.modified_files, path, _MAX_FILES)
                self._append_unique(self.actual_output_paths, path, _MAX_FILES)
            failed = payload.get("failed", [])
            if isinstance(failed, list) and failed:
                self._record_tool_failure("write_files_batch", tool_input, output, status="failed")
            return

        section = ""
        parsed_any = False
        for raw_line in (output or "").splitlines():
            line = raw_line.strip()
            if line.startswith("Created:"):
                section = "created"
                continue
            if line.startswith("Updated:"):
                section = "updated"
                continue
            if line.startswith("Skipped:"):
                section = "skipped"
                continue
            if line.startswith("Failed:"):
                section = "failed"
                match = re.search(r"(\d+)", line)
                if match and int(match.group(1)) > 0:
                    self._record_tool_failure("write_files_batch", tool_input, output, status="failed")
                continue
            if not line.startswith("- "):
                continue
            parsed_any = True
            path = line[2:].strip()
            if section == "created":
                self._append_unique(self.created_files, path, _MAX_FILES)
                self._append_unique(self.actual_output_paths, path, _MAX_FILES)
            elif section == "updated":
                self._append_unique(self.modified_files, path, _MAX_FILES)
                self._append_unique(self.actual_output_paths, path, _MAX_FILES)

        if parsed_any:
            return
        files = (tool_input or {}).get("files", [])
        if isinstance(files, list):
            for item in files[:_MAX_FILES]:
                if isinstance(item, dict) and item.get("path"):
                    self._append_unique(self.modified_files, str(item["path"]), _MAX_FILES)
                    self._append_unique(self.actual_output_paths, str(item["path"]), _MAX_FILES)

    def _record_verify_project_build(self, output: str) -> None:
        lines = (output or "").splitlines()
        summary = lines[0].strip() if lines else "verify_project_build"
        parsed_items: list[dict] = []
        for raw_line in lines[1:]:
            line = raw_line.rstrip()
            match = _BUILD_RESULT_RE.match(line.strip())
            if match:
                command = match.group("command").strip()
                parsed_items.append({
                    "tool": "verify_project_build",
                    "command": command,
                    "stage": classify_verification_command(command) or "unknown",
                    "status": match.group("status").strip(),
                    "summary": summary,
                })
                continue
            if line.startswith("  ") and parsed_items:
                parsed_items[-1]["preview"] = _preview(line.strip())

        if not parsed_items:
            parsed_items.append({
                "tool": "verify_project_build",
                "stage": "unknown",
                "status": _status_from_output(output),
                "summary": summary,
                "preview": _preview(output),
            })

        for item in parsed_items[:_MAX_RESULTS]:
            self._upsert_verification_result(self.build_results, item)
            self._upsert_verification_result(self.verification_results, item)

        lowered = summary.lower()
        if lowered.startswith("warn:") and "local dependencies" in lowered:
            self.add_limitation("Project build verification needs local dependencies before all checks can pass.")

    def _record_verify_changed_files(self, output: str) -> None:
        lines = (output or "").splitlines()
        summary = lines[0].strip() if lines else "verify_changed_files"
        item = {
            "tool": "verify_changed_files",
            "command": "verify_changed_files",
            "stage": "static",
            "status": _status_from_output(output),
            "summary": summary,
            "preview": _preview(output),
        }
        self._upsert_verification_result(self.verification_results, item)

    def _record_bash_verification(
        self,
        tool_input: dict,
        output: str,
        success: bool,
        *,
        exit_code: int | None = None,
    ) -> list[str]:
        command = str(tool_input.get("command") or "").strip()
        status = _verification_status(output, success)
        if status == "failed" and is_environment_verification_failure(command, output):
            status = "missing_dependency"
        if status == "passed" and not verification_success_is_reliable(command):
            return []
        classified = classify_verification_segments(command)
        if not classified and status == "failed" and is_python_probe_command(command):
            classified = [("static", command)]
        stages: list[str] = []
        for stage, segment_command in classified:
            evidence = VerificationEvidence.observed(
                kind="command",
                command=segment_command,
                scope=stage,
                status=status,
                output=output,
                exit_code=exit_code,
                source="bash",
            ).to_dict()
            self._upsert_verification_result(self.verification_evidence, evidence)
            item = {
                "tool": "bash",
                "command": segment_command,
                "stage": stage,
                "status": status,
                "preview": _preview(output),
            }
            self._upsert_verification_result(self.verification_results, item)
            stages.append(stage)
        return stages

    def _record_symbol_check(self, tool_input: dict, output: str, success: bool) -> None:
        status = _status_from_output(output)
        if status == "unknown":
            status = "passed" if success else "failed"
        path = _normalize_path(str(tool_input.get("path") or ""))
        command = "python_symbol_check" + (f" {path}" if path else "")
        item = {
            "tool": "python_symbol_check",
            "command": command,
            "path": path,
            "stage": "static",
            "status": status,
            "preview": _preview(output),
        }
        self._upsert_verification_result(self.verification_results, item)

    def _record_impact(self, payload, output: str) -> None:
        if isinstance(payload, dict):
            self.impact_review = dict(payload)
            return
        match = re.search(r"^Patch risk:\s*([A-Za-z_]+)", output or "", re.MULTILINE)
        if match:
            requires = re.search(r"^Requires replan:\s*(true|false)", output or "", re.MULTILINE | re.IGNORECASE)
            fingerprint = re.search(r"^Risk fingerprint:\s*(\S+)", output or "", re.MULTILINE)
            self.impact_review = {
                "risk": match.group(1).strip().lower(),
                "requires_replan": bool(requires and requires.group(1).lower() == "true"),
                "fingerprint": fingerprint.group(1) if fingerprint else "",
                "summary": _preview(output),
            }
        elif output.strip():
            self._add_note(_preview(output))

    def _record_inspection(self, payload: dict) -> None:
        status = str(payload.get("status") or "").strip()
        if status:
            self._add_note(f"inspection: {status}")
        for item in payload.get("notes", [])[:6] if isinstance(payload.get("notes", []), list) else []:
            self._note_or_limitation(str(item))
        missing = payload.get("missing", [])
        if isinstance(missing, list) and missing and status != "ok":
            self._add_note("inspection missing: " + ", ".join(str(item) for item in missing[:2]))

    def _record_completeness(self, payload: dict) -> None:
        self.completeness_review = dict(payload)
        for item in payload.get("limitations", [])[:6] if isinstance(payload.get("limitations", []), list) else []:
            self.add_limitation(str(item))
        for item in payload.get("notes", [])[:6] if isinstance(payload.get("notes", []), list) else []:
            self._note_or_limitation(str(item))
        missing = payload.get("missing", [])
        if isinstance(missing, list) and missing:
            self._add_note("completeness missing: " + ", ".join(str(item) for item in missing[:2]))
        for step in payload.get("recommended_next_steps", [])[:3] if isinstance(payload.get("recommended_next_steps", []), list) else []:
            self._add_note(f"next: {step}")

    def _record_acceptance(self, payload: dict) -> None:
        outputs = payload.get("expected_outputs", [])
        if isinstance(outputs, list):
            for item in outputs[:4]:
                self._note_or_limitation(str(item))

    def record_tool_result(
        self,
        name: str,
        tool_input: dict | None,
        output: str,
        success: bool = True,
        *,
        dispatch_failed: bool = False,
        command_failed: bool = False,
        metadata: dict | None = None,
    ) -> None:
        tool_name = str(name or "").strip()
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        output_text = str(output or "")
        payload = _parse_json_object(output_text)
        metadata = metadata if isinstance(metadata, dict) else {}

        typed_child = ChildAgentResult.from_metadata(
            metadata,
            final_text=output_text,
            name=tool_name or "child",
        )
        if tool_name in {"task", "apply_agent_changes"} and typed_child is not None:
            child_outcome = {
                "session_id": typed_child.session_id,
                "agent_id": typed_child.agent_id,
                "parent_session_id": typed_child.parent_session_id,
                "trace_id": typed_child.trace_id,
                "status": typed_child.status,
                "changed_files": [
                    _normalize_path(str(item))
                    for item in typed_child.changed_files[:_MAX_FILES]
                    if str(item).strip()
                ],
                "conflicts": list(typed_child.conflicts)[:_MAX_RESULTS],
                "verification": _preview(str(
                    (typed_child.verification or {}).get("summary") or ""
                )),
            }
            self._append_result(self.child_outcomes, child_outcome, _MAX_RESULTS)
            if tool_name == "apply_agent_changes" and child_outcome["status"] == "applied":
                for path in child_outcome["changed_files"]:
                    self._append_unique(self.modified_files, path, _MAX_FILES)
                    self._append_unique(self.actual_output_paths, path, _MAX_FILES)

        if success and self._write_invalidates_verification(tool_name, tool_input):
            self.verification_results.clear()
            self.verification_evidence.clear()
            self.build_results.clear()

        if success and tool_name in _GENERIC_WRITE_TOOLS:
            path = tool_input.get("path")
            if isinstance(path, str) and path.strip():
                self._append_unique(self.modified_files, path, _MAX_FILES)
                self._append_unique(self.actual_output_paths, path, _MAX_FILES)

        bash_verification_stage = None
        if tool_name == "bash" and not dispatch_failed:
            bash_verification_stage = self._record_bash_verification(
                tool_input, output_text, success and not command_failed,
                exit_code=(
                    metadata.get("exit")
                    if isinstance(metadata.get("exit"), int)
                    else None
                ),
            )

        if tool_name == "analyze_project_requirements" and isinstance(payload, dict):
            self._record_project_spec(payload)
        elif tool_name == "create_project_blueprint" and isinstance(payload, dict):
            self._record_blueprint(payload)
        elif tool_name == "scaffold_project":
            self._record_scaffold(output_text)
        elif tool_name == "write_files_batch":
            self._record_write_batch(payload if isinstance(payload, dict) else None, tool_input, output_text)
        elif tool_name == "verify_project_build":
            self._record_verify_project_build(output_text)
        elif tool_name == "verify_changed_files":
            self._record_verify_changed_files(output_text)
        elif tool_name == "python_symbol_check":
            self._record_symbol_check(tool_input, output_text, success)
        elif tool_name == "analyze_impact":
            self._record_impact(payload if isinstance(payload, dict) else None, output_text)
        elif tool_name == "inspect_generated_project" and isinstance(payload, dict):
            self._record_inspection(payload)
        elif tool_name == "check_project_completeness" and isinstance(payload, dict):
            self._record_completeness(payload)
        elif tool_name == "plan_project_acceptance" and isinstance(payload, dict):
            self._record_acceptance(payload)

        verification_tool = bool(bash_verification_stage) or tool_name in {
            "verify_project_build",
            "verify_changed_files",
            "python_symbol_check",
        }
        dispatch_like_failure = output_text.startswith(("Error:", "Denied"))
        if ((not success) or output_text.startswith(("Error:", "Denied", "FAIL"))) and (
            not verification_tool or dispatch_like_failure
        ):
            self._record_tool_failure(tool_name, tool_input, output_text)

    def is_empty(self) -> bool:
        return not any([
            self.project_spec,
            self.blueprint,
            self.created_files,
            self.modified_files,
            self.expected_files,
            self.actual_output_paths,
            self.verification_results,
            self.verification_evidence,
            self.build_results,
            self.impact_review,
            self.completeness_review,
            self.limitations,
            self.tool_failures,
            self.child_outcomes,
            self.notes,
        ])

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task_mode": self.task_mode,
            "project_spec": self.project_spec,
            "blueprint": self.blueprint,
            "created_files": list(self.created_files),
            "modified_files": list(self.modified_files),
            "expected_files": list(self.expected_files),
            "actual_output_paths": list(self.actual_output_paths),
            "verification_results": [dict(item) for item in self.verification_results],
            "verification_evidence": [dict(item) for item in self.verification_evidence],
            "build_results": [dict(item) for item in self.build_results],
            "impact_review": dict(self.impact_review) if isinstance(self.impact_review, dict) else self.impact_review,
            "completeness_review": dict(self.completeness_review) if isinstance(self.completeness_review, dict) else self.completeness_review,
            "limitations": list(self.limitations),
            "tool_failures": [dict(item) for item in self.tool_failures],
            "child_outcomes": [dict(item) for item in self.child_outcomes],
            "notes": list(self.notes),
        }

    def review_input(self) -> dict:
        return {
            "task_mode": self.task_mode,
            "created_files": list(self.created_files),
            "modified_files": list(self.modified_files),
            "expected_files": list(self.expected_files),
            "actual_output_paths": list(self.actual_output_paths),
            "verification_results": [dict(item) for item in self.verification_results],
            "verification_evidence": [dict(item) for item in self.verification_evidence],
            "build_results": [dict(item) for item in self.build_results],
            "impact_review": dict(self.impact_review) if isinstance(self.impact_review, dict) else self.impact_review,
            "completeness_review": dict(self.completeness_review) if isinstance(self.completeness_review, dict) else self.completeness_review,
            "limitations": list(self.limitations),
            "tool_failures": [dict(item) for item in self.tool_failures],
            "child_outcomes": [dict(item) for item in self.child_outcomes],
            "notes": list(self.notes),
        }

    def summary_text(self, max_items: int = 8) -> str:
        if self.is_empty():
            return "RunEvidence: empty"

        lines = ["RunEvidence:"]
        if self.created_files:
            lines.append(f"- created_files: {len(self.created_files)}")
        if self.modified_files:
            lines.append(f"- modified_files: {len(self.modified_files)}")
        if self.expected_files:
            lines.append(f"- expected_files: {len(self.expected_files)}")
        if self.actual_output_paths and not (self.created_files or self.modified_files):
            lines.append(f"- output_paths: {len(self.actual_output_paths)}")
        if self.verification_results:
            counts = Counter(str(item.get("status") or "unknown") for item in self.verification_results)
            passed = counts.get("passed", 0) + counts.get("ok", 0)
            failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("denied", 0) + counts.get("blocked", 0) + counts.get("timeout", 0)
            missing = counts.get("missing_dependency", 0)
            parts = [f"{passed} passed", f"{failed} failed"]
            if missing:
                parts.append(f"{missing} missing_dependency")
            if counts.get("warn", 0):
                parts.append(f"{counts['warn']} warn")
            lines.append("- verification: " + ", ".join(parts))
        if isinstance(self.completeness_review, dict) and self.completeness_review.get("status"):
            lines.append(f"- completeness: {self.completeness_review.get('status')}")
        if isinstance(self.impact_review, dict) and self.impact_review.get("risk"):
            lines.append(f"- impact: {self.impact_review.get('risk')}")
        if self.limitations:
            lines.append(f"- limitations: {self.limitations[0]}")
        if self.tool_failures:
            lines.append(f"- tool_failures: {len(self.tool_failures)}")
        if self.child_outcomes:
            lines.append(f"- child_outcomes: {len(self.child_outcomes)}")
        return "\n".join(lines[: max(2, int(max_items or 8) + 1)])
