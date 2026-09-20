"""Exact, currency-aware invoice pricing.

The single source of pricing truth for the repository.  All money is handled
as integer *minor units* (cents for USD/EUR, whole yen for JPY) so totals never
suffer binary float drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Iterable, Mapping

__all__ = ["Quote", "quote_order", "format_minor", "CURRENCY_EXPONENTS"]

#: Minor-unit exponent per supported currency (USD 100 cents, JPY 0 decimals).
CURRENCY_EXPONENTS = {"USD": 2, "EUR": 2, "JPY": 0}

# Wide enough that string->Decimal parsing and multiplication stay exact.
_WORKING_PRECISION = 60


@dataclass(frozen=True)
class Quote:
    """Immutable pricing result in integer minor units."""

    currency: str
    subtotal_minor: int
    discount_minor: int
    total_minor: int


def _check_currency(currency: object) -> str:
    if not isinstance(currency, str) or currency not in CURRENCY_EXPONENTS:
        raise ValueError(f"unsupported currency: {currency!r}")
    return currency


def _check_discount(discount_bps: object) -> int:
    if isinstance(discount_bps, bool) or not isinstance(discount_bps, int):
        raise ValueError(f"discount_bps must be an integer in 0..10000: {discount_bps!r}")
    if not 0 <= discount_bps <= 10000:
        raise ValueError(f"discount_bps must be in 0..10000: {discount_bps!r}")
    return discount_bps


def _parse_unit_price(unit_price: object) -> Decimal:
    if isinstance(unit_price, bool) or isinstance(unit_price, float):
        raise ValueError(f"unit_price must be a decimal string, not {unit_price!r}")
    if isinstance(unit_price, Decimal):
        price = unit_price
    elif isinstance(unit_price, str):
        try:
            price = Decimal(unit_price)
        except (InvalidOperation, ValueError):
            raise ValueError(f"invalid unit_price: {unit_price!r}") from None
    else:
        raise ValueError(f"invalid unit_price: {unit_price!r}")
    if not price.is_finite():
        raise ValueError(f"unit_price must be finite: {unit_price!r}")
    if price < 0:
        raise ValueError(f"unit_price must not be negative: {unit_price!r}")
    return price


def _check_quantity(quantity: object) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError(f"quantity must be a positive integer: {quantity!r}")
    if quantity <= 0:
        raise ValueError(f"quantity must be a positive integer: {quantity!r}")
    return quantity


def _quantum(exponent: int) -> Decimal:
    return Decimal(1).scaleb(-exponent)


def format_minor(amount_minor: int, exponent: int) -> str:
    """Render integer minor units as an exact decimal string (no float math).

    ``format_minor(1230, 2) == "12.30"`` and ``format_minor(7, 0) == "7"``.
    """

    if exponent <= 0:
        return str(amount_minor)
    sign = "-" if amount_minor < 0 else ""
    magnitude = abs(amount_minor)
    scale = 10**exponent
    return f"{sign}{magnitude // scale}.{magnitude % scale:0{exponent}d}"


def quote_order(
    lines: Iterable[Mapping[str, object]],
    *,
    currency: str = "USD",
    discount_bps: int = 0,
) -> Quote:
    """Price ``lines`` exactly and return a :class:`Quote`.

    ``lines`` is an iterable of mappings with ``unit_price`` (decimal string)
    and ``quantity`` (positive integer).  Unrelated keys are ignored and the
    caller's objects are never mutated.  An empty order is valid.
    """

    exponent = CURRENCY_EXPONENTS[_check_currency(currency)]
    discount_bps = _check_discount(discount_bps)
    quantum = _quantum(exponent)

    subtotal_minor = 0
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        for line in lines:
            unit_price = _parse_unit_price(line["unit_price"])
            quantity = _check_quantity(line["quantity"])
            # Exact price * quantity, rounded HALF_UP once, never per unit.
            line_minor = (unit_price * quantity).quantize(quantum, rounding=ROUND_HALF_UP)
            subtotal_minor += int(line_minor.scaleb(exponent))

        discount_minor = int(
            (Decimal(subtotal_minor) * discount_bps / 10000).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )

    return Quote(
        currency=currency,
        subtotal_minor=subtotal_minor,
        discount_minor=discount_minor,
        total_minor=subtotal_minor - discount_minor,
    )
