from billing.legacy import total_amount
def receipt(lines):
    return f"USD {total_amount(lines):.2f}"
