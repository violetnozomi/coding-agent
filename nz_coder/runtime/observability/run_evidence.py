"""记录一次运行的文件变更、验证结果和审查意见，供报告和审查使用。"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field

from nz_coder.tool_platform.command_policy import classify_bash
from nz_coder.runtime.agent.child_result import ChildAgentResult
from nz_coder.runtime.agent.task_policy import is_documentation_file
from nz_coder.tools import (
    collect_filesystem_mutation_paths,
    is_filesystem_mutation_tool,
)
from nz_coder.intelligence.verification_planner import (
    classify_verification_command,
    classify_verification_segments,
    is_python_probe_command,
    verification_command_key,
    verification_output_failed,
    verification_success_is_reliable,
)
from nz_coder.runtime.verification.evidence import (
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


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    return (text or "").strip()[:limit]


def _normalize_path(value: str) -> str:
    text = str(value or "").strip()
    return text.replace("\\", "/").lstrip("./")


def _parse_json_object(output: str) -> dict | list | None:
    text = (output or "").strip()
    if not text or len(text) > 200_000:
        return None
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _list_items(payload: dict, key: str, limit: int) -> list:
    items = payload.get(key)
    return items[:limit] if isinstance(items, list) else []


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


def _verification_status(output: str, success: bool, *, exit_code: int | None = None) -> str:
    if not success:
        parsed = _status_from_output(output)
        return parsed if parsed in {"failed", "error", "denied"} else "failed"
    # 已有实际退出状态时，预期异常的日志不能把真实成功重新解释为失败。
    if type(exit_code) is int:
        return "passed" if exit_code == 0 else "failed"
    return "failed" if verification_output_failed(output) else "passed"


def _looks_like_limitation(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _LIMITATION_HINTS)


@dataclass
class RunEvidence:
    run_id: str
    task_assessment: dict | None = None
    project_spec: dict | None = None
    blueprint: dict | None = None
    created_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    actual_output_paths: list[str] = field(default_factory=list)
    _verification_results: list[dict] = field(default_factory=list, repr=False)
    _verification_evidence: list[dict] = field(default_factory=list, repr=False)
    _build_results: list[dict] = field(default_factory=list, repr=False)
    verification_domain: object | None = field(default=None, repr=False)
    impact_review: dict | None = None
    completeness_review: dict | None = None
    limitations: list[str] = field(default_factory=list)
    tool_failures: list[dict] = field(default_factory=list)
    child_outcomes: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def _append_unique(self, items: list[str], value: str, limit: int) -> None:
        normalized = str(value or "").strip()
        if "/" in normalized or "\\" in normalized:
            normalized = _normalize_path(normalized)
        if normalized and normalized not in items and len(items) < limit:
            items.append(normalized)

    def _append_result(self, items: list[dict], payload: dict, limit: int) -> None:
        if payload and payload not in items and len(items) < limit:
            items.append(payload)

    def _record_output_path(self, path: str, *, created: bool) -> None:
        # 两类文件共用一份按出现顺序排列的输出清单，各清单单独去重和限额。
        files = self.created_files if created else self.modified_files
        self._append_unique(files, path, _MAX_FILES)
        self._append_unique(self.actual_output_paths, path, _MAX_FILES)

    def _record_workspace_write(self, tool_name: str, tool_input: dict) -> None:
        writes_files = is_filesystem_mutation_tool(tool_name) and not tool_input.get("dry_run")
        paths = collect_filesystem_mutation_paths(tool_input) if writes_files else ()

        # 接入验证域后，由域按工作区版本判定哪些结果仍然有效。
        if self.verification_domain is None:
            if tool_name == "bash":
                command = str(tool_input.get("command") or "")
                invalidates_checks = classify_bash(command).get("mutating")
            else:
                # 写入范围不明时也要重新验证，只有明确全是文档的改动可以沿用结果。
                invalidates_checks = writes_files and (
                    not paths or not all(is_documentation_file(path) for path in paths)
                )
            if invalidates_checks:
                self._verification_results.clear()
                self._verification_evidence.clear()
                self._build_results.clear()

        for path in paths:
            self._record_output_path(path, created=False)

    @property
    def verification_results(self) -> list[dict]:
        if self.verification_domain is None:
            return self._verification_results
        snapshot = self.verification_domain.snapshot()
        executions = {fact.execution_id: fact for fact in snapshot.executions}
        results = []
        for spec in snapshot.specs:
            decision = snapshot.decision(spec.id)
            execution = executions.get(decision.execution_id)
            # 一次执行可以覆盖其他检查。报告只列实际执行，并保留计划命令供对照。
            if execution is None or decision.status == "covered":
                continue
            results.append({
                "command": execution.command.original,
                "check_command": spec.command.original,
                "stage": spec.stage,
                "status": decision.status,
                "summary": decision.reason,
                "cwd": execution.command.cwd,
                "evidence_generation": decision.evidence_generation,
                "exit_code": execution.exit_code,
            })
        return results[-_MAX_RESULTS:]

    @property
    def verification_evidence(self) -> list[dict]:
        if self.verification_domain is None:
            return self._verification_evidence
        return [dict(item) for item in self.verification_results]

    @property
    def build_results(self) -> list[dict]:
        if self.verification_domain is None:
            return self._build_results
        return [item for item in self.verification_results if item["stage"] == "static"]

    def _upsert_verification_result(self, items: list[dict], payload: dict) -> None:
        """同一阶段的同一命令用最新结果原位替换，即使列表已满也能记下重试。"""
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
            for item in _list_items(payload, key, 6):
                self._note_or_limitation(str(item))

    def _record_blueprint(self, payload: dict) -> None:
        self.blueprint = dict(payload)
        for item in _list_items(payload, "files", _MAX_FILES):
            if isinstance(item, dict) and item.get("path"):
                self._append_unique(self.expected_files, str(item["path"]), _MAX_FILES)
        for note in _list_items(payload, "notes", 6):
            self._note_or_limitation(str(note))

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
                self._record_output_path(line[2:].strip(), created=True)

    def _record_write_batch(self, payload: dict | None, tool_input: dict | None, output: str) -> None:
        if isinstance(payload, dict):
            for path in payload.get("created", [])[:_MAX_FILES]:
                self._record_output_path(path, created=True)
            for path in payload.get("updated", [])[:_MAX_FILES]:
                self._record_output_path(path, created=False)
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
                self._record_output_path(path, created=True)
            elif section == "updated":
                self._record_output_path(path, created=False)

        if parsed_any:
            return
        files = (tool_input or {}).get("files", [])
        if isinstance(files, list):
            for item in files[:_MAX_FILES]:
                if isinstance(item, dict) and item.get("path"):
                    self._record_output_path(str(item["path"]), created=False)

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
        status = _verification_status(output, success, exit_code=exit_code)
        if status == "failed" and is_environment_verification_failure(
            command,
            output,
            exit_code=exit_code,
        ):
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

    def _record_impact(self, payload: dict | None, output: str) -> None:
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
        for note in _list_items(payload, "notes", 6):
            self._note_or_limitation(str(note))
        missing = _list_items(payload, "missing", 2)
        if missing and status != "ok":
            self._add_note("inspection missing: " + ", ".join(str(item) for item in missing))

    def _record_completeness(self, payload: dict) -> None:
        self.completeness_review = dict(payload)
        for limitation in _list_items(payload, "limitations", 6):
            self.add_limitation(str(limitation))
        for note in _list_items(payload, "notes", 6):
            self._note_or_limitation(str(note))
        missing = _list_items(payload, "missing", 2)
        if missing:
            self._add_note("completeness missing: " + ", ".join(str(item) for item in missing))
        for step in _list_items(payload, "recommended_next_steps", 3):
            self._add_note(f"next: {step}")

    def _record_acceptance(self, payload: dict) -> None:
        for item in _list_items(payload, "expected_outputs", 4):
            self._note_or_limitation(str(item))

    def _record_child_outcome(self, tool_name: str, child: ChildAgentResult) -> None:
        changed_files = [
            _normalize_path(str(path))
            for path in child.changed_files[:_MAX_FILES]
            if str(path).strip()
        ]
        verification_summary = (child.verification or {}).get("summary") or ""
        outcome = {
            "session_id": child.session_id,
            "agent_id": child.agent_id,
            "parent_session_id": child.parent_session_id,
            "trace_id": child.trace_id,
            "status": child.status,
            "changed_files": changed_files,
            "conflicts": list(child.conflicts)[:_MAX_RESULTS],
            "verification": _preview(str(verification_summary)),
        }
        self._append_result(self.child_outcomes, outcome, _MAX_RESULTS)

        # 子任务先在自己的工作区修改文件，父运行应用成功后才认领这些路径。
        if tool_name == "apply_agent_changes" and child.status == "applied":
            for path in changed_files:
                self._record_output_path(path, created=False)

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
        parsed_output = _parse_json_object(output_text)
        payload = parsed_output if isinstance(parsed_output, dict) else None
        metadata = metadata if isinstance(metadata, dict) else {}

        child = ChildAgentResult.from_metadata(
            metadata,
            final_text=output_text,
            name=tool_name or "child",
        )
        if tool_name in {"task", "apply_agent_changes"} and child is not None:
            self._record_child_outcome(tool_name, child)

        if success:
            self._record_workspace_write(tool_name, tool_input)

        if tool_name == "analyze_project_requirements" and payload is not None:
            self._record_project_spec(payload)
        elif tool_name == "create_project_blueprint" and payload is not None:
            self._record_blueprint(payload)
        elif tool_name == "scaffold_project":
            self._record_scaffold(output_text)
        elif tool_name == "write_files_batch" and success:
            self._record_write_batch(payload, tool_input, output_text)
        elif tool_name == "analyze_impact":
            self._record_impact(payload, output_text)
        elif tool_name == "inspect_generated_project" and payload is not None:
            self._record_inspection(payload)
        elif tool_name == "check_project_completeness" and payload is not None:
            self._record_completeness(payload)
        elif tool_name == "plan_project_acceptance" and payload is not None:
            self._record_acceptance(payload)

        verification_tool = tool_name in {
            "verify_project_build",
            "verify_changed_files",
            "python_symbol_check",
        }
        if self.verification_domain is None:
            if tool_name == "bash" and not dispatch_failed:
                exit_code = metadata.get("exit")
                stages = self._record_bash_verification(
                    tool_input,
                    output_text,
                    success and not command_failed,
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                )
                # Bash 也能执行普通命令。只有这次识别出了验证阶段，才按检查处理失败。
                verification_tool = bool(stages)
            elif tool_name == "verify_project_build":
                self._record_verify_project_build(output_text)
            elif tool_name == "verify_changed_files":
                self._record_verify_changed_files(output_text)
            elif tool_name == "python_symbol_check":
                self._record_symbol_check(tool_input, output_text, success)

        # 工具明确报错或拒绝执行时，即使是验证工具也要留下失败记录。
        tool_error = output_text.startswith(("Error:", "Denied"))
        failed = not success or output_text.startswith("FAIL")
        if tool_error or (failed and not verification_tool):
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
        """完整报告保留需求和蓝图的原始引用，并附上本次运行的观测记录。"""
        return {
            "run_id": self.run_id,
            "task_assessment": copy.deepcopy(self.task_assessment),
            "project_spec": self.project_spec,
            "blueprint": self.blueprint,
            **self._observations(),
        }

    def review_input(self) -> dict:
        """审查使用任务评估的独立副本和实际观测到的证据。"""
        return {
            "task_assessment": copy.deepcopy(self.task_assessment),
            **self._observations(),
        }

    def _observations(self) -> dict:
        # 两个出口都复制清单和其中的记录字典，记录里的嵌套值沿用原对象。
        return {
            "created_files": list(self.created_files),
            "modified_files": list(self.modified_files),
            "expected_files": list(self.expected_files),
            "actual_output_paths": list(self.actual_output_paths),
            "verification_results": [dict(item) for item in self.verification_results],
            "verification_evidence": [dict(item) for item in self.verification_evidence],
            "build_results": [dict(item) for item in self.build_results],
            "impact_review": (
                dict(self.impact_review) if isinstance(self.impact_review, dict) else self.impact_review
            ),
            "completeness_review": (
                dict(self.completeness_review)
                if isinstance(self.completeness_review, dict)
                else self.completeness_review
            ),
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
            passed = counts["passed"] + counts["ok"]
            failed = sum(counts[status] for status in ("failed", "error", "denied", "blocked", "timeout"))
            missing = counts["missing_dependency"]
            parts = [f"{passed} passed", f"{failed} failed"]
            if missing:
                parts.append(f"{missing} missing_dependency")
            if counts["warn"]:
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
