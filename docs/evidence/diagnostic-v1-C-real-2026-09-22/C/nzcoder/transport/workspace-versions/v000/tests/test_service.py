import json
from configkit.service import export_config, inspect_config


def test_export(tmp_path):
    source, dest = tmp_path / "source.json", tmp_path / "dest.json"
    source.write_text(json.dumps({"name":"demo", "host":"localhost", "port":8080}))
    before = source.read_bytes()
    assert export_config(source, dest) == inspect_config(source)
    assert inspect_config(dest)["address"] == "localhost:8080"
    assert source.read_bytes() == before


def test_export_writes_v2(tmp_path):
    source, dest = tmp_path / "source.json", tmp_path / "dest.json"
    source.write_text('{"name":"demo","host":"localhost","port":8080}')
    before = source.read_bytes()
    summary = export_config(source, dest)
    assert summary == {"name": "demo", "address": "localhost:8080", "enabled": True}
    data = json.loads(dest.read_text())
    assert data["version"] == 2
    assert data["service"]["name"] == "demo"
    assert data["service"]["endpoint"] == {"host": "localhost", "port": 8080}
    assert "name" not in data and "host" not in data and "port" not in data
    assert source.read_bytes() == before


def test_export_accepts_v2_source(tmp_path):
    source, dest = tmp_path / "source.json", tmp_path / "dest.json"
    source.write_text('{"version":2,"service":{"name":"demo","endpoint":{"host":"h","port":9}}}')
    before = source.read_bytes()
    assert export_config(source, dest) == {"name": "demo", "address": "h:9", "enabled": True}
    assert inspect_config(dest)["name"] == "demo"
    assert source.read_bytes() == before


def test_inspect_v2_and_v1_agree(tmp_path):
    v1, v2 = tmp_path / "v1.json", tmp_path / "v2.json"
    v1.write_text('{"name":"demo","host":"localhost","port":8080}')
    v2.write_text('{"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":8080}}}')
    assert inspect_config(v1) == inspect_config(v2)
