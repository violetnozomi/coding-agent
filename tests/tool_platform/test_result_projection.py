"""Contract tests for the unified model-visible tool result budget."""
from __future__ import annotations

from pathlib import Path

from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector


def test_small_result_is_preserved_without_artifact() -> None:
    projected = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=100),
        artifact_writer=lambda _call_id, _output: "should-not-run",
    ).project("call-1", "small evidence", tool_name="read_file")

    assert projected.text == "small evidence"
    assert projected.artifact_path is None
    assert projected.metadata["truncated"] is False
    assert projected.metadata["original_tokens"] == projected.metadata["projected_tokens"]


def test_large_result_preserves_head_tail_and_durable_reference(tmp_path: Path) -> None:
    artifact = tmp_path / "full.txt"

    def write(_call_id: str, output: str) -> str:
        artifact.write_text(output, encoding="utf-8")
        return ".nz-coder/session/tool-results/full.txt"

    original = "HEAD-SIGNAL\n" + ("middle-data\n" * 500) + "TAIL-FAILURE-SIGNAL"
    projected = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=120, head_fraction=0.55),
        artifact_writer=write,
    ).project("call-large", original, tool_name="bash")

    assert projected.metadata["truncated"] is True
    assert projected.metadata["tool_name"] == "bash"
    assert projected.metadata["original_tokens"] > projected.metadata["projected_tokens"]
    assert projected.metadata["projected_tokens"] <= 120
    assert projected.artifact_path == ".nz-coder/session/tool-results/full.txt"
    assert "HEAD-SIGNAL" in projected.text
    assert "TAIL-FAILURE-SIGNAL" in projected.text
    assert projected.artifact_path in projected.text
    assert artifact.read_text(encoding="utf-8") == original


def test_budget_scales_with_context_but_remains_bounded() -> None:
    small = ToolResultBudget.for_context(16_000)
    medium = ToolResultBudget.for_context(128_000)
    huge = ToolResultBudget.for_context(1_000_000)

    assert 512 <= small.max_tokens < medium.max_tokens
    assert medium.max_tokens <= huge.max_tokens <= 8_000


def test_projection_survives_artifact_persistence_failure() -> None:
    def fail(_call_id: str, _output: str) -> str:
        raise OSError("disk unavailable")

    projected = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=80),
        artifact_writer=fail,
    ).project("call-fail", "head\n" + ("x" * 2000) + "\ntail", tool_name="mcp_x")

    assert projected.metadata["truncated"] is True
    assert projected.metadata["artifact_error"] == "disk unavailable"
    assert projected.artifact_path is None
    assert projected.metadata["projected_tokens"] <= 80


def test_batch_budget_bounds_aggregate_results_and_preserves_pairing(tmp_path: Path) -> None:
    projector = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=120),
        artifact_writer=lambda call_id, _output: str(tmp_path / f"{call_id}.txt"),
    )
    items = [(f"call-{i}", "bash", f"HEAD-{i}\n" + "x" * 3000 + f"\nFAIL-{i}") for i in range(20)]

    projected = projector.project_batch(items, max_tokens=600)

    assert [item.metadata["tool_call_id"] for item in projected] == [item[0] for item in items]
    assert sum(item.metadata["projected_tokens"] for item in projected) <= 600
    visible = max(projected, key=lambda item: item.metadata["projected_tokens"])
    visible_index = int(visible.metadata["tool_call_id"].removeprefix("call-"))
    assert f"FAIL-{visible_index}" in visible.text
    assert str(tmp_path / f"call-{visible_index}.txt") in visible.text


def test_batch_budget_remains_strict_when_smaller_than_result_count() -> None:
    """Irreducible pressure keeps every pairing without inventing capacity."""
    projector = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=64),
        artifact_writer=lambda call_id, _output: (
            f".nz-coder/tool-results/{call_id}-full-output.txt"
        ),
    )
    items = [
        (f"call-{index}", "bash", "x" * 1000)
        for index in range(5)
    ]

    projected = projector.project_batch(items, max_tokens=2)

    assert len(projected) == 5
    assert sum(item.metadata["projected_tokens"] for item in projected) <= 2
    assert [item.metadata["tool_call_id"] for item in projected] == [
        item[0] for item in items
    ]
    assert all(item.metadata["artifact_path"] for item in projected)


def test_tiny_share_never_overflows_on_long_artifact_pointer() -> None:
    """A pointer that cannot fit remains in metadata instead of overflowing text."""
    projector = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=64),
        artifact_writer=lambda _call_id, _output: "very/" + ("long/" * 100),
    )

    projected = projector.project_batch(
        [("call", "bash", "failure\n" * 1000)],
        max_tokens=1,
    )[0]

    assert projected.metadata["projected_tokens"] <= 1
    assert projected.metadata["artifact_path"].startswith("very/long/")


def test_named_projection_policies_keep_the_most_useful_evidence() -> None:
    output = "FIRST-LINE\n" + "middle\n" * 500 + "FINAL-FAILURE"

    def writer(_call_id: str, _output: str) -> str:
        return "artifact.txt"

    read = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=80), artifact_writer=writer,
    ).project("read", output, tool_name="read_file")
    test = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=80), artifact_writer=writer,
    ).project("test", output, tool_name="pytest")

    assert "FIRST-LINE" in read.text
    assert read.metadata["policy"] == "head"
    assert "FINAL-FAILURE" in test.text
    assert test.metadata["policy"] == "tail"
