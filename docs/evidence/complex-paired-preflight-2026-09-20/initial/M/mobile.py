from billing.legacy import total_amount
def summary(lines):
    return f"{total_amount(lines):.2f}"
