"""Removing an entry takes its own leg file, never another entry's.

The leg file ends with the entry's name made a file part, and the removal
finds it by that ending whatever line it was written under. An entry
whose name ends like another's, "Tram 1 leg Centre" and "Centre", wrote
a file the other's glob found: removing "Centre" took it.
"""
from __future__ import annotations

import ha_stub

geojson = ha_stub.load("geojson")
integration = ha_stub.load("__init__")


def test_a_name_that_ends_like_another_is_not_it():
    theirs = geojson.leg_geojson_name("R", "0", "Tram 1 leg Centre")
    ours = geojson.leg_geojson_name("R", "1", "Centre")
    assert geojson.owns_leg_file(ours, "Centre")
    assert not geojson.owns_leg_file(theirs, "Centre")
    assert geojson.owns_leg_file(theirs, "Tram 1 leg Centre")
    # a line id with underscores is still a line id
    assert geojson.owns_leg_file(geojson.leg_geojson_name("FR:Line::A_B", "none", "Centre"), "Centre")


def test_removing_an_entry_keeps_the_other_ones_leg(tmp_path):
    theirs = tmp_path / geojson.leg_geojson_name("R", "0", "Tram 1 leg Centre")
    ours = tmp_path / geojson.leg_geojson_name("R", "1", "Centre")
    for path in (theirs, ours):
        path.write_text("{}")
    integration._remove_geojson_files(str(tmp_path), "Centre", [])
    assert [p.name for p in tmp_path.iterdir()] == [theirs.name]
