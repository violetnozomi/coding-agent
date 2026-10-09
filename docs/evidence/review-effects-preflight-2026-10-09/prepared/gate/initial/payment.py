def amount(value, allow_refund=False):
    if value < 0:
        raise ValueError("negative")
    return value
