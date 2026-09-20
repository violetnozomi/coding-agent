from billing.quote import quote_order


def monthly(lines, *, currency="USD", discount_bps=0):
    return quote_order(lines, currency=currency, discount_bps=discount_bps).total_minor
