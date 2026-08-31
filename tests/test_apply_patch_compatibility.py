"""Compatibility tests for provider-emitted apply_patch argument shapes."""
from __future__ import annotations

from nz_coder.foundation import config
from nz_coder.tools import dispatch, get_specs
from nz_coder.tools.files import apply_patch


def test_apply_patch_inherits_top_level_path_for_single_file_hunks(tmp_path):
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "module.py"
        target.write_text("first = 1\nsecond = 2\n", encoding="utf-8")

        result = apply_patch(
            [
                {"old_text": "first = 1", "new_text": "first = 10"},
                {"old_text": "second = 2", "new_text": "second = 20"},
            ],
            path="module.py",
        )

        assert result.startswith("Applied patch (2 changes across 1 files)")
        assert target.read_text(encoding="utf-8") == "first = 10\nsecond = 20\n"
    finally:
        config.WORKDIR = old_workdir


def test_apply_patch_schema_exposes_top_level_path_fallback():
    spec = next(
        item for item in get_specs()
        if item.get("function", {}).get("name") == "apply_patch"
    )

    properties = spec["function"]["parameters"]["properties"]
    assert "path" in properties
    assert "single-file" in properties["path"]["description"]

    change_schema = properties["changes"]["items"]
    assert "path" in change_schema["required"]


def test_apply_patch_dispatch_preserves_top_level_path(tmp_path):
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "module.py"
        target.write_text("value = 1\n", encoding="utf-8")

        result = dispatch("apply_patch", {
            "path": "module.py",
            "changes": [{"old_text": "value = 1", "new_text": "value = 2"}],
        })

        assert result.startswith("Applied patch")
        assert target.read_text(encoding="utf-8") == "value = 2\n"
    finally:
        config.WORKDIR = old_workdir


def test_apply_patch_append_adds_eof_content_without_brittle_anchor(tmp_path):
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        target = tmp_path / "tests" / "test_app.py"
        target.parent.mkdir()
        target.write_text("def test_existing():\n    assert True\n", encoding="utf-8")

        result = apply_patch([{
            "op": "append",
            "path": "tests/test_app.py",
            "new_text": "\ndef test_new_case():\n    assert 1 + 1 == 2\n",
        }])

        assert result.startswith("Applied patch")
        assert target.read_text(encoding="utf-8").endswith(
            "\ndef test_new_case():\n    assert 1 + 1 == 2\n"
        )
    finally:
        config.WORKDIR = old_workdir
