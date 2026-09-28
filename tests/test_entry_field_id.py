"""An entry field's id, read the one way the whole integration reads it.

The flow stores a route, an origin, a destination or an agency as
"id: name", and 20 places cut the id out by hand, some guarding an empty
field and some not. const.id_of does it for all of them: what stands
before the first ": ", and "" for a field that is empty or missing.
"""
from __future__ import annotations

import sys

import ha_stub

ha_stub.load("const")
const = sys.modules["gtfs2_under_test.const"]


def test_the_id_is_what_stands_before_the_name():
    assert const.id_of("R1: Tram A") == "R1"
    assert const.id_of("IDFM:463292: Raspail") == "IDFM:463292"
    # a name holding ": " itself keeps the id whole
    assert const.id_of("S1: Gare: quai 2") == "S1"


def test_a_field_without_a_name_is_its_id():
    assert const.id_of("R1") == "R1"


def test_an_empty_or_missing_field_is_no_id():
    assert const.id_of("") == ""
    assert const.id_of(None) == ""
