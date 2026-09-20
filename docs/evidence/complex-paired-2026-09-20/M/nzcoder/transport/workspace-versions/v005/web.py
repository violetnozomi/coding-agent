from billing.legacy import total_amount
def checkout(lines):
    return {"total": total_amount(lines)}
