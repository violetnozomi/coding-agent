from demo_pkg.parser import parse_items

def test_parse_items_empty_string():
    assert parse_items("") == []

def test_parse_items_trims_values():
    assert parse_items("a, b") == ["a", "b"]
