import pytest
from configkit import Config, loads
from configkit.config.errors import ConfigError


V1 = '{"name":"demo","host":"localhost","port":8080,"enabled":true}'
V2 = '{"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":8080}},"enabled":true}'


def test_v1():
    assert loads('{"name":"demo","host":"localhost","port":8080}') == Config("demo", "localhost", 8080)


def test_bad_port():
    with pytest.raises(ValueError):
        loads('{"name":"demo","host":"localhost","port":0}')


def test_v1_and_v2_decode_identically():
    assert loads(V1) == loads(V2) == Config("demo", "localhost", 8080, True)


def test_missing_version_is_v1():
    assert loads('{"name":"demo","host":"localhost","port":8080}') == Config("demo", "localhost", 8080)


def test_enabled_defaults_to_true():
    assert loads('{"name":"demo","host":"localhost","port":8080}').enabled is True
    assert loads('{"version":2,"service":{"name":"d","endpoint":{"host":"h","port":1}}}').enabled is True


def test_unknown_fields_are_ignored():
    assert loads('{"name":"d","host":"h","port":1,"zzz":1}') == Config("d", "h", 1)
    assert loads('{"version":2,"service":{"name":"d","endpoint":{"host":"h","port":1},"extra":2},"other":{}}') == Config("d", "h", 1)


@pytest.mark.parametrize(
    "text,path,code",
    [
        ('{"version":3,"name":"demo","host":"h","port":1}', "version", "unsupported"),
        ('{"version":true,"name":"demo","host":"h","port":1}', "version", "unsupported"),
        ('{"name":"  ","host":"h","port":1}', "name", "invalid"),
        ('{"name":5,"host":"h","port":1}', "name", "invalid"),
        ('{"host":"h","port":1}', "name", "invalid"),
        ('{"name":"d","host":"","port":1}', "host", "invalid"),
        ('{"name":"d","host":"h"}', "port", "invalid"),
        ('{"name":"d","host":"h","port":65536}', "port", "invalid"),
        ('{"name":"d","host":"h","port":True}', "port", "invalid"),
        ('{"name":"d","host":"h","port":1,"enabled":"yes"}', "enabled", "invalid"),
        ('{"version":2}', "service.name", "invalid"),
        ('{"version":2,"service":{"endpoint":{"host":"h","port":1}}}', "service.name", "invalid"),
        ('{"version":2,"service":{"name":"d"}}', "service.endpoint.host", "invalid"),
        ('{"version":2,"service":{"name":"  ","endpoint":{"host":"h","port":1}}}', "service.name", "invalid"),
        ('{"version":2,"service":{"name":"d","endpoint":{"host":"h"}}}', "service.endpoint.port", "invalid"),
        ('{"version":2,"service":{"name":"d","endpoint":{"host":"h","port":0}}}', "service.endpoint.port", "invalid"),
        ('{"version":2,"service":{"name":"d","endpoint":{"host":"h","port":1}},"enabled":1}', "enabled", "invalid"),
        ('[]', "$", "invalid"),
        ('not json', "$", "invalid"),
    ],
)
def test_error_shape(text, path, code):
    with pytest.raises(ConfigError) as info:
        loads(text)
    assert isinstance(info.value, ValueError)
    assert info.value.to_dict() == {"error": "invalid_config", "issues": [{"path": path, "code": code}]}


def test_only_first_problem_is_reported():
    with pytest.raises(ConfigError) as info:
        loads('{"name":"","host":"","port":0,"enabled":"x"}')
    assert info.value.to_dict()["issues"] == [{"path": "name", "code": "invalid"}]
    with pytest.raises(ConfigError) as info:
        loads('{"name":"d","host":"","port":0,"enabled":"x"}')
    assert info.value.to_dict()["issues"] == [{"path": "host", "code": "invalid"}]
    with pytest.raises(ConfigError) as info:
        loads('{"name":"d","host":"h","port":0,"enabled":"x"}')
    assert info.value.to_dict()["issues"] == [{"path": "port", "code": "invalid"}]
