"""Direction labels read from the longest trip of each direction.

A trip with no direction_id counts as direction 0. Grouped apart from
the zeros in the query and merged with them after, the two longest
trips went into one list, and the label read the start of one and the
end of the other.
"""
from __future__ import annotations

import types

from sqlalchemy import create_engine, text

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

STOPS = {"A": "Gare", "B": "Centre", "C": "Hopital", "D": "Stade", "E": "Lac"}


def _schedule(tmp_path, trips):
    engine = create_engine(f"sqlite:///{tmp_path / 'labels.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table trips (trip_id varchar, route_id varchar, direction_id integer)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, stop_sequence integer)"))
        for stop_id, name in STOPS.items():
            conn.execute(text("insert into stops values (:s, :n)"), {"s": stop_id, "n": name})
        for trip_id, direction, calls in trips:
            conn.execute(text("insert into trips values (:t, 'R', :d)"), {"t": trip_id, "d": direction})
            for seq, stop_id in enumerate(calls, 1):
                conn.execute(text("insert into stop_times values (:t, :s, :q)"),
                             {"t": trip_id, "s": stop_id, "q": seq})
    return types.SimpleNamespace(engine=engine)


def test_trips_without_direction_count_as_direction_zero(tmp_path):
    schedule = _schedule(tmp_path, [
        ("T0", 0, "ABC"),          # direction 0, three calls
        ("TN", None, "ABCD"),      # no direction_id, the longest of "0"
        ("T1", 1, "DCBA"),
    ])
    labels = gtfs_helper.get_direction_labels(schedule, "R")
    assert labels == {"0": "Gare → Stade", "1": "Stade → Gare"}


def test_the_longest_trip_names_its_direction(tmp_path):
    schedule = _schedule(tmp_path, [
        ("short", 0, "BC"),
        ("long", 0, "ABCDE"),
    ])
    assert gtfs_helper.get_direction_labels(schedule, "R") == {"0": "Gare → Lac"}
