"""Tests for InfCode-aligned synthetic input expansion budgeting."""
from __future__ import annotations

from types import SimpleNamespace

from nz_coder.state.context import prompt_budget
from nz_coder.state.input_expansion import (
    compact_stored,
    resolve_and_apply_budget,
    tag_file_attachments,
)


def test_single_large_attachment_is_truncated_without_touching_user_text(tmp_path):
    (tmp_path / "large.txt").write_text("A" * 20_000, encoding="utf-8")
    message = {"role": "user", "content": "review this"}
    tag_file_attachments(
        message,
        "review this",
        [SimpleNamespace(path="large.txt", size=20_000)],
    )

    stats = resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=4_000, output_tokens=1_000),
        tmp_path,
    )

    assert message["content"].startswith("review this\n\n")
    assert "Context truncated" in message["content"]
    assert message["_nz_user_text"] == "review this"
    assert message["_nz_input_expansions"][0]["truncated"] is True
    assert stats == {"resolved": 1, "truncated": 1, "compacted": 0}

    first_render = message["content"]
    second = resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=4_000, output_tokens=1_000),
        tmp_path,
    )
    assert message["content"] == first_render
    assert second == {"resolved": 0, "truncated": 0, "compacted": 0}


def test_bounded_cjk_attachment_estimates_unread_tail_by_sample_density(tmp_path):
    """Large CJK files must not use the ASCII four-bytes-per-token fallback."""
    (tmp_path / "cjk.txt").write_text("中" * 100_000, encoding="utf-8")
    message = {"role": "user", "content": "review"}
    tag_file_attachments(
        message,
        "review",
        [SimpleNamespace(path="cjk.txt", size=300_000)],
    )

    resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=4_000, output_tokens=1_000),
        tmp_path,
    )

    assert message["_nz_input_expansions"][0]["originalTokens"] >= 95_000


def test_multiple_attachments_keep_later_small_source_and_tombstone_earlier(tmp_path):
    (tmp_path / "large.txt").write_text("L" * 20_000, encoding="utf-8")
    (tmp_path / "small.txt").write_text("small evidence", encoding="utf-8")
    message = {"role": "user", "content": "compare"}
    tag_file_attachments(
        message,
        "compare",
        [
            SimpleNamespace(path="large.txt", size=20_000),
            SimpleNamespace(path="small.txt", size=14),
        ],
    )

    resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=4_000, output_tokens=1_000),
        tmp_path,
    )

    first, second = message["_nz_input_expansions"]
    assert first["compacted"] is True
    assert "use read_file" in first["text"]
    assert second.get("compacted") is not True
    assert "small evidence" in message["content"]


def test_preflight_compaction_only_degrades_expansion_content(tmp_path):
    (tmp_path / "notes.txt").write_text("important details", encoding="utf-8")
    message = {"role": "user", "content": "natural instruction"}
    tag_file_attachments(
        message,
        "natural instruction",
        [SimpleNamespace(path="notes.txt", size=17)],
    )
    resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=64_000, output_tokens=8_000),
        tmp_path,
    )

    degraded = compact_stored([message], "preflight")

    assert degraded == 1
    assert message["content"].startswith("natural instruction\n\n")
    assert "use read_file" in message["content"]
    assert message["_nz_user_text"] == "natural instruction"
    assert message["_nz_input_expansions"][0]["compactionReason"] == "preflight"


def test_attachment_source_escape_is_replaced_not_read(tmp_path):
    outside = tmp_path.parent / "outside-expansion.txt"
    outside.write_text("secret", encoding="utf-8")
    message = {
        "role": "user",
        "content": "inspect",
        "_nz_user_text": "inspect",
        "_nz_input_expansions": [{
            "kind": "file",
            "source": str(outside),
            "resolved": False,
        }],
    }

    stats = resolve_and_apply_budget(
        [message],
        prompt_budget(context_tokens=64_000, output_tokens=8_000),
        tmp_path,
    )

    assert "secret" not in message["content"]
    assert "Context omitted" in message["content"]
    assert stats["compacted"] == 1
