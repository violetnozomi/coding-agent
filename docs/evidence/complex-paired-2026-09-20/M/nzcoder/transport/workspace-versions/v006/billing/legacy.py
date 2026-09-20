def total_amount(lines):
    return sum(float(line["unit_price"]) * line["quantity"] for line in lines)
