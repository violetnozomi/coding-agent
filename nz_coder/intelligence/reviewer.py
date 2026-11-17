"""Read-only structured evidence review for NZ-Coder runs."""
from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

from nz_coder.tools import register

_FAIL_STATUSES = {"failed", "error", "denied", "blocked", "timeout"}
_PASS_STATUSES = {"passed", "ok"}
_LIMITED_VERIFY_STATUSES = {"missing_dependency", "warn"}

_RUNTIME_REVIEW: ContextVar[Callable[[], dict] | None] = ContextVar(
    "nz_runtime_evidence_review", default=None,
)


@contextmanager
def scoped_runtime_review(reader: Callable[[], dict] | None):
    """Bind only the executing Agent's read capability, never a model payload."""
    token = _RUNTIME_REVIEW.set(reader)
    try:
        yield
    finally:
        _RUNTIME_REVIEW.reset(token)


def _short(text: str, limit: int = 180) -> str:
    return str(text or "").strip()[:limit]


def _add_unique(items: list[str], value: str) -> None:
    text = _short(value)
    if text and text not in items:
        items.append(text)


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(mapping: dict, key: str) -> list:
    value = mapping.get(key, [])
    return value if isinstance(value, list) else []


def _count(mapping: dict, key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, list):
        return len(value)
    count_key = f"{key}_count"
    try:
        return max(0, int(mapping.get(count_key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _verification_items(evidence: dict) -> list[dict]:
    items: list[dict] = []
    for key in ("verification_results", "build_results"):
        for item in _as_list(evidence, key)[:20]:
            if isinstance(item, dict):
                items.append(dict(item))
    if items:
        return items
    count = _count(evidence, "verification_results") or _count(evidence, "build_results")
    return [{"status": "unknown", "summary": "verification recorded"} for _ in range(min(count, 20))]


def _verification_summary(evidence: dict) -> dict:
    items = _verification_items(evidence)
    statuses = [str(item.get("status") or "unknown").strip().lower() for item in items]
    return {
        "items": items,
        "present": bool(items),
        "passed": any(status in _PASS_STATUSES for status in statuses),
        "failed": any(status in _FAIL_STATUSES for status in statuses),
        "only_missing_dependency": bool(statuses)
        and any(status == "missing_dependency" for status in statuses)
        and all(status in _LIMITED_VERIFY_STATUSES for status in statuses),
        "statuses": statuses,
    }


def _tool_failures(evidence: dict) -> list[dict]:
    return [item for item in _as_list(evidence, "tool_failures") if isinstance(item, dict)]


def _has_only_nonfatal_failures(failures: list[dict]) -> bool:
    if not failures:
        return False
    for item in failures:
        status = str(item.get("status") or "").lower()
        preview = str(item.get("preview") or "").lower()
        if status != "missing_dependency" and "missing depend" not in preview:
            return False
    return True


def _coverage_ratio(evidence: dict) -> tuple[int, int, float | None]:
    expected = [str(item) for item in _as_list(evidence, "expected_files") if str(item).strip()]
    created = [str(item) for item in _as_list(evidence, "created_files") if str(item).strip()]
    actual = [str(item) for item in _as_list(evidence, "actual_output_paths") if str(item).strip()]
    if not expected:
        return 0, 0, None
    modified = [str(item) for item in _as_list(evidence, "modified_files") if str(item).strip()]
    actual_set = set(created + actual + modified)
    covered = sum(1 for item in expected if item in actual_set)
    return covered, len(expected), round(covered / len(expected), 3)


def _normalize_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")

def _basename(path: str) -> str:
    return _normalize_path(path).rsplit("/", 1)[-1]

def _actual_output_paths(evidence: dict) -> list[str]:
    paths: list[str] = []
    for key in ("actual_output_paths", "modified_files", "created_files"):
        for item in _as_list(evidence, key):
            normalized = _normalize_path(str(item))
            if normalized and normalized not in paths:
                paths.append(normalized)
    return paths

def _requested_path_conflicts(evidence: dict, runtime: dict) -> list[dict]:
    requested = [_normalize_path(str(item)) for item in _as_list(runtime, "requested_paths") if str(item).strip()]
    actual = _actual_output_paths(evidence)
    actual_set = set(actual)
    conflicts: list[dict] = []
    for requested_path in requested:
        if not requested_path or requested_path in actual_set:
            continue
        requested_name = _basename(requested_path)
        if not requested_name:
            continue
        same_basename = [path for path in actual if _basename(path) == requested_name]
        if same_basename:
            conflicts.append({
                "requested": requested_path,
                "actual": same_basename[0],
            })
    return conflicts

def _base_review() -> dict:
    return {
        "review_status": "needs_fix",
        "score": 0.0,
        "reasons": [],
        "missing_evidence": [],
        "limitations": [],
        "required_next_steps": [],
        "final_answer_guidance": [],
        "summary": "",
    }


def _finalize_review(review: dict) -> dict:
    status = review["review_status"]
    score_map = {
        "approved": 0.9,
        "approved_with_limitations": 0.75,
        "needs_fix": 0.45,
        "failed": 0.15,
    }
    score = score_map.get(status, 0.4)
    if review["limitations"]:
        score -= 0.05
    if review["missing_evidence"]:
        score -= 0.1
    if status in {"approved", "approved_with_limitations"} and not review["reasons"]:
        _add_unique(review["reasons"], "Recorded evidence is sufficient.")
    if status == "approved":
        guidance = [
            "State what changed or was created.",
            "Report the verification evidence that was recorded.",
        ]
    elif status == "approved_with_limitations":
        guidance = [
            "State what is complete.",
            "Explicitly call out the remaining limitation.",
        ]
    elif status == "needs_fix":
        guidance = [
            "Do not claim full completion yet.",
            "Explain which evidence is still missing or failing.",
        ]
    else:
        guidance = [
            "Do not claim the task is complete.",
            "Say what core evidence is missing before retrying.",
        ]
    for item in guidance:
        _add_unique(review["final_answer_guidance"], item)
    summary_parts = [status]
    if review["reasons"]:
        summary_parts.append(review["reasons"][0])
    if review["missing_evidence"]:
        summary_parts.append("missing=" + ", ".join(review["missing_evidence"][:2]))
    if review["limitations"]:
        summary_parts.append("limitations=" + review["limitations"][0])
    review["score"] = round(max(0.0, min(1.0, score)), 2)
    review["summary"] = "; ".join(summary_parts[:3])
    return review


def _review_artifacts(evidence: dict, runtime: dict) -> dict:
    review = _base_review()
    created_count = _count(evidence, "created_files")
    actual_count = _count(evidence, "actual_output_paths") + _count(evidence, "modified_files")
    expected_count = _count(evidence, "expected_files")
    verify = _verification_summary(evidence)
    completeness = _as_dict(evidence.get("completeness_review"))
    completeness_status = str(completeness.get("status") or "").strip().lower()
    limitations = [str(item) for item in _as_list(evidence, "limitations")[:4] if str(item).strip()]
    tool_failures = _tool_failures(evidence)
    covered, total, ratio = _coverage_ratio(evidence)

    review["limitations"] = limitations[:3]

    if not (created_count or actual_count):
        review["review_status"] = "failed"
        _add_unique(review["reasons"], "No requested output files were recorded.")
        _add_unique(review["missing_evidence"], "created_files")
        _add_unique(review["required_next_steps"], "Create the requested output files.")
        return _finalize_review(review)

    hard_tool_failures = bool(tool_failures and not _has_only_nonfatal_failures(tool_failures))
    if hard_tool_failures:
        _add_unique(review["reasons"], f"{len(tool_failures)} tool failure(s) were recorded.")

    path_conflicts = _requested_path_conflicts(evidence, runtime)
    if path_conflicts:
        first = path_conflicts[0]
        review["review_status"] = "needs_fix"
        _add_unique(
            review["reasons"],
            f"User named target file {first['requested']}, but the run produced same-basename output at {first['actual']} instead.",
        )
        _add_unique(
            review["required_next_steps"],
            "Edit the exact requested path, or explicitly justify a different existing target before creating similarly named files elsewhere.",
        )
        return _finalize_review(review)

    wants_tests = bool(_as_dict(runtime).get("wants_tests"))
    tests_modified = bool(_as_dict(runtime).get("tests_modified"))
    if wants_tests and not tests_modified:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Task asks for tests but no test-file change was recorded.")
        _add_unique(review["required_next_steps"], "Add or update the requested tests and rerun a targeted check.")
        return _finalize_review(review)

    if not verify["present"]:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Project files exist but no build or verification evidence was recorded.")
        _add_unique(review["missing_evidence"], "verification_results")
        _add_unique(review["required_next_steps"], "Run the relevant build or verification command.")
        return _finalize_review(review)

    if verify["failed"]:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Verification recorded at least one failing result.")
        _add_unique(review["required_next_steps"], "Fix the failing verification command and rerun the relevant verification command.")
        return _finalize_review(review)

    if total:
        if ratio is not None and ratio < 0.5:
            review["review_status"] = "failed"
            _add_unique(review["reasons"], f"Expected file coverage is too low ({covered}/{total}).")
            _add_unique(review["missing_evidence"], "expected_files_coverage")
            _add_unique(review["required_next_steps"], "Create the missing expected files.")
            return _finalize_review(review)
        if ratio is not None and ratio < 1.0:
            review["review_status"] = "needs_fix"
            _add_unique(review["reasons"], f"Some expected files are still missing ({covered}/{total}).")
            _add_unique(review["missing_evidence"], "expected_files_coverage")
            _add_unique(review["required_next_steps"], "Create the remaining expected files.")
            return _finalize_review(review)
    elif expected_count == 0:
        _add_unique(review["reasons"], "Expected files were not recorded; review is based on created files and verification only.")

    if completeness_status in {"missing", "failed"}:
        review["review_status"] = "failed"
        _add_unique(review["reasons"], f"Completeness review reported {completeness_status}.")
        _add_unique(review["required_next_steps"], "Address the missing required outputs.")
        return _finalize_review(review)

    if completeness_status == "partial":
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Completeness review is only partial.")
        _add_unique(review["required_next_steps"], "Address the missing completeness items before claiming full completion.")
        return _finalize_review(review)

    if verify["only_missing_dependency"]:
        review["review_status"] = "approved_with_limitations"
        _add_unique(review["reasons"], "Project structure is present, but verification depends on local dependencies.")
        _add_unique(review["limitations"], "Install local dependencies and rerun verification to fully confirm the project.")
        _add_unique(review["required_next_steps"], "Install dependencies and rerun the relevant verification command.")
        return _finalize_review(review)

    review["review_status"] = "approved_with_limitations" if limitations else "approved"
    _add_unique(review["reasons"], "Project files and verification evidence are present.")
    if completeness_status in {"ok", "ok_with_limitations"}:
        _add_unique(review["reasons"], f"Completeness review reported {completeness_status}.")
    if hard_tool_failures:
        review["review_status"] = "needs_fix"
        _add_unique(review["required_next_steps"], "Resolve the recorded tool failures before claiming completion.")
    return _finalize_review(review)


def _review_code_change(evidence: dict, runtime: dict) -> dict:
    review = _base_review()
    modified_count = sum(_count(evidence, key) for key in ("modified_files", "created_files", "actual_output_paths"))
    has_diff = bool(
        runtime.get("has_diff") or runtime.get("changed_files")
        or runtime.get("edits_this_run") or runtime.get("diff_chars")
    )
    verify = _verification_summary(evidence)
    limitations = [str(item) for item in _as_list(evidence, "limitations")[:4] if str(item).strip()]
    tool_failures = _tool_failures(evidence)

    review["limitations"] = limitations[:3]

    if not modified_count and not has_diff:
        review["review_status"] = "failed"
        _add_unique(review["reasons"], "No modified files or diff evidence were recorded.")
        _add_unique(review["missing_evidence"], "modified_files")
        _add_unique(review["required_next_steps"], "Make the requested code change before finalizing.")
        return _finalize_review(review)

    hard_tool_failures = bool(tool_failures and not _has_only_nonfatal_failures(tool_failures))
    if hard_tool_failures:
        _add_unique(review["reasons"], f"{len(tool_failures)} tool failure(s) were recorded.")

    path_conflicts = _requested_path_conflicts(evidence, runtime)
    if path_conflicts:
        first = path_conflicts[0]
        review["review_status"] = "needs_fix"
        _add_unique(
            review["reasons"],
            f"User named target file {first['requested']}, but the run produced same-basename output at {first['actual']} instead.",
        )
        _add_unique(
            review["required_next_steps"],
            "Edit the exact requested path, or explicitly justify a different existing target before creating similarly named files elsewhere.",
        )
        return _finalize_review(review)

    wants_tests = bool(_as_dict(runtime).get("wants_tests"))
    tests_modified = bool(_as_dict(runtime).get("tests_modified"))
    if wants_tests and not tests_modified:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Task asks for tests but no test-file change was recorded.")
        _add_unique(review["required_next_steps"], "Add or update the requested tests and rerun a targeted check.")
        return _finalize_review(review)

    if not verify["present"]:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Code changes exist but no verification evidence was recorded.")
        _add_unique(review["missing_evidence"], "verification_results")
        _add_unique(review["required_next_steps"], "Run verify_changed_files or a targeted test command.")
        return _finalize_review(review)

    if verify["failed"]:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Verification recorded a failure.")
        _add_unique(review["required_next_steps"], "Fix the failing verification and rerun a targeted check.")
        return _finalize_review(review)

    review["review_status"] = "approved"
    _add_unique(review["reasons"], "Code changes and verification evidence are present.")

    if limitations:
        review["review_status"] = "approved_with_limitations"
    if hard_tool_failures:
        review["review_status"] = "needs_fix"
        _add_unique(review["required_next_steps"], "Resolve the recorded tool failures before claiming completion.")
    return _finalize_review(review)


def _review_unknown(evidence: dict) -> dict:
    review = _base_review()
    tool_failures = _tool_failures(evidence)
    limitations = [str(item) for item in _as_list(evidence, "limitations")[:4] if str(item).strip()]
    review["limitations"] = limitations[:3]

    if _verification_summary(evidence)["failed"]:
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Verification recorded a failure.")
        _add_unique(review["required_next_steps"], "Resolve the failing verification before claiming completion.")
        return _finalize_review(review)

    if not any(evidence.values()):
        review["review_status"] = "approved"
        _add_unique(review["reasons"], "No changed files or expected artifacts were recorded.")
        return _finalize_review(review)

    if tool_failures and not _has_only_nonfatal_failures(tool_failures):
        review["review_status"] = "needs_fix"
        _add_unique(review["reasons"], "Tool failures were recorded for this run.")
        _add_unique(review["required_next_steps"], "Resolve the failed tool step before claiming completion.")
        return _finalize_review(review)

    review["review_status"] = "approved_with_limitations" if limitations else "approved"
    _add_unique(review["reasons"], "No blocking evidence was recorded.")
    return _finalize_review(review)

def _add_impact_findings(review: dict, evidence: dict, runtime: dict) -> None:
    """Keep observed patch risk visible even when the current run is read-only."""
    impact = _as_dict(evidence.get("impact_review"))
    diff_chars = 0
    try:
        diff_chars = int(_as_dict(runtime).get("diff_chars", 0) or 0)
    except (TypeError, ValueError):
        diff_chars = 0
    if not impact and diff_chars >= 1200:
        if review["review_status"] == "approved":
            review["review_status"] = "approved_with_limitations"
        _add_unique(review["limitations"], "Impact review was not recorded for a non-trivial diff.")
        _add_unique(review["required_next_steps"], "Run analyze_impact for a final risk summary.")
    elif impact.get("requires_replan"):
        if review["review_status"] == "approved":
            review["review_status"] = "approved_with_limitations"
        categories = [
            str(item.get("category") or "unknown")
            for item in impact.get("risk_signals", [])
            if isinstance(item, dict)
        ]
        detail = ", ".join(categories[:4]) or str(impact.get("risk") or "unknown")
        # 静态信号保留为限制；生成新路线不是风险已解决的证据，也不产生新验收义务。
        _add_unique(review["limitations"], f"Static patch risk signal: {detail}; this alone does not establish a defect.")
    elif impact.get("risk"):
        _add_unique(review["reasons"], f"Impact review recorded risk={impact.get('risk')}.")


def review_run_evidence(evidence: dict, runtime: dict | None = None) -> dict:
    """Review recorded outputs and user obligations independently of assessment labels."""
    evidence = evidence if isinstance(evidence, dict) else {}
    runtime = runtime if isinstance(runtime, dict) else {}
    reviews: list[dict] = []
    artifact_facts = any(
        _count(evidence, key)
        for key in ("created_files", "expected_files")
    ) or bool(evidence.get("completeness_review"))
    change_facts = any(
        _count(evidence, key)
        for key in ("modified_files", "created_files", "actual_output_paths")
    ) or any(runtime.get(key) for key in (
        "has_diff", "changed_files", "edits_this_run", "diff_chars", "wants_tests",
    ))
    if artifact_facts:
        reviews.append(_review_artifacts(evidence, runtime))
    if change_facts:
        reviews.append(_review_code_change(evidence, runtime))
    if not reviews:
        reviews.append(_review_unknown(evidence))

    # 混合任务同时保留产物覆盖率和补丁风险；较轻的一项不能覆盖另一项的缺口。
    result = _base_review()
    severity = {"approved": 0, "approved_with_limitations": 1, "needs_fix": 2, "failed": 3}
    result["review_status"] = max(
        (review["review_status"] for review in reviews), key=severity.__getitem__,
    )
    for review in reviews:
        for key in ("reasons", "missing_evidence", "limitations", "required_next_steps"):
            for item in review[key]:
                _add_unique(result[key], item)
    _add_impact_findings(result, evidence, runtime)
    return _finalize_review(result)

def review_run_evidence_tool() -> str:
    """Model-facing review has no authority-bearing arguments.

    Offline callers can still use review_run_evidence(evidence, runtime).
    The tool never falls back to trusting caller-supplied ledgers.
    """
    reader = _RUNTIME_REVIEW.get()
    payload = reader() if reader is not None else {
        "review_status": "unavailable",
        "evidence_source": "runtime_unavailable",
        "summary": "No active Runtime evidence is bound. Do not infer completion.",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)



register(
    name="review_run_evidence",
    description=(
        "Inspect the current Agent's Runtime-owned evidence and unresolved requirements. "
        "Call with no arguments; do not supply files, commands, or verification claims. "
        "Read-only advisory review; normal completion and semantic review still apply."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    handler=review_run_evidence_tool,
    execution="read",
)
