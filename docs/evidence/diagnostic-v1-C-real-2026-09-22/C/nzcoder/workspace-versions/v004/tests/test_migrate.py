import json
import pytest
from configkit.config.errors import ConfigError
from configkit.config.migrate import migrate_file

V1 = '{"name":"demo","host":"localhost","port":8080,"enabled":true}'


def test_migrate_writes_v2_and_returns_dict(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(V1)
    result = migrate_file(str(path))
    assert result == {
        "version": 2,
        "service": {"name": "demo", "endpoint": {"host": "localhost", "port": 8080}},
        "enabled": True,
    }
    assert json.loads(path.read_text()) == result


def test_repeated_migration_is_semantically_stable(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(V1)
    first = migrate_file(str(path))
    second = migrate_file(str(path))
    assert first == second
    assert json.loads(path.read_text()) == second


def test_dry_run_leaves_source_bytes_unchanged(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(V1)
    before = path.read_bytes()
    result = migrate_file(str(path), dry_run=True)
    assert result["version"] == 2
    assert path.read_bytes() == before


def test_migrate_accepts_v2_input(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"version":2,"service":{"name":"d","endpoint":{"host":"h","port":1}}}')
    assert migrate_file(str(path)) == {
        "version": 2,
        "service": {"name": "d", "endpoint": {"host": "h", "port": 1}},
        "enabled": True,
    }


def test_invalid_config_does_not_touch_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"name":"demo","host":"h","port":0}')
    before = path.read_bytes()
    with pytest.raises(ConfigError):
        migrate_file(str(path))
    assert path.read_bytes() == before


def test_invalid_config_does_not_create_destination(tmp_path):
    source, dest = tmp_path / "source.json", tmp_path / "dest.json"
    source.write_text('not json')
    with pytest.raises(ConfigError):
        migrate_file(str(dest))
    assert not dest.exists()
    assert source.read_text() == "not json"
