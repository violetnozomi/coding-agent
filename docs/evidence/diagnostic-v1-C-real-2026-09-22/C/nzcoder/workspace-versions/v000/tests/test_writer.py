from configkit import Config, dumps, loads


def test_roundtrip():
    config = Config("demo", "localhost", 1234, False)
    assert loads(dumps(config)) == config
