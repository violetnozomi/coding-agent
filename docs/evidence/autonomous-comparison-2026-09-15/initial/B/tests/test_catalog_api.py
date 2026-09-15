from catalog.api import format_product
def test_format(): assert format_product({'name':'A'}) == 'A'
