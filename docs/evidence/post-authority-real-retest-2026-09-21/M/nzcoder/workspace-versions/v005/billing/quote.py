"""Exact-decimal invoice pricing.

This module is the single source of pricing truth for the repository. It
replaces the float based ``billing.legacy.total_amount`` helper with a
frozen :class:`Quote` value object produced by :func:`quote_order`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = ["Quote", "quote_order", "format_minor", "currency_exponent"]

# Currency code -> number of minor-unit decimal places.
_CURRENCY_EXPONENTS = {
    "USD": 2,
    "EUR": 2,
    "JPY": 0,
}

_BPS_DENOMINATOR = Decimal(10000)


@dataclass(frozen=True)
class Quote:
    """Immutable result of pricing an order.

    All monetary fields are integer minor units (for example cents), so no
    binary floating point value ever participates in the totals.
    """

    currency: str
    subtotal_minor: int
    discount_minor: int
    total_minor: int


def currency_exponent(currency: str) -> int:
    """Return the number of minor-unit decimal places for ``currency``."""

    if not isinstance(currency, str):
        raise ValueError(f"unsupported currency: {currency!r}")
    try:
        return _CURRENCY_EXPONENTS[currency]
    except KeyError:
        raise ValueError(f"unsupported currency: {currency!r}") from None


def _validate_discount_bps(discount_bps: int) -> int:
    if isinstance(discount_bps, bool) or not isinstance(discount_bps, int):
        raise ValueError(f"discount_bps must be an integer: {discount_bps!r}")
    if discount_bps < 0 or discount_bps > 10000:
        raise ValueError(f"discount_bps out of range 0..10000: {discount_bps!r}")
    return discount_bps


def _parse_unit_price(raw: object) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float):
        # Floats are deliberately rejected: they cannot represent decimal
        # money exactly, which is the bug this migration removes.
        raise ValueError(f"unit_price must be a decimal string, not {type(raw).__name__}")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, ValueError):
            raise ValueError(f"invalid unit_price: {raw!r}") from None
    else:
        raise ValueError(f"unit_price must be a decimal string, got {type(raw).__name__}")

    if not value.is_finite():
        raise ValueError(f"unit_price must be finite: {raw!r}")
    if value < 0:
        raise ValueError(f"unit_price must not be negative: {raw!r}")
    return value


def _parse_quantity(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"quantity must be a positive integer: {raw!r}")
    if raw <= 0:
        raise ValueError(f"quantity must be a positive integer: {raw!r}")
    return raw


def _round_half_up(value: Decimal) -> int:
    """Round ``value`` HALF_UP to the nearest integer."""

    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _round_minor(value: Decimal, exponent: int) -> int:
    """Round a major-unit amount HALF_UP to integer minor units.

    ``exponent`` is the currency's number of decimal places, so the value is
    rounded to that many decimals and only then scaled into minor units.
    """

    return _round_half_up(value * (10 ** exponent))


def quote_order(
    lines: Iterable[Mapping[str, object]],
    *,
    currency: str = "USD",
    discount_bps: int = 0,
) -> Quote:
    """Price ``lines`` and return an immutable :class:`Quote`.

    ``lines`` is any iterable of mappings carrying ``unit_price`` (a decimal
    string) and ``quantity`` (a positive integer). Unrelated keys are ignored
    and the caller's objects are never mutated, so the same ``lines`` value
    can safely be reused.

    Each line is priced as ``unit_price * quantity`` using exact decimal
    arithmetic and only then rounded HALF_UP to the currency's minor units;
    the per-line rounded amounts are summed. The order-level discount is
    rounded HALF_UP to minor units as well, and
    ``total_minor = subtotal_minor - discount_minor``.

    Currency and ``discount_bps`` are validated even for empty orders, and
    any invalid input raises :class:`ValueError`.
    """

    currency_exponent(currency)
    _validate_discount_bps(discount_bps)

    exponent = _CURRENCY_EXPONENTS[currency]
    subtotal_minor = 0
    for line in lines:
        if not isinstance(line, Mapping):
            raise ValueError("each line must be a mapping with 'unit_price' and 'quantity'")
        try:
            raw_price = line["unit_price"]
            raw_quantity = line["quantity"]
        except KeyError as exc:
            raise ValueError(f"line is missing required key: {exc}") from None

        price = _parse_unit_price(raw_price)
        quantity = _parse_quantity(raw_quantity)
        subtotal_minor += _round_minor(price * quantity, exponent)

    if discount_bps:
        # The subtotal is already in minor units, so the fractional discount is
        # rounded straight to integer minor units (HALF_UP).
        discount_minor = _round_half_up(
            Decimal(subtotal_minor) * discount_bps / _BPS_DENOMINATOR
        )
    else:
        discount_minor = 0

    return Quote(
        currency=currency,
        subtotal_minor=subtotal_minor,
        discount_minor=discount_minor,
        total_minor=subtotal_minor - discount_minor,
    )


def format_minor(amount_minor: int, currency: str) -> str:
    """Format integer ``amount_minor`` using ``currency``'s decimal places.

    ``USD``/``EUR`` render two decimals (``1230 -> "12.30"``) while ``JPY``
    renders no decimals (``1230 -> "1230"``).
    """

    exponent = currency_exponent(currency)
    if exponent == 0:
        return str(amount_minor)

    sign = "-" if amount_minor < 0 else ""
    scale = 10 ** exponent
    whole, fraction = divmod(abs(amount_minor), scale)
    return f"{sign}{whole}.{fraction:0{exponent}d}"
