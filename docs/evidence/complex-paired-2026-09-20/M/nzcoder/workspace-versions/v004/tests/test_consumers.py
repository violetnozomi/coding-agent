from web import checkout
from cli import receipt
from jobs import export
from mobile import summary
from reporting import monthly
def test_legacy_consumers():
    lines = [{"unit_price": "1.25", "quantity": 2}]
    assert checkout(lines) == {"total": 2.5}
    assert receipt(lines) == "USD 2.50"
    assert export(lines) == "total\n2.50\n"
    assert summary(lines) == "2.50"
    assert monthly(lines) == 2.5
