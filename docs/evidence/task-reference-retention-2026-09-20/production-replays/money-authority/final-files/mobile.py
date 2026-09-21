"""Mobile summary: bare formatted amount with no currency (e.g. ``"12.30"``)."""

from billing.quote import quote_order


def summary(lines, *, currency="USD", discount_bps=0):
    """Return the total formatted for the currency, without the code."""

    return quote_order(lines, currency=currency, discount_bps=discount_bps).total_display
