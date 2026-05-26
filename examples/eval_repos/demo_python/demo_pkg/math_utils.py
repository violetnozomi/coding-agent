"""Math utilities with intentional demo bugs."""

def safe_divide(a: float, b: float) -> float:
    return a / b

def clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value
