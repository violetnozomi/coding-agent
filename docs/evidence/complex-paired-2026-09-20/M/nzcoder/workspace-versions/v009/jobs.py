"""CSV export job: ``"currency,total_minor\nUSD,1230\n"``."""

from billing.quote import quote_order


def export(lines, *, currency="USD", discount_bps=0):
    """Return a two-line CSV with a currency/total_minor header."""

    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    return f"currency,total_minor\n{quote.currency},{quote.total_minor}\n"
