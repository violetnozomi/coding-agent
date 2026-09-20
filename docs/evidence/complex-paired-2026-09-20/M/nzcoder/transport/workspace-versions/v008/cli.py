"""CLI receipt rendering: ``"USD 12.30"`` (JPY renders without decimals)."""

from billing.quote import quote_order


def receipt(lines, *, currency="USD", discount_bps=0):
    """Return the formatted total prefixed with the currency code."""

    return quote_order(lines, currency=currency, discount_bps=discount_bps).formatted_total
