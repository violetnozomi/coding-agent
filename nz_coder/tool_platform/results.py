"""Unified token-aware projection policy for model-visible tool results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from nz_coder.state.context import estimate_tokens, prompt_budget
from nz_coder.state.workdir import current_workdir
from nz_coder.state.sessions import active_session_id
from nz_coder.tool_platform.artifacts import ArtifactStore


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
        allocations = list(needs)
        current_tokens = list(needs)
        projected = []
        for (call_id, tool_name, output), need in zip(items, needs):
            policy, _fraction = _projection_policy(tool_name, self._budget.head_fraction)
            projected.append(ProjectedToolResult(str(output), {
                "tool_name": tool_name, "policy": policy,
                "original_tokens": need,
                "projected_tokens": need, "truncated": False,
            }))

        for item_index in sorted(
            range(len(items)), key=lambda index: (-needs[index], index),
        ):
            used = sum(current_tokens)
            if used <= total:
                break
            call_id, tool_name, output = items[item_index]
            other_tokens = used - current_tokens[item_index]
            share = max(0, total - other_tokens)
            allocations[item_index] = share
            if share >= 32:
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
                text = _tiny_projection_text(signal, artifact, share)
                child = ProjectedToolResult(text, {
                    "tool_call_id": call_id, "tool_name": tool_name, "policy": policy,
                    "original_tokens": estimate_tokens(str(output)),
                    "projected_tokens": estimate_tokens(text),
                    "truncated": True, "artifact_path": artifact,
                }, artifact)
            projected[item_index] = child
            current_tokens[item_index] = child.metadata["projected_tokens"]

        result: list[ProjectedToolResult] = []
        for item_index, ((call_id, _tool_name, _output), child) in enumerate(
            zip(items, projected)
        ):
            metadata = {
                **child.metadata, "tool_call_id": call_id,
                "batch_budget_tokens": total,
                "batch_allocated_tokens": allocations[item_index],
                "batch_allocation": "largest-first-spill",
            }
            result.append(ProjectedToolResult(child.text, metadata, child.artifact_path))
        return result

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


def _tiny_projection_text(
    signal: str,
    artifact_path: str | None,
    max_tokens: int,
) -> str:
    """Fit an irreducible result without allowing its pointer to overflow."""
    budget = max(0, int(max_tokens))
    if budget == 0:
        return ""
    signal = str(signal or "")
    note = f"[full:{artifact_path}]" if artifact_path else ""
    combined = f"{signal}\n{note}" if signal and note else signal or note
    if estimate_tokens(combined) <= budget:
        return combined
    if note and estimate_tokens(note) <= budget:
        note_tokens = estimate_tokens(note)
        prefix = _fit_prefix(signal, max(0, budget - note_tokens - 1))
        candidate = f"{prefix}\n{note}" if prefix else note
        if estimate_tokens(candidate) <= budget:
            return candidate
        return note
    # The durable pointer remains in structured metadata when its literal path
    # cannot fit.  Preserving protocol pairing is more important than emitting
    # an unusably truncated path.
    return _fit_prefix(signal, budget)


def _fit_prefix(value: str, max_tokens: int) -> str:
    """Return the longest practical prefix under a strict token ceiling."""
    budget = max(0, int(max_tokens))
    if budget == 0 or not value:
        return ""
    if estimate_tokens(value) <= budget:
        return value
    low = 0
    high = len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(value[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    candidate = value[:low]
    while candidate and estimate_tokens(candidate) > budget:
        candidate = candidate[:-1]
    return candidate


def _persist_full_output(tool_call_id: str, output: str) -> str:
    """Persist one immutable result and expose only an opaque Session handle."""
    del tool_call_id  # IDs are generated independently to prevent path influence.
    session_id = active_session_id() or "direct-tool"
    return ArtifactStore(current_workdir(), session_id).put(output, kind="tool-result")
