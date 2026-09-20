"""Web checkout: JSON-friendly totals backed by the central quote engine."""

from billing.quote import quote_order


def checkout(lines, *, currency="USD", discount_bps=0):
    """Return ``{"currency": ..., "total_minor": ...}`` for ``lines``.

    The shape is unchanged from the previous float-based API; the total is now
    exact integer minor units and the currency is reported alongside it.
    """

    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    return {"currency": quote.currency, "total_minor": quote.total_minor}
