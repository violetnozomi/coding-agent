import json
import pytest
from configkit import Config, dumps, loads
from configkit.config.errors import ConfigError
from configkit.config.migrate import migrate_file


def test_v2():
    data = {"version": 2, "service": {"name": "demo", "endpoint": {"host": "local", "port": 80}}}
    assert loads(json.dumps(data)) == Config("demo", "local", 80)
    assert json.loads(dumps(Config("demo", "local", 80)))["version"] == 2


def test_dry_run(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"name":"demo","host":"local","port":80}')
    before = path.read_bytes()
    assert migrate_file(path, dry_run=True)["version"] == 2
    assert path.read_bytes() == before
    assert migrate_file(path) == migrate_file(path)


def test_error():
    with pytest.raises(ConfigError) as caught:
        loads('{"version":2,"service":{"name":"demo","endpoint":{"host":"local","port":false}}}')
    assert caught.value.to_dict() == {"error":"invalid_config","issues":[{"path":"service.endpoint.port","code":"invalid"}]}
