import web

from openlibrary.core.lists.model import Seed, ThingReferenceDict


def test_thing_reference_dict_is_importable():
    ref = ThingReferenceDict(key="/works/OL1W")
    assert ref["key"] == "/works/OL1W"


def test_seed_with_string():
    seed = Seed([1, 2, 3], "subject/Politics and government")
    assert seed._list == [1, 2, 3]
    assert seed.value == "subject/Politics and government"
    assert seed.key == "subject/Politics and government"
    assert seed.type == "subject"


def test_seed_with_nonstring():
    not_a_string = web.storage({"key": "not_a_string.key"})
    seed = Seed((1, 2, 3), not_a_string)
    assert seed._list == (1, 2, 3)
    assert seed.value == not_a_string
    assert hasattr(seed, "key")
    assert hasattr(seed, "type") is False
    assert seed.document == not_a_string


def test_seed_from_json_with_thing_reference():
    the_list = [1, 2, 3]
    seed = Seed.from_json(the_list, {"key": "/works/OL1W"})
    assert seed._list == the_list
    assert seed.key == "/works/OL1W"
    assert hasattr(seed, "type") is False


def test_seed_from_json_with_subject_string():
    the_list = [1, 2, 3]
    seed = Seed.from_json(the_list, "subject/Politics and government")
    assert seed._list == the_list
    assert seed.value == "subject/Politics and government"
    assert seed.key == "subject/Politics and government"
    assert seed.type == "subject"
