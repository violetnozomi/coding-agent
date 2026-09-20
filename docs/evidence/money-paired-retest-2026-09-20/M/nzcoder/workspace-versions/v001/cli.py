from billing.quote import CURRENCY_EXPONENTS, quote_order


def receipt(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    exponent = CURRENCY_EXPONENTS[quote.currency]
    if exponent == 0:
        return f"{quote.currency} {quote.total_minor}"
    major = f"{quote.total_minor / (10 ** exponent):.{exponent}f}"
    return f"{quote.currency} {major}"
