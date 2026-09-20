"""Exact, currency-aware invoice pricing for the invoice desk.

``quote_order`` is the single source of pricing arithmetic in the project.
All consumers (web, cli, jobs, mobile, reporting) delegate to it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

#: Number of decimal places ("minor units") per supported currency.
CURRENCY_EXPONENTS: Mapping[str, int] = {"USD": 2, "EUR": 2, "JPY": 0}

#: Inclusive bounds for the order-level discount, in basis points.
MIN_DISCOUNT_BPS = 0
MAX_DISCOUNT_BPS = 10000

_ONE = Decimal(1)


@dataclass(frozen=True)
class Quote:
    """An immutable priced order, expressed in currency minor units."""

    currency: str
    subtotal_minor: int
    discount_minor: int
    total_minor: int

    @property
    def exponent(self) -> int:
        """Number of decimal places used when rendering this currency."""

        return CURRENCY_EXPONENTS[self.currency]

    def format_amount(self, amount_minor: int | None = None) -> str:
        """Render a minor-unit amount as a fixed-precision decimal string.

        JPY (0 decimals) renders ``"1230"``; USD/EUR render ``"12.30"``.
        """

        minor = self.total_minor if amount_minor is None else amount_minor
        sign = "-" if minor < 0 else ""
        minor = abs(minor)
        exponent = int(self.exponent)
        if exponent == 0:
            return f"{sign}{minor}"
        scale: int = 10**exponent
        whole: int = minor // scale
        fraction: int = minor % scale
        return f"{sign}{whole}.{fraction:0{exponent}d}"

    @property
    def total_display(self) -> str:
        """Total rendered with the currency's number of decimal places."""

        return self.format_amount()

    @property
    def formatted_total(self) -> str:
        """Total prefixed with the currency code, e.g. ``"USD 12.30"``."""

        return f"{self.currency} {self.total_display}"


def _validate_currency(currency: object) -> str:
    if not isinstance(currency, str):
        raise ValueError(f"unsupported currency: {currency!r}")
    code = currency.upper()
    if code not in CURRENCY_EXPONENTS:
        raise ValueError(f"unsupported currency: {currency!r}")
    return code


def _validate_discount_bps(discount_bps: object) -> int:
    if isinstance(discount_bps, bool) or not isinstance(discount_bps, int):
        raise ValueError(f"invalid discount_bps: {discount_bps!r}")
    if not MIN_DISCOUNT_BPS <= discount_bps <= MAX_DISCOUNT_BPS:
        raise ValueError(f"discount_bps out of range [0, 10000]: {discount_bps!r}")
    return discount_bps


def _to_decimal_price(unit_price: object) -> Decimal:
    if isinstance(unit_price, bool) or isinstance(unit_price, float):
        raise ValueError(f"invalid unit price: {unit_price!r}")
    if isinstance(unit_price, Decimal):
        price = unit_price
    elif isinstance(unit_price, str):
        text = unit_price.strip()
        if not text:
            raise ValueError(f"invalid unit price: {unit_price!r}")
        try:
            price = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"invalid unit price: {unit_price!r}") from exc
    else:
        raise ValueError(f"invalid unit price: {unit_price!r}")
    if not price.is_finite():
        raise ValueError(f"invalid unit price: {unit_price!r}")
    if price < 0:
        raise ValueError(f"negative unit price: {unit_price!r}")
    return price


def _parse_quantity(quantity: object) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError(f"invalid quantity: {quantity!r}")
    if quantity <= 0:
        raise ValueError(f"invalid quantity: {quantity!r}")
    return quantity


def _line_values(line: object) -> tuple[object, object]:
    if not isinstance(line, Mapping):
        raise ValueError(f"invalid line: {line!r}")
    try:
        unit_price = line["unit_price"]
        quantity = line["quantity"]
    except KeyError as exc:  # pragma: no cover - message formatting only
        raise ValueError(f"line is missing {exc.args[0]!r}: {line!r}") from exc
    return unit_price, quantity


def _round_half_up(value: Decimal, exponent: int) -> int:
    """Round ``value`` HALF_UP to ``exponent`` decimal places.

    Returns the amount as an integer count of minor units, so ``2.50`` in USD
    (exponent 2) becomes ``250``.
    """

    scaled = value.scaleb(exponent)
    return int(scaled.quantize(_ONE, rounding=ROUND_HALF_UP))


def quote_order(
    lines: Iterable[Mapping[str, object]],
    *,
    currency: str = "USD",
    discount_bps: int = 0,
) -> Quote:
    """Price ``lines`` into an immutable :class:`Quote`.

    Each line is a mapping with a decimal ``unit_price`` string and a positive
    integer ``quantity``; unrelated keys are ignored and caller input is never
    mutated. Per-line amounts are rounded HALF_UP only after multiplying the
    exact decimal price by the quantity, then summed. ``discount_bps`` is
    applied to the order subtotal (not per line) and rounded HALF_UP. Currency
    and discount are validated even for empty orders.
    """

    code = _validate_currency(currency)
    bps = _validate_discount_bps(discount_bps)
    exponent = CURRENCY_EXPONENTS[code]

    subtotal_minor = 0
    for line in lines:
        unit_price, quantity = _line_values(line)
        price = _to_decimal_price(unit_price)
        count = _parse_quantity(quantity)
        subtotal_minor += _round_half_up(price * count, exponent)

    # The discount is derived from the (already rounded) subtotal and rounded to
    # whole minor units, i.e. HALF_UP in minor-unit space.
    discount_minor = _round_half_up(
        Decimal(subtotal_minor) * Decimal(bps) / Decimal(10000), 0
    )

    return Quote(
        currency=code,
        subtotal_minor=subtotal_minor,
        discount_minor=discount_minor,
        total_minor=subtotal_minor - discount_minor,
    )


__all__ = [
    "CURRENCY_EXPONENTS",
    "MAX_DISCOUNT_BPS",
    "MIN_DISCOUNT_BPS",
    "Quote",
    "quote_order",
]
