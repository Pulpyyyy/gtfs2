"""The stops of the listed trips, from the rider's stop on.

The trip stops service matched the origin by searching for its id in a
line of text: an id held inside another one (S1 in S10) started the
list at the wrong stop, and a stop name holding ": " cut it short.
"""
from __future__ import annotations

import asyncio
import types

from sqlalchemy import create_engine, text

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


def _schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trips.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, "
                          "stop_sequence integer, departure_time varchar)"))
        conn.execute(text("insert into stops values ('S10', 'Gare'), ('S1', 'Centre'), "
                          "('S2', 'Place: Hotel de Ville'), ('S3', 'Lac')"))
        calls = [("T1", "S10", 1, "08:00"), ("T1", "S1", 2, "08:05"), ("T1", "S2", 3, "08:10"),
                 ("T1", "S3", 4, "08:20"),
                 # listed out of order on purpose: stop_sequence decides
                 ("T2", "S3", 3, "09:20"), ("T2", "S1", 1, "09:05"), ("T2", "S2", 2, "09:10")]
        for trip, stop, seq, at in calls:
            conn.execute(text("insert into stop_times values (:t, :s, :q, :d)"),
                         {"t": trip, "s": stop, "q": seq, "d": f"1970-01-01 {at}:00.000000"})
    return types.SimpleNamespace(engine=engine)


def test_from_the_origin_on(tmp_path):
    got = gtfs_helper._trip_stops(_schedule(tmp_path), ["T1", "T2"], ["S1"])
    assert got == {
        "T1": ["Centre - 08:05:00", "Place: Hotel de Ville - 08:10:00", "Lac - 08:20:00"],
        "T2": ["Centre - 09:05:00", "Place: Hotel de Ville - 09:10:00", "Lac - 09:20:00"],
    }


def test_no_trip_no_query():
    assert gtfs_helper._trip_stops(None, [], ["S1"]) == {}


def test_an_unknown_entity_answers_nothing(monkeypatch):
    hass = types.SimpleNamespace(states=types.SimpleNamespace(get=lambda _e: None),
                                 config_entries=types.SimpleNamespace(async_get_entry=lambda _i: None))
    registry = types.SimpleNamespace(async_get=lambda _e: None)
    monkeypatch.setattr(gtfs_helper.er, "async_get", lambda _hass: registry)
    got = asyncio.run(gtfs_helper.get_trip_stops(hass, {"entity_id": "sensor.gone"}))
    assert got["trip_stops"] == {} and got["entity"] == "sensor.gone"
