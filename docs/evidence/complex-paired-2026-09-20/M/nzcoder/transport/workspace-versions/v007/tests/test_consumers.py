"""Consumer contract tests for the migration to :func:`billing.quote.quote_order`."""

import pytest

from billing.legacy import total_amount
from billing.quote import Quote, quote_order
from cli import receipt
from jobs import export
from mobile import summary
from reporting import monthly
from web import checkout

LINES = [{"unit_price": "1.25", "quantity": 2}]


def test_legacy_total_amount_remains_callable():
    assert total_amount(LINES) == 2.5


def test_consumers_share_the_documented_output_formats():
    assert checkout(LINES) == {"currency": "USD", "total_minor": 250}
    assert receipt(LINES) == "USD 2.50"
    assert export(LINES) == "currency,total_minor\nUSD,250\n"
    assert summary(LINES) == "2.50"
    assert monthly(LINES) == 250


def test_consumers_accept_keyword_only_currency_and_discount():
    # JPY has no minor units: 1.25 * 2 = 2.5 -> HALF_UP -> 3.
    assert checkout(LINES, currency="JPY") == {"currency": "JPY", "total_minor": 3}
    assert receipt(LINES, currency="JPY") == "JPY 3"
    assert export(LINES, currency="EUR", discount_bps=5000) == (
        "currency,total_minor\nEUR,125\n"
    )
    # 250 * 25% = 62.5 -> HALF_UP -> 63; 250 - 63 = 187 -> "1.87".
    assert summary(LINES, currency="EUR", discount_bps=2500) == "1.87"
    assert monthly(LINES, discount_bps=10000) == 0


def test_consumers_reject_invalid_input_via_quote_order():
    for consumer in (checkout, receipt, export, summary, monthly):
        with pytest.raises(ValueError):
            consumer(LINES, currency="GBP")
        with pytest.raises(ValueError):
            consumer([{"unit_price": "1.00", "quantity": 0}])


def test_quote_is_immutable_and_exact():
    quote = quote_order([{"unit_price": "0.005", "quantity": 1}])
    assert quote == Quote(
        currency="USD", subtotal_minor=1, discount_minor=0, total_minor=1
    )
    with pytest.raises(Exception):
        quote.total_minor = 5


def test_rounding_happens_after_multiplying_not_per_unit():
    # 0.005 * 3 = 0.015 -> HALF_UP to 0.02 (rounding per unit would give 0.03).
    assert quote_order([{"unit_price": "0.005", "quantity": 3}]).subtotal_minor == 2


def test_discount_is_rounded_half_up_on_the_order_total():
    quote = quote_order([{"unit_price": "0.05", "quantity": 1}], discount_bps=5000)
    assert quote.subtotal_minor == 5
    assert quote.discount_minor == 3
    assert quote.total_minor == 2


def test_jpy_has_no_minor_units():
    quote = quote_order([{"unit_price": "12.4", "quantity": 1}], currency="JPY")
    assert quote.total_minor == 12
    assert quote.total_display == "12"
    assert quote.formatted_total == "JPY 12"


def test_empty_orders_are_valid_but_currency_and_discount_are_still_validated():
    assert quote_order([], currency="EUR", discount_bps=100).total_minor == 0
    with pytest.raises(ValueError):
        quote_order([], currency="GBP")
    with pytest.raises(ValueError):
        quote_order([], discount_bps=10001)


@pytest.mark.parametrize(
    "line",
    [
        {"unit_price": "-1.00", "quantity": 1},
        {"unit_price": "abc", "quantity": 1},
        {"unit_price": "nan", "quantity": 1},
        {"unit_price": "inf", "quantity": 1},
        {"unit_price": "1.00", "quantity": True},
        {"unit_price": "1.00", "quantity": 1.5},
        {"unit_price": "1.00", "quantity": -1},
        {"unit_price": 1.0, "quantity": 1},
        {"unit_price": True, "quantity": 1},
    ],
)
def test_invalid_prices_and_quantities_are_rejected(line):
    with pytest.raises(ValueError):
        quote_order([line])


def test_invalid_discount_types_are_rejected():
    for discount in (True, False, 1.0, "100"):
        with pytest.raises(ValueError):
            quote_order(LINES, discount_bps=discount)


def test_unrelated_line_keys_are_ignored_and_input_is_not_mutated():
    lines = [{"unit_price": "1.25", "quantity": 2, "sku": "A1", "note": "keep me"}]
    snapshot = [dict(lines[0])]
    assert quote_order(lines).total_minor == 250
    assert lines == snapshot
