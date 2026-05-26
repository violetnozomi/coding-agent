from demo_pkg.strings import normalize_space, slugify

def test_slugify_basic():
    assert slugify("Hello, NZ Coder!") == "hello-nz-coder"

def test_normalize_space_existing_behavior():
    assert normalize_space("a   b") == "a b"
