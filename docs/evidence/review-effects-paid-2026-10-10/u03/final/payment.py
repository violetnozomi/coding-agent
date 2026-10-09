def amount(value, allow_refund=False):
    if value < 0 and not allow_refund:
        raise ValueError("negative")
    return value
