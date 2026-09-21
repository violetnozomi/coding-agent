from billing.quote import quote_order


def export(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    return f"currency,total_minor\n{quote.currency},{quote.total_minor}\n"
