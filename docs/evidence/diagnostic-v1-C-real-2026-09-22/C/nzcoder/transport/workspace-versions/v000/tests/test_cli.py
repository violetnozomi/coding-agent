import json
from configkit.cli import main


V1 = '{"name":"demo","host":"localhost","port":80}'


def test_show(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"name":"demo","host":"localhost","port":80}')
    assert main(["show", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["address"] == "localhost:80"


def test_show_accepts_v2(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"version":2,"service":{"name":"demo","endpoint":{"host":"localhost","port":80}}}')
    assert main(["show", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"name": "demo", "address": "localhost:80", "enabled": True}


def test_migrate_prints_v2_and_writes_v2(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(V1)
    assert main(["migrate", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["version"] == 2
    assert json.loads(path.read_text()) == printed


def test_migrate_dry_run_prints_v2_without_writing(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text(V1)
    before = path.read_bytes()
    assert main(["migrate", str(path), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == 2
    assert path.read_bytes() == before


def test_invalid_config_exits_2_with_error_json_and_no_write(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"name":"demo","host":"h","port":0}')
    before = path.read_bytes()
    assert main(["migrate", str(path)]) == 2
    err = json.loads(capsys.readouterr().err)
    assert err == {"error": "invalid_config", "issues": [{"path": "port", "code": "invalid"}]}
    assert path.read_bytes() == before


def test_unsupported_version_via_cli(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"version":9,"name":"demo","host":"h","port":1}')
    assert main(["show", str(path)]) == 2
    assert json.loads(capsys.readouterr().err)["issues"] == [{"path": "version", "code": "unsupported"}]
