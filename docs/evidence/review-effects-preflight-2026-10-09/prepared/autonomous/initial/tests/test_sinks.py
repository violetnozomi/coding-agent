from app.api import handle
from app.adapters.json_sink import write as json_write
from app.adapters.text_sink import write as text_write


def test_sinks():
    record = handle("created", "42")
    assert '"name": "created"' in json_write(record)
    assert text_write(record) == "created=42"
