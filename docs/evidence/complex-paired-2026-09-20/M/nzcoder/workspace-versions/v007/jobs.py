from billing.legacy import total_amount
def export(lines):
    return f"total\n{total_amount(lines):.2f}\n"
