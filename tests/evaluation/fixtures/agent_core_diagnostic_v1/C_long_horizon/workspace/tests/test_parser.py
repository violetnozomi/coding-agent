import pytest
from configkit import Config, loads


def test_v1():
    assert loads('{"name":"demo","host":"localhost","port":8080}') == Config("demo", "localhost", 8080)


def test_bad_port():
    with pytest.raises(ValueError):
        loads('{"name":"demo","host":"localhost","port":0}')
