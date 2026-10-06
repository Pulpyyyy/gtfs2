"""Two entries never share their files through their names.

An entry's timetable and leg files are named after the entry, made a
file part: case, accents and punctuation fold away. The flow refused
only the exact same name, so "Orleans" beside "Orléans" was accepted and
wrote over the first one's files.
"""
from __future__ import annotations

import ha_stub

map_files = ha_stub.load("data.map_files")


def test_names_that_fold_together_are_taken():
    taken = {"Orléans", "Bus 1 Gare > Centre", None}
    assert map_files.name_in_use("Orleans", taken)
    assert map_files.name_in_use("bus-1 gare - centre", taken)
    assert map_files.name_in_use("Orléans", taken)
    assert not map_files.name_in_use("Orleans Gare", taken)
