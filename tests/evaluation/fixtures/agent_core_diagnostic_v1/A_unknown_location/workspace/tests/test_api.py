from app.api import handle


def test_legacy_shape():
    assert handle("created", "42") == {"name": "created", "value": "42"}
