"""Tests for the migrated pricing consumers.

These keep the original consumer round-trip meaningful for the new
integer-minor-unit contract. ``1.25 * 2 == 2.50`` becomes 250 minor units.
"""

from web import checkout
from cli import receipt
from jobs import export
from mobile import summary
from reporting import monthly


def test_consumers_use_quote_order():
    lines = [{"unit_price": "1.25", "quantity": 2}]
    assert checkout(lines) == {"currency": "USD", "total_minor": 250}
    assert receipt(lines) == "USD 2.50"
    assert export(lines) == "currency,total_minor\nUSD,250\n"
    assert summary(lines) == "2.50"
    assert monthly(lines) == 250


def test_consumers_ignore_unrelated_line_keys_and_do_not_mutate():
    lines = [
        {"unit_price": "1.25", "quantity": 2, "sku": "A", "note": "ignore me"},
        {"unit_price": "0.05", "quantity": 3, "sku": "B"},
    ]
    snapshot = [dict(line) for line in lines]
    assert checkout(lines) == {"currency": "USD", "total_minor": 265}
    assert receipt(lines) == "USD 2.65"
    assert export(lines) == "currency,total_minor\nUSD,265\n"
    assert summary(lines) == "2.65"
    assert monthly(lines) == 265
    assert lines == snapshot


def test_consumers_share_currency_and_discount_defaults():
    assert checkout([]) == {"currency": "USD", "total_minor": 0}
    assert receipt([]) == "USD 0.00"
    assert export([]) == "currency,total_minor\nUSD,0\n"
    assert summary([]) == "0.00"
    assert monthly([]) == 0

    jpy = [{"unit_price": "1200", "quantity": 2}]
    assert checkout(jpy, currency="JPY") == {"currency": "JPY", "total_minor": 2400}
    assert receipt(jpy, currency="JPY") == "JPY 2400"
    assert export(jpy, currency="JPY") == "currency,total_minor\nJPY,2400\n"
    assert summary(jpy, currency="JPY") == "2400"
    assert monthly(jpy, currency="JPY") == 2400

    discounted = [{"unit_price": "10.00", "quantity": 1}]
    assert checkout(discounted, discount_bps=1000) == {"currency": "USD", "total_minor": 900}
    assert receipt(discounted, discount_bps=1000) == "USD 9.00"
    assert export(discounted, discount_bps=1000) == "currency,total_minor\nUSD,900\n"
    assert summary(discounted, discount_bps=1000) == "9.00"
    assert monthly(discounted, discount_bps=1000) == 900


def test_consumers_reject_invalid_input_via_quote():
    import pytest

    lines = [{"unit_price": "1.25", "quantity": 2}]
    for consumer in (checkout, receipt, export, summary, monthly):
        with pytest.raises(ValueError):
            consumer(lines, currency="GBP")
        with pytest.raises(ValueError):
            consumer(lines, discount_bps=True)
        with pytest.raises(ValueError):
            consumer([{"unit_price": "1.25", "quantity": 0}])
