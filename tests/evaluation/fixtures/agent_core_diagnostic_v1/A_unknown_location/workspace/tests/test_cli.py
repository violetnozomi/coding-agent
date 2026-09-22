from app.cli import main


def test_cli(capsys):
    assert main(["created", "42"]) == 0
    assert capsys.readouterr().out.strip() == "created=42"
