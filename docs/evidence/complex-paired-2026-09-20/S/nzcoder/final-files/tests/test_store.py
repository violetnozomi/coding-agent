import store
from cli import update
def test_missing(tmp_path):
    assert store.load(tmp_path / "settings.json") == {}
def test_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    store.save(path, {"name": "雪", "active": True})
    assert store.load(path) == {"name": "雪", "active": True}
def test_cli_preserves_keys(tmp_path):
    path = tmp_path / "settings.json"
    store.save(path, {"a": 1})
    assert update(path, "b", 2) == {"a": 1, "b": 2}
