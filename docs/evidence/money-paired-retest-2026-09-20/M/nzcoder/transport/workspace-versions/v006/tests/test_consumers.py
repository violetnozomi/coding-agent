from cli import receipt
from jobs import export
from mobile import summary
from reporting import monthly
from web import checkout

# Chosen so that float math in the legacy code was exact: pins byte-for-byte
# the pre-migration consumer output formats that the migration must preserve.
LINES = [{"unit_price": "1.25", "quantity": 2}]


def test_legacy_consumers():
    assert checkout(LINES) == {"total": 2.5}
    assert receipt(LINES) == "USD 2.50"
    assert export(LINES) == "total\n2.50\n"
    assert summary(LINES) == "2.50"
    assert monthly(LINES) == 250


def test_consumers_preserve_formats_for_empty_and_jpy():
    assert checkout([]) == {"total": 0.0}
    assert receipt([]) == "USD 0.00"
    assert export([]) == "total\n0.00\n"
    assert summary([]) == "0.00"
    assert monthly([]) == 0

    jpy = [{"unit_price": "15", "quantity": 2}]
    assert checkout(jpy, currency="JPY") == {"total": 30.0}
    assert receipt(jpy, currency="JPY") == "JPY 30"
    assert export(jpy, currency="JPY") == "total\n30\n"
    assert summary(jpy, currency="JPY") == "30"
    assert monthly(jpy, currency="JPY") == 30


def test_consumers_share_keyword_only_options():
    assert checkout(LINES, discount_bps=500) == {"total": 1.25}
    assert receipt(LINES, discount_bps=500) == "USD 1.25"
    assert export(LINES, discount_bps=500) == "total\n1.25\n"
    assert summary(LINES, discount_bps=500) == "1.25"
    assert monthly(LINES, discount_bps=500) == 125


def test_consumers_use_central_quote_pricing():
    from billing.quote import quote_order

    # 0.005 * 3 == 0.015 -> 0.02 HALF_UP; float math would produce 0.03.
    lines = [{"unit_price": "0.005", "quantity": 3}]
    quote = quote_order(lines)
    assert quote.total_minor == 2
    assert checkout(lines) == {"total": 0.02}
    assert receipt(lines) == "USD 0.02"
    assert export(lines) == "total\n0.02\n"
    assert summary(lines) == "0.02"
    assert monthly(lines) == 2


def test_legacy_api_remains_available_and_unused_internally():
    from billing.legacy import total_amount

    assert total_amount([{"unit_price": "1.25", "quantity": 2}]) == 2.5

    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    for name in ("web.py", "cli.py", "jobs.py", "mobile.py", "reporting.py"):
        source = (root / name).read_text()
        assert "legacy" not in source, name
        assert not re.search(r"total_amount", source), name
