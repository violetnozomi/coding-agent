from catalog.api import format_product
def test_report(): assert format_product({'name':'A'}) == 'A'
