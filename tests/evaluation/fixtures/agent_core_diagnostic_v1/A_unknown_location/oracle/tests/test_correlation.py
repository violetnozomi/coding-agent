import json

from app import handle
from app.adapters.json_sink import write as json_write
from app.adapters.text_sink import write as text_write
from app.cli import main
from app.events import Event
from app.service import process


def test_full_chain():
    for value in ("id-1", "", "客户"):
        record = handle("created", "42", correlation_id=value)
        assert record == process(Event("created", "42", value))
        assert json.loads(json_write(record))["correlation_id"] == value
        assert text_write(record) == "created=42 correlation_id=" + value


def test_cli_id(capsys):
    main(["created", "42", "--correlation-id", "abc", "--format", "json"])
    assert json.loads(capsys.readouterr().out)["correlation_id"] == "abc"
