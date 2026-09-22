from configkit import Config, dumps, loads
import json


def test_roundtrip():
    config = Config("demo", "localhost", 1234, False)
    assert loads(dumps(config)) == config


def test_dumps_emits_v2_only():
    data = json.loads(dumps(Config("demo", "localhost", 1234, False)))
    assert data == {
        "version": 2,
        "service": {"name": "demo", "endpoint": {"host": "localhost", "port": 1234}},
        "enabled": False,
    }
    assert "name" not in data and "host" not in data and "port" not in data


def test_v1_input_is_read_but_never_written_back_as_v1():
    config = loads('{"name":"demo","host":"localhost","port":80}')
    data = json.loads(dumps(config))
    assert data["version"] == 2
    assert "name" not in data
