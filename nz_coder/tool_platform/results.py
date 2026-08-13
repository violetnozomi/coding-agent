"""Unified token-aware projection policy for model-visible tool results."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Callable

from nz_coder.state.context import estimate_tokens, prompt_budget
from nz_coder.state.workdir import current_workdir
from nz_coder.sessions import session_tool_results_dir


@dataclass(frozen=True)
class ToolResultBudget:
    """Maximum model-visible size and evidence split for one tool result."""

    max_tokens: int
    head_fraction: float = 0.6

    def __post_init__(self) -> None:
        if self.max_tokens < 32:
            raise ValueError("ToolResultBudget max_tokens must be at least 32")
        if not 0.2 <= self.head_fraction <= 0.8:
            raise ValueError("ToolResultBudget head_fraction must be between 0.2 and 0.8")

    @classmethod
    def for_context(cls, context_tokens: int | None = None) -> ToolResultBudget:
        """Allocate four percent of usable input, bounded for stable requests."""
        budget = prompt_budget(context_tokens=context_tokens)
        base = budget.usable_input_tokens or 16_000
        return cls(max_tokens=min(8_000, max(512, int(base * 0.04))))


@dataclass(frozen=True)
class ProjectedToolResult:
    """Bounded model text plus durable full-output provenance."""

    text: str
    metadata: dict
    artifact_path: str | None = None


ArtifactWriter = Callable[[str, str], str]


class ToolResultProjector:
    """Project every tool result through one context-derived policy."""

    def __init__(
        self,
        *,
        budget: ToolResultBudget | None = None,
        artifact_writer: ArtifactWriter | None = None,
    ) -> None:
        self._budget = budget or ToolResultBudget.for_context()
        self._artifact_writer = artifact_writer or _persist_full_output

    @property
    def batch_max_tokens(self) -> int:
        """Aggregate ceiling for one assistant tool-call batch."""
        return min(16_000, self._budget.max_tokens * 2)

    def project(
        self,
        tool_call_id: str,
        output: str,
        *,
        tool_name: str = "",
    ) -> ProjectedToolResult:
        """Return unchanged small output or bounded head/tail evidence."""
        original = str(output)
        original_tokens = estimate_tokens(original)
        policy, head_fraction = _projection_policy(tool_name, self._budget.head_fraction)
        common = {
            "tool_name": str(tool_name),
            "original_chars": len(original),
            "original_tokens": original_tokens,
            "budget_tokens": self._budget.max_tokens,
            "policy": policy,
        }
        if original_tokens <= self._budget.max_tokens:
            return ProjectedToolResult(
                original,
                {
                    **common,
                    "projected_chars": len(original),
                    "projected_tokens": original_tokens,
                    "truncated": False,
                },
            )

        artifact_path = None
        artifact_error = ""
        try:
            artifact_path = self._artifact_writer(str(tool_call_id), original)
        except OSError as error:
            artifact_error = str(error)[:500]

        text = self._bounded_text(original, artifact_path, head_fraction=head_fraction)
        metadata = {
            **common,
            "projected_chars": len(text),
            "projected_tokens": estimate_tokens(text),
            "truncated": True,
            "artifact_path": artifact_path,
        }
        if artifact_error:
            metadata["artifact_error"] = artifact_error
        return ProjectedToolResult(text, metadata, artifact_path)

    def project_batch(
        self, items: list[tuple[str, str, str]], *, max_tokens: int,
    ) -> list[ProjectedToolResult]:
        """Project one contiguous call batch under an aggregate visible budget."""
        if not items:
            return []
        total = max(1, int(max_tokens))
        needs = [max(1, estimate_tokens(str(output))) for _call_id, _name, output in items]
        allocations = _adaptive_batch_allocations(needs, total)
        projected: list[ProjectedToolResult] = []
        for item_index, ((call_id, tool_name, output), share) in enumerate(zip(items, allocations)):
            if needs[item_index] <= share:
                policy, _fraction = _projection_policy(tool_name, self._budget.head_fraction)
                child = ProjectedToolResult(str(output), {
                    "tool_name": tool_name, "policy": policy,
                    "original_tokens": needs[item_index],
                    "projected_tokens": needs[item_index], "truncated": False,
                })
            elif share >= 32:
                child = ToolResultProjector(
                    budget=ToolResultBudget(share), artifact_writer=self._artifact_writer,
                ).project(call_id, output, tool_name=tool_name)
            else:
                artifact = None
                try:
                    artifact = self._artifact_writer(call_id, str(output))
                except OSError:
                    pass
                policy, _fraction = _projection_policy(tool_name, self._budget.head_fraction)
                signal = str(output).splitlines()[-1] if policy == "tail" else str(output).splitlines()[0]
                note = f"\n[full:{artifact}]" if artifact else ""
                text = signal[: max(1, share * 3)] + note
                while estimate_tokens(text) > share and len(signal) > 1:
                    signal = signal[: max(1, int(len(signal) * 0.75))]
                    text = signal + note
                child = ProjectedToolResult(text, {
                    "tool_call_id": call_id, "tool_name": tool_name, "policy": policy,
                    "original_tokens": estimate_tokens(str(output)),
                    "projected_tokens": estimate_tokens(text),
                    "truncated": True, "artifact_path": artifact,
                }, artifact)
            metadata = {
                **child.metadata, "tool_call_id": call_id,
                "batch_budget_tokens": total, "batch_allocated_tokens": share,
                "batch_allocation": "adaptive-small-first",
            }
            projected.append(ProjectedToolResult(child.text, metadata, child.artifact_path))
        return projected

    def _bounded_text(
        self, output: str, artifact_path: str | None, *, head_fraction: float,
    ) -> str:
        path_note = (
            f" Full output: {artifact_path}."
            if artifact_path
            else " Full output persistence failed."
        )
        marker = (
            "\n\n<persisted-output>\n"
            "[... tool result projected by context budget."
            f"{path_note} ...]\n"
            "</persisted-output>\n\n"
        )
        available = max(8, self._budget.max_tokens - estimate_tokens(marker) - 2)
        # Four characters per token is a useful initial bound for source text;
        # the loop below also handles CJK and JSON escaping conservatively.
        visible_chars = min(len(output), max(16, available * 4))
        while visible_chars > 8:
            head_chars = max(1, int(visible_chars * head_fraction))
            tail_chars = max(1, visible_chars - head_chars)
            candidate = output[:head_chars] + marker + output[-tail_chars:]
            if estimate_tokens(candidate) <= self._budget.max_tokens:
                return candidate
            visible_chars = int(visible_chars * 0.85)
        candidate = output[:4] + marker + output[-4:]
        while estimate_tokens(candidate) > self._budget.max_tokens and len(marker) > 8:
            marker = marker[: max(8, int(len(marker) * 0.8))]
            candidate = output[:2] + marker + output[-2:]
        return candidate


def _projection_policy(tool_name: str, default_fraction: float) -> tuple[str, float]:
    name = str(tool_name).lower()
    if name in {"read", "read_file", "grep", "grep_search", "glob", "list_directory"}:
        return "head", 0.8
    if name in {"bash", "pytest", "test", "run_tests", "task"} or "test" in name:
        return "tail", 0.2
    if "diff" in name or "patch" in name:
        return "head-tail", 0.5
    return "head-tail", default_fraction


def _adaptive_batch_allocations(needs: list[int], total: int) -> list[int]:
    """Water-fill a batch so small evidence completes before large outputs grow."""
    if not needs:
        return []
    count = len(needs)
    floor = min(32, max(1, total // count))
    allocations = [min(floor, need) for need in needs]
    remaining = max(0, total - sum(allocations))
    unfinished = {index for index, need in enumerate(needs) if allocations[index] < need}
    while remaining and unfinished:
        # Equal growth avoids one large result consuming all capacity.  When a
        # small result reaches its need it leaves the pool and capacity is
        # immediately redistributed among the remaining large results.
        share = max(1, remaining // len(unfinished))
        progressed = False
        for index in sorted(unfinished, key=lambda item: (needs[item], item)):
            grant = min(needs[index] - allocations[index], share, remaining)
            if grant <= 0:
                continue
            allocations[index] += grant
            remaining -= grant
            progressed = True
            if remaining == 0:
                break
        unfinished = {index for index in unfinished if allocations[index] < needs[index]}
        if not progressed:
            break
    return allocations


def _persist_full_output(tool_call_id: str, output: str) -> str:
    """Atomically persist one immutable full result in the active Session."""
    directory = session_tool_results_dir()
    directory.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_call_id or "unknown")[:120]
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
    path = directory / f"{safe_id}-{digest}.txt"
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{safe_id}-",
            suffix=".tmp",
            dir=str(directory),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(output)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
    try:
        return str(path.relative_to(current_workdir()))
    except ValueError:
        return str(path)
