"""The line, way and stops of the departure shown, or the entry's own.

Eight places read them, each with its own fallback: an empty route_id was
kept by the realtime readers and replaced by the map files, an origin of
None crashed one reader and fell back in the others. shown_ends is the one
rule they all use now.
"""
from __future__ import annotations

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

ENTRY = {"route": "R1: Line 1", "direction": "1", "origin": "S1: One", "destination": "S9: Nine"}


def test_the_departure_shown_leads():
    departure = {"route_id": "R2", "trip_direction_id": "0",
                 "origin_stop_id": "S2", "destination_stop_id": "S8"}
    assert gtfs_helper.shown_ends(ENTRY, departure) == ("R2", "0", "S2", "S8")


def test_no_departure_left_reads_the_entry():
    assert gtfs_helper.shown_ends(ENTRY, {}) == ("R1", "1", "S1", "S9")


def test_an_empty_line_or_stop_falls_back_on_the_entry():
    departure = {"route_id": "", "origin_stop_id": None, "destination_stop_id": ""}
    assert gtfs_helper.shown_ends(ENTRY, departure) == ("R1", "1", "S1", "S9")


def test_a_direction_of_zero_is_kept():
    assert gtfs_helper.shown_ends(ENTRY, {"trip_direction_id": 0})[1] == "0"
