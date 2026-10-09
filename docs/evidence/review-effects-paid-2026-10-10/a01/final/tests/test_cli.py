import json

from app.cli import main


def test_cli(capsys):
    assert main(["created", "42"]) == 0
    assert capsys.readouterr().out.strip() == "created=42"


def test_cli_with_correlation_id_text(capsys):
    assert main(["created", "42", "--correlation-id", "abc"]) == 0
    assert capsys.readouterr().out.strip() == "created=42 correlation_id=abc"


def test_cli_defaults_to_text(capsys):
    assert main(["created", "42", "--correlation-id", "abc"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "created=42 correlation_id=abc"
    assert not out.startswith("{")


def test_cli_json_output(capsys):
    assert main(["created", "42", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"name": "created", "value": "42"}


def test_cli_json_output_with_correlation_id(capsys):
    assert main(["created", "42", "--format", "json", "--correlation-id", "abc"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "name": "created",
        "value": "42",
        "correlation_id": "abc",
    }


def test_cli_json_output_with_empty_correlation_id(capsys):
    assert main(["created", "42", "--format", "json", "--correlation-id", ""]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "name": "created",
        "value": "42",
        "correlation_id": "",
    }


def test_cli_text_output_with_empty_correlation_id(capsys):
    assert main(["created", "42", "--correlation-id", ""]) == 0
    assert capsys.readouterr().out.strip() == "created=42 correlation_id="
