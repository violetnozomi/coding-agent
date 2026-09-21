from billing.quote import quote_order


def checkout(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    return {"currency": quote.currency, "total_minor": quote.total_minor}
