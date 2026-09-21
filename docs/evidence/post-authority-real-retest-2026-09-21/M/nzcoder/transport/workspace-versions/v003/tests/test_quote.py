"""Contract tests for the exact-decimal pricing core."""

from decimal import Decimal

import pytest

from billing.legacy import total_amount
from billing.quote import Quote, currency_exponent, format_minor, quote_order


def test_quote_is_frozen_value_object():
    quote = quote_order([{"unit_price": "1.25", "quantity": 2}])
    assert quote == Quote(currency="USD", subtotal_minor=250, discount_minor=0, total_minor=250)
    with pytest.raises(Exception):
        quote.total_minor = 0  # type: ignore[misc]


def test_rounding_is_half_up_after_multiplying_exact_price():
    # 0.005 * 1 rounds HALF_UP to 0.01 (1 minor unit); float math gives 0.0.
    assert quote_order([{"unit_price": "0.005", "quantity": 1}]).total_minor == 1
    # Rounding happens after multiplying, not before: 0.005 * 3 == 0.015 -> 0.02
    quote = quote_order([{"unit_price": "0.005", "quantity": 3}])
    assert quote.subtotal_minor == 2
    # Half-up on .5 boundaries
    assert quote_order([{"unit_price": "2.345", "quantity": 1}]).total_minor == 235


def test_lines_are_summed_after_each_line_is_rounded():
    lines = [
        {"unit_price": "0.005", "quantity": 1},
        {"unit_price": "0.005", "quantity": 1},
    ]
    # Each line rounds to 0.01, so the sum is 0.02 rather than 0.01.
    assert quote_order(lines).subtotal_minor == 2


def test_currency_exponents():
    assert currency_exponent("USD") == 2
    assert currency_exponent("EUR") == 2
    assert currency_exponent("JPY") == 0
    assert format_minor(1230, "USD") == "12.30"
    assert format_minor(1230, "EUR") == "12.30"
    assert format_minor(1230, "JPY") == "1230"
    assert format_minor(-5, "USD") == "-0.05"


def test_jpy_has_no_minor_units():
    quote = quote_order([{"unit_price": "1200.4", "quantity": 1}], currency="JPY")
    assert quote.subtotal_minor == 1200
    assert quote_order([{"unit_price": "1200.5", "quantity": 1}], currency="JPY").total_minor == 1201


def test_discount_is_rounded_half_up():
    quote = quote_order([{"unit_price": "10.00", "quantity": 1}], discount_bps=333)
    # 1000 minor * 333 / 10000 = 33.3 -> 33
    assert quote.discount_minor == 33
    assert quote.total_minor == 967
    # 1000 * 1250/10000 = 125 exactly
    exact = quote_order([{"unit_price": "10.00", "quantity": 1}], discount_bps=1250)
    assert exact.discount_minor == 125
    assert exact.total_minor == 875
    # 1 * 5000/10000 = 0.5 -> HALF_UP -> 1
    half = quote_order([{"unit_price": "0.01", "quantity": 1}], discount_bps=5000)
    assert half.discount_minor == 1
    assert half.total_minor == 0


def test_full_discount_and_zero_discount():
    assert quote_order([{"unit_price": "5.00", "quantity": 2}], discount_bps=10000).total_minor == 0
    assert quote_order([{"unit_price": "5.00", "quantity": 2}], discount_bps=0).discount_minor == 0


def test_empty_orders_are_valid_but_still_validate():
    quote = quote_order([])
    assert quote == Quote("USD", 0, 0, 0)
    assert quote_order(iter([]), currency="JPY") == Quote("JPY", 0, 0, 0)
    with pytest.raises(ValueError):
        quote_order([], currency="GBP")
    with pytest.raises(ValueError):
        quote_order([], currency="usd")
    with pytest.raises(ValueError):
        quote_order([], discount_bps=10001)
    with pytest.raises(ValueError):
        quote_order([], discount_bps=-1)
    with pytest.raises(ValueError):
        quote_order([], discount_bps=True)


def test_caller_input_is_not_mutated_and_unrelated_keys_ignored():
    line = {"unit_price": "1.25", "quantity": 2, "sku": "A"}
    lines = [line]
    quote_order(lines)
    assert line == {"unit_price": "1.25", "quantity": 2, "sku": "A"}
    assert lines == [line]


def test_invalid_prices_are_rejected():
    for bad in ["", "abc", "1.2.3", "NaN", "Infinity", "-Infinity", "-1.00", "-0.01"]:
        with pytest.raises(ValueError):
            quote_order([{"unit_price": bad, "quantity": 1}])
    for bad in [1.25, 0.0, True, None, [], {}]:
        with pytest.raises(ValueError):
            quote_order([{"unit_price": bad, "quantity": 1}])  # type: ignore[list-item]
    # Decimal input is accepted when it is finite and non-negative.
    assert quote_order([{"unit_price": Decimal("1.10"), "quantity": 3}]).total_minor == 330


def test_invalid_quantities_are_rejected():
    for bad in [0, -1, True, "2", 1.0, None]:
        with pytest.raises(ValueError):
            quote_order([{"unit_price": "1.00", "quantity": bad}])  # type: ignore[list-item]


def test_malformed_lines_are_rejected():
    with pytest.raises(ValueError):
        quote_order([{"quantity": 1}])
    with pytest.raises(ValueError):
        quote_order([{"unit_price": "1.00"}])
    with pytest.raises(ValueError):
        quote_order(["not-a-mapping"])  # type: ignore[list-item]


def test_legacy_total_amount_remains_callable():
    # The compatibility boundary is preserved for external callers only.
    lines = [{"unit_price": "1.25", "quantity": 2}]
    assert total_amount(lines) == 2.5
