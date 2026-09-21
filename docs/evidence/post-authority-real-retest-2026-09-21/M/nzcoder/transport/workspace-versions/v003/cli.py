from billing.quote import format_minor, quote_order


def receipt(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    return f"{quote.currency} {format_minor(quote.total_minor, quote.currency)}"
