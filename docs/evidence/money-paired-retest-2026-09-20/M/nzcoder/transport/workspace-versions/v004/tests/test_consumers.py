from web import checkout
from cli import receipt
from jobs import export
from mobile import summary
from reporting import monthly


def test_consumers_use_central_quote():
    lines = [{"unit_price": "1.25", "quantity": 2}]
    assert checkout(lines) == {"currency": "USD", "total_minor": 250}
    assert receipt(lines) == "USD 2.50"
    assert export(lines) == "currency,total_minor\nUSD,250\n"
    assert summary(lines) == "2.50"
    assert monthly(lines) == 250


def test_consumers_share_keyword_only_options():
    lines = [{"unit_price": "10.00", "quantity": 1}]
    assert checkout(lines, currency="JPY", discount_bps=1000) == {
        "currency": "JPY",
        "total_minor": 9,
    }
    assert receipt(lines, currency="JPY") == "JPY 10"
    assert export(lines, currency="EUR") == "currency,total_minor\nEUR,1000\n"
    assert summary(lines, currency="EUR") == "10.00"
    assert monthly(lines, discount_bps=500) == 950


def test_legacy_api_remains_available():
    from billing.legacy import total_amount

    assert total_amount([{"unit_price": "1.25", "quantity": 2}]) == 2.5
