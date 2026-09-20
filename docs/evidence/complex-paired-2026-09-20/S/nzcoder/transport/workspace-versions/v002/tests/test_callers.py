"""Tests for the two settings-store callers: cli.update and sync.merge."""

import pytest

import store
from cli import update
from sync import merge


def test_update_returns_new_data(tmp_path):
    path = tmp_path / "s.json"
    assert update(path, "a", 1) == {"a": 1}
    assert update(path, "b", "x") == {"a": 1, "b": "x"}
    assert store.load(path) == {"a": 1, "b": "x"}


def test_update_forwards_expected_revision(tmp_path):
    path = tmp_path / "s.json"
    update(path, "a", 1)  # revision 1
    assert update(path, "b", 2, expected_revision=1) == {"a": 1, "b": 2}
    before = path.read_bytes()
    with pytest.raises(store.ConflictError):
        update(path, "c", 3, expected_revision=1)
    assert path.read_bytes() == before


def test_update_accepts_positional_expected_revision(tmp_path):
    path = tmp_path / "s.json"
    update(path, "a", 1)
    assert update(path, "b", 2, 1) == {"a": 1, "b": 2}


def test_update_on_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(store.CorruptStoreError):
        update(path, "a", 1)
    assert path.read_text(encoding="utf-8") == "{oops"


def test_update_rejects_invalid_value(tmp_path):
    path = tmp_path / "s.json"
    with pytest.raises(ValueError):
        update(path, "a", {"nested": 1})
    assert not path.exists()


def test_update_upgrades_legacy(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert update(path, "b", 2) == {"a": 1, "b": 2}
    assert store.load_snapshot(path).revision == 1


def test_merge_preserves_unrelated_entries(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"keep": 1, "change": 1})
    revision = merge(path, {"change": 2, "new": 3})
    assert revision == 2
    assert store.load(path) == {"keep": 1, "change": 2, "new": 3}


def test_merge_returns_revision(tmp_path):
    path = tmp_path / "s.json"
    assert merge(path, {"a": 1}) == 1
    assert merge(path, {"b": 2}) == 2
    assert store.load_snapshot(path).revision == 2


def test_merge_forwards_expected_revision(tmp_path):
    path = tmp_path / "s.json"
    assert merge(path, {"a": 1}) == 1
    assert merge(path, {"b": 2}, expected_revision=1) == 2
    assert merge(path, {"c": 3}, 2) == 3
    before = path.read_bytes()
    with pytest.raises(store.ConflictError):
        merge(path, {"d": 4}, expected_revision=0)
    assert path.read_bytes() == before


def test_merge_on_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(store.CorruptStoreError):
        merge(path, {"a": 1})
    assert path.read_text(encoding="utf-8") == "[]"


def test_merge_rejects_invalid_update(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        merge(path, {"b": [1, 2]})
    assert path.read_bytes() == before


def test_merge_upgrades_legacy(tmp_path):
    path = tmp_path / "s.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert merge(path, {"b": 2}) == 1
    assert store.load(path) == {"a": 1, "b": 2}
    assert store.load_snapshot(path).revision == 1
