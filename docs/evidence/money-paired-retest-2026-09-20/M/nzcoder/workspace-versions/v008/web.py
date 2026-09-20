from billing.quote import CURRENCY_EXPONENTS, quote_order


def checkout(lines, *, currency="USD", discount_bps=0):
    quote = quote_order(lines, currency=currency, discount_bps=discount_bps)
    exponent = CURRENCY_EXPONENTS[quote.currency]
    return {"total": quote.total_minor / (10 ** exponent)}
