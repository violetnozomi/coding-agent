import dataclasses
from decimal import Decimal

import pytest

from billing.quote import CURRENCY_EXPONENTS, Quote, quote_order


def test_quote_is_frozen_dataclass():
    assert dataclasses.is_dataclass(Quote)
    assert Quote.__dataclass_params__.frozen
    quote = quote_order([{"unit_price": "1.00", "quantity": 1}])
    with pytest.raises(dataclasses.FrozenInstanceError):
        quote.total_minor = 0


def test_empty_order_is_valid():
    assert quote_order([]) == Quote("USD", 0, 0, 0)
    assert quote_order([], currency="JPY") == Quote("JPY", 0, 0, 0)


def test_currency_exponents():
    assert CURRENCY_EXPONENTS == {"USD": 2, "EUR": 2, "JPY": 0}


def test_price_is_not_rounded_per_unit():
    # 0.005 * 3 == 0.015 exactly -> 0.02; per-unit rounding would give 0.03.
    quote = quote_order([{"unit_price": "0.005", "quantity": 3}])
    assert quote.subtotal_minor == 2
    assert quote.total_minor == 2


@pytest.mark.parametrize(
    "unit_price,quantity,expected_minor",
    [
        ("0.005", 1, 1),  # half up at the minor unit
        ("0.125", 1, 13),
        ("0.125", 4, 50),
        ("2.675", 1, 268),
        ("0", 5, 0),
        ("1.005", 1, 101),  # float math would give 100
        ("1.005", 100, 10050),
    ],
)
def test_half_up_rounding_per_line(unit_price, quantity, expected_minor):
    quote = quote_order([{"unit_price": unit_price, "quantity": quantity}])
    assert quote.subtotal_minor == expected_minor


def test_jpy_rounds_to_whole_yen():
    quote = quote_order([{"unit_price": "1.5", "quantity": 1}], currency="JPY")
    assert quote.subtotal_minor == 2
    assert quote.total_minor == 2


def test_lines_are_summed_after_rounding():
    lines = [
        {"unit_price": "1.005", "quantity": 1},  # 1.01 -> 101
        {"unit_price": "1.005", "quantity": 1},  # 1.01 -> 101
    ]
    assert quote_order(lines).subtotal_minor == 202


def test_discount_is_rounded_half_up():
    quote = quote_order([{"unit_price": "50.00", "quantity": 1}], discount_bps=1)
    assert quote.subtotal_minor == 5000
    assert quote.discount_minor == 1  # 0.5 rounds up
    assert quote.total_minor == 4999


def test_discount_full_percentage():
    quote = quote_order([{"unit_price": "12.30", "quantity": 1}], discount_bps=1000)
    assert Quote(quote.currency, quote.subtotal_minor, quote.discount_minor, quote.total_minor) == Quote(
        "USD", 1230, 123, 1107
    )


def test_decimal_unit_prices_accepted():
    quote = quote_order([{"unit_price": Decimal("1.25"), "quantity": 2}])
    assert quote.total_minor == 250


def test_unrelated_keys_ignored_and_input_not_mutated():
    line = {"unit_price": "1.25", "quantity": 2, "sku": "abc", "note": "x"}
    before = dict(line)
    quote = quote_order([line])
    assert quote.total_minor == 250
    assert line == before


def test_consumes_any_iterable():
    lines = ({"unit_price": "1.00", "quantity": 1} for _ in range(3))
    assert quote_order(lines).total_minor == 300


@pytest.mark.parametrize("currency", ["GBP", "usd", "", None, 1])
def test_unsupported_currency_rejected_even_when_empty(currency):
    with pytest.raises(ValueError):
        quote_order([], currency=currency)


@pytest.mark.parametrize("discount_bps", [-1, 10001, True, False, 1.0, "5", None])
def test_invalid_discount_rejected_even_when_empty(discount_bps):
    with pytest.raises(ValueError):
        quote_order([], discount_bps=discount_bps)


@pytest.mark.parametrize("unit_price", ["-1", "-0.01", "abc", "", "nan", "inf", "-inf", None, 1.25, True, [1]])
def test_invalid_unit_price_rejected(unit_price):
    with pytest.raises(ValueError):
        quote_order([{"unit_price": unit_price, "quantity": 1}])


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, False, "2", None])
def test_invalid_quantity_rejected(quantity):
    with pytest.raises(ValueError):
        quote_order([{"unit_price": "1.00", "quantity": quantity}])


def test_missing_keys_raise():
    with pytest.raises(KeyError):
        quote_order([{"quantity": 1}])
    with pytest.raises(KeyError):
        quote_order([{"unit_price": "1.00"}])
