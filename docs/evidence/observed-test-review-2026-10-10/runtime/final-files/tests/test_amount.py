import pytest
from payment import amount
def test_explicit(): assert amount(-3, allow_refund=True) == -3
def test_default():
    with pytest.raises(ValueError): amount(-3)
def test_positive(): assert amount(3) == 3
