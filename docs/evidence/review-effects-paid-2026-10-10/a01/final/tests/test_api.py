from app.api import handle
from app.events import Event


def test_legacy_shape():
    assert handle("created", "42") == {"name": "created", "value": "42"}


def test_handle_accepts_optional_correlation_id():
    assert handle("created", "42", correlation_id="abc") == {
        "name": "created",
        "value": "42",
        "correlation_id": "abc",
    }


def test_handle_accepts_correlation_id_keyword():
    assert handle("created", "42", correlation_id="abc")["correlation_id"] == "abc"


def test_handle_none_correlation_id_is_absent():
    assert handle("created", "42", correlation_id=None) == {
        "name": "created",
        "value": "42",
    }


def test_handle_empty_correlation_id_is_present():
    assert handle("created", "42", correlation_id="") == {
        "name": "created",
        "value": "42",
        "correlation_id": "",
    }


def test_event_accepts_optional_correlation_id():
    assert Event("created", "42").correlation_id is None
    assert Event("created", "42", correlation_id="abc").correlation_id == "abc"
    assert Event("created", "42", "abc").correlation_id == "abc"
