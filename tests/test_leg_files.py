"""An entry keeps one leg file, the one of its current line.

The leg file is named after the departure's line and direction as well
as the entry: when those changed, the old file stayed until the entry was
removed, and a card still found it.
"""
from __future__ import annotations

import ha_stub

geojson = ha_stub.load("geojson")


def test_the_leg_of_an_earlier_line_goes(tmp_path):
    old = tmp_path / geojson.leg_geojson_name("R1", "0", "a")
    none = tmp_path / geojson.leg_geojson_name("R3", "None", "a")
    new = tmp_path / geojson.leg_geojson_name("R2", "1", "a")
    other = tmp_path / geojson.leg_geojson_name("R1", "0", "x leg a")
    for path in (old, none, new, other):
        path.write_text("{}")
    geojson._drop_other_legs(str(tmp_path), "a", str(new))
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([new.name, other.name])
