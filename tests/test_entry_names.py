"""Two entries never share their files through their names.

An entry's timetable and leg files are named after the entry, made a
file part: case, accents and punctuation fold away. The flow refused
only the exact same name, so "Orleans" beside "Orléans" was accepted and
wrote over the first one's files.
"""
from __future__ import annotations

import ha_stub

geojson = ha_stub.load("geojson")


def test_names_that_fold_together_are_taken():
    taken = {"Orléans", "Bus 1 Gare > Centre", None}
    assert geojson.name_in_use("Orleans", taken)
    assert geojson.name_in_use("bus-1 gare - centre", taken)
    assert geojson.name_in_use("Orléans", taken)
    assert not geojson.name_in_use("Orleans Gare", taken)
