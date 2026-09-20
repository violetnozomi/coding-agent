"""Failure-path and contract tests for the v2 settings store."""

import dataclasses
import json
import os
import stat

import pytest

import store


def _write(path, text):
    path.write_bytes(text.encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# happy paths / format detection
# ---------------------------------------------------------------------------


def test_missing_file_is_revision_zero(tmp_path):
    snap = store.load_snapshot(tmp_path / "missing.json")
    assert snap.revision == 0
    assert isinstance(snap.revision, int)
    assert snap.data == {}
    assert store.load(tmp_path / "missing.json") == {}


def test_legacy_dict_is_revision_zero(tmp_path):
    path = _write(
        tmp_path / "s.json",
        '{"a": 1, "b": "x", "c": null, "d": 1.5, "e": true}',
    )
    snap = store.load_snapshot(path)
    assert snap.revision == 0
    assert snap.data == {"a": 1, "b": "x", "c": None, "d": 1.5, "e": True}


def test_dict_with_only_version_is_legacy(tmp_path):
    path = _write(tmp_path / "s.json", '{"version": 2}')
    snap = store.load_snapshot(path)
    assert snap.revision == 0
    assert snap.data == {"version": 2}


def test_partial_envelope_keys_are_legacy(tmp_path):
    path = _write(tmp_path / "s.json", '{"version": 2, "revision": 7}')
    snap = store.load_snapshot(path)
    assert snap.revision == 0
    assert snap.data == {"version": 2, "revision": 7}


def test_empty_object_is_legacy(tmp_path):
    path = _write(tmp_path / "s.json", "{}")
    snap = store.load_snapshot(path)
    assert snap.revision == 0
    assert snap.data == {}


def test_envelope_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    assert store.save(path, {"name": "雪", "count": 3}) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 2,
        "revision": 1,
        "data": {"name": "雪", "count": 3},
    }
    snap = store.load_snapshot(path)
    assert snap.revision == 1
    assert snap.data == {"name": "雪", "count": 3}
    assert store.load(path) == {"name": "雪", "count": 3}


def test_save_upgrades_legacy_and_increments(tmp_path):
    path = _write(tmp_path / "s.json", '{"a": 1}')
    assert store.save(path, {"a": 2}) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert store.load_snapshot(path).revision == 1
    assert store.load(path) == {"a": 2}
    assert store.save(path, {"a": 3}) == 2
    assert store.load_snapshot(path).revision == 2


# ---------------------------------------------------------------------------
# corrupt files are never rewritten
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "{",  # malformed JSON
        "",  # empty file
        "[1, 2]",  # non-object top level
        '"scalar"',
        "null",
        '{"a": 1, "a": 2}',  # duplicate key at top level
        '{"version": 2, "revision": 1, "revision": 2, "data": {}}',
        '{"a": {"b": 1, "b": 2}}',  # duplicate key nested
        '{"a": [1, {"x": 1, "x": 2}]}',
        '{"version": 3, "revision": 1, "data": {}}',  # unsupported version
        '{"version": "2", "revision": 1, "data": {}}',
        '{"version": true, "revision": 1, "data": {}}',
        '{"version": 2, "revision": 0, "data": {}}',  # non-positive revision
        '{"version": 2, "revision": -1, "data": {}}',
        '{"version": 2, "revision": 1.0, "data": {}}',
        '{"version": 2, "revision": true, "data": {}}',
        '{"version": 2, "revision": "1", "data": {}}',
        '{"version": 2, "revision": 1, "data": {}} ',  # trailing space ok
        '{"version": 2, "revision": 1, "data": {}, "extra": 1}',
        '{"version": 2, "revision": 1, "data": {"n": {"x": 1}}}',
        '{"version": 2, "revision": 1, "data": [1]}',
        '{"version": 2, "revision": 1, "data": {"n": NaN}}',
        '{"version": 2, "revision": 1, "data": {"n": Infinity}}',
        '{"nested": [1, 2]}',  # legacy data must be flat
        '{"a": NaN}',
        '{"a": Infinity}',
    ],
)
def test_corrupt_files_raise_and_are_not_rewritten(tmp_path, text):
    if text.endswith("trailing space ok"):  # pragma: no cover - guard
        pytest.skip("marker")
    path = _write(tmp_path / "s.json", text)
    before = path.read_bytes()
    with pytest.raises(store.CorruptStoreError):
        store.load(path)
    with pytest.raises(store.CorruptStoreError):
        store.load_snapshot(path)
    with pytest.raises(store.CorruptStoreError):
        store.save(path, {"ok": 1})
    assert path.read_bytes() == before


def test_trailing_whitespace_is_accepted(tmp_path):
    path = _write(tmp_path / "s.json", '{"a": 1}\n')
    assert store.load(path) == {"a": 1}


def test_invalid_utf8_is_corrupt(tmp_path):
    path = tmp_path / "s.json"
    path.write_bytes(b"\xff\xfe\x00")
    before = path.read_bytes()
    with pytest.raises(store.CorruptStoreError):
        store.load(path)
    with pytest.raises(store.CorruptStoreError):
        store.save(path, {"a": 1})
    assert path.read_bytes() == before


def test_error_hierarchy():
    assert issubclass(store.CorruptStoreError, store.StoreError)
    assert issubclass(store.ConflictError, store.StoreError)
    assert issubclass(store.StoreError, Exception)


# ---------------------------------------------------------------------------
# data validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        "not-a-dict",
        [1, 2],
        {1: "x"},
        {None: "x"},
        {"a": {"b": 1}},
        {"a": [1]},
        {"a": (1,)},
        {"a": {1, 2}},
        {"a": float("nan")},
        {"a": float("inf")},
        {"a": float("-inf")},
    ],
)
def test_save_rejects_invalid_data(tmp_path, data):
    path = tmp_path / "s.json"
    with pytest.raises(ValueError):
        store.save(path, data)
    assert not path.exists()


def test_save_accepts_scalars(tmp_path):
    path = tmp_path / "s.json"
    data = {"s": "雪", "i": 7, "f": 1.25, "b": False, "n": None}
    assert store.save(path, data) == 1
    assert store.load(path) == data


def test_save_invalid_data_leaves_existing_file(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        store.save(path, {"b": {"nested": 1}})
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# expected_revision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False, -1, 1.0, "1", object()])
def test_save_rejects_invalid_expected_revision(tmp_path, value):
    path = tmp_path / "s.json"
    with pytest.raises(ValueError):
        store.save(path, {"a": 1}, expected_revision=value)
    assert not path.exists()


def test_expected_revision_match_succeeds(tmp_path):
    path = tmp_path / "s.json"
    assert store.save(path, {"a": 1}, expected_revision=0) == 1
    assert store.save(path, {"a": 2}, expected_revision=1) == 2


def test_conflict_raises_without_touching_bytes(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    store.save(path, {"a": 2})
    before = path.read_bytes()
    with pytest.raises(store.ConflictError):
        store.save(path, {"a": 3}, expected_revision=1)
    assert path.read_bytes() == before
    assert store.load_snapshot(path).revision == 2


def test_conflict_on_missing_file(tmp_path):
    path = tmp_path / "s.json"
    with pytest.raises(store.ConflictError):
        store.save(path, {"a": 1}, expected_revision=1)
    assert not path.exists()


def test_conflict_on_corrupt_file_reports_corruption(tmp_path):
    path = _write(tmp_path / "s.json", "{oops")
    with pytest.raises(store.CorruptStoreError):
        store.save(path, {"a": 1}, expected_revision=0)


# ---------------------------------------------------------------------------
# atomic write / failure injection
# ---------------------------------------------------------------------------


def test_replace_failure_preserves_bytes_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    before = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        store.save(path, {"a": 2})
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_replace_failure_keeps_destination_absent(tmp_path, monkeypatch):
    path = tmp_path / "s.json"

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        store.save(path, {"a": 1})
    monkeypatch.undo()
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_fsync_failure_preserves_old_bytes(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    before = path.read_bytes()

    def boom(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="fsync failed"):
        store.save(path, {"a": 2})
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


def test_no_sidecar_files_after_success(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    store.save(path, {"a": 2})
    assert [p.name for p in tmp_path.iterdir()] == ["s.json"]


@pytest.mark.skipif(os.name != "posix", reason="posix permission bits")
def test_save_preserves_permission_bits(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    os.chmod(path, 0o640)
    store.save(path, {"a": 2})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name != "posix", reason="posix permission bits")
def test_save_preserves_read_only_bits(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    os.chmod(path, 0o444)
    assert store.save(path, {"a": 2}) == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o444


# ---------------------------------------------------------------------------
# independence / frozen snapshot
# ---------------------------------------------------------------------------


def test_loaded_data_is_independent(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    data = store.load(path)
    data["a"] = 99
    data["b"] = 2
    assert store.load(path) == {"a": 1}


def test_snapshot_data_is_independent(tmp_path):
    path = tmp_path / "s.json"
    store.save(path, {"a": 1})
    snap = store.load_snapshot(path)
    snap.data["a"] = 99
    assert store.load(path) == {"a": 1}


def test_save_does_not_mutate_caller_data(tmp_path):
    path = tmp_path / "s.json"
    data = {"a": 1}
    store.save(path, data)
    data["b"] = 2
    assert store.load(path) == {"a": 1}


def test_snapshot_is_frozen(tmp_path):
    snap = store.load_snapshot(tmp_path / "missing.json")
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.revision = 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.data = {}
