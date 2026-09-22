import json
from configkit.service import export_config, inspect_config


def test_export(tmp_path):
    source, dest = tmp_path / "source.json", tmp_path / "dest.json"
    source.write_text(json.dumps({"name":"demo", "host":"localhost", "port":8080}))
    before = source.read_bytes()
    assert export_config(source, dest) == inspect_config(source)
    assert inspect_config(dest)["address"] == "localhost:8080"
    assert source.read_bytes() == before
