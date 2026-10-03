"""Direction labels read from the longest trip of each direction.

A trip with no direction_id counts as direction 0. Grouped apart from
the zeros in the query and merged with them after, the two longest
trips went into one list, and the label read the start of one and the
end of the other.
"""
from __future__ import annotations

import feed_db
import ha_stub

pair_direction = ha_stub.load("pair_direction")

STOPS = {"A": "Gare", "B": "Centre", "C": "Hopital", "D": "Stade", "E": "Lac"}


def _schedule(tmp_path, trips):
    """The trips [(trip_id, direction_id, stops)] of line R, imported by pygtfs."""
    return feed_db.build(tmp_path, feed_db.line_feed(
        STOPS, [(trip, "R", direction, calls) for trip, direction, calls in trips]))


def test_trips_without_direction_count_as_direction_zero(tmp_path):
    schedule = _schedule(tmp_path, [
        ("T0", 0, "ABC"),          # direction 0, three calls
        ("TN", None, "ABCD"),      # no direction_id, the longest of "0"
        ("T1", 1, "DCBA"),
    ])
    labels = pair_direction.get_direction_labels(schedule, "R")
    assert labels == {"0": "Gare → Stade", "1": "Stade → Gare"}


def test_the_longest_trip_names_its_direction(tmp_path):
    schedule = _schedule(tmp_path, [
        ("short", 0, "BC"),
        ("long", 0, "ABCDE"),
    ])
    assert pair_direction.get_direction_labels(schedule, "R") == {"0": "Gare → Lac"}
