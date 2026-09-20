from billing.quote import CURRENCY_EXPONENTS, format_minor, quote_order


def receipt(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    exponent = CURRENCY_EXPONENTS[quote.currency]
    return f"{quote.currency} {format_minor(quote.total_minor, exponent)}"
