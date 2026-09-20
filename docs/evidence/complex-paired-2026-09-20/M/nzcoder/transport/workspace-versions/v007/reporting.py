"""Monthly reporting: integer minor-unit totals."""

from billing.quote import quote_order


def monthly(lines, *, currency="USD", discount_bps=0):
    """Return the exact total in minor units as an ``int``."""

    return quote_order(lines, currency=currency, discount_bps=discount_bps).total_minor
