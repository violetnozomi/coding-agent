from demo_pkg.math_utils import safe_divide

def test_safe_divide_zero_returns_zero():
    assert safe_divide(5, 0) == 0
