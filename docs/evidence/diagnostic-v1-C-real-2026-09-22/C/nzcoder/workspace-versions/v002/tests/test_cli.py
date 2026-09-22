import json
from configkit.cli import main


def test_show(tmp_path, capsys):
    path = tmp_path / "config.json"
    path.write_text('{"name":"demo","host":"localhost","port":80}')
    assert main(["show", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["address"] == "localhost:80"
