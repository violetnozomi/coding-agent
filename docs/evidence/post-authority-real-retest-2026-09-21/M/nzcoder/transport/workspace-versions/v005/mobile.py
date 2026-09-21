from billing.quote import format_minor, quote_order


def summary(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    return format_minor(quote.total_minor, quote.currency)
