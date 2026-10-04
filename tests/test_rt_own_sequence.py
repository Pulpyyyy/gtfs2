"""A stop update naming no stop is read at the sequence of its own trip.

A feed may update every call of a trip by its stop_sequence alone, no
stop_id. The board read every trip at the shown trip's sequence: on a
line whose patterns call at one stop under two numbers (8 on one, 28 on
the other), a trip of the second pattern took the delay of the stop it
called at eighth, 82 s, instead of its own 45 s. The trips the board
does not list were matched by stop_id only and read nothing from such a
feed. The sequence each trip calls with is now read from the timetable.
"""
from __future__ import annotations

import datetime
import types

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, text

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 3, 19, 0, tzinfo=UTC)
# (trip, stop, sequence, time): SHOWN calls at S1 eighth, the other
# pattern twenty-eighth, its eighth call being S9; BRANCH never calls at S1
CALLS = [("SHOWN", "S1", 8, "19:05:00"),
         ("OTHER", "S9", 8, "18:50:00"), ("OTHER", "S1", 28, "19:10:00"),
         ("OFF", "S9", 8, "20:30:00"), ("OFF", "S1", 28, "20:40:00"),
         ("BRANCH", "S9", 8, "19:00:00")]


def _stamp(clock):
    hh, mm, ss = (int(x) for x in clock.split(":"))
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(hours=hh, minutes=mm, seconds=ss)
            ).strftime("%Y-%m-%d %H:%M:%S.000000")


@pytest.fixture
def schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stop_times (trip_id text, stop_id text, stop_sequence int, departure_time text)"))
        for trip, stop, sequence, clock in CALLS:
            conn.execute(text("insert into stop_times values (:t, :s, :q, :d)"),
                         {"t": trip, "s": stop, "q": sequence, "d": _stamp(clock)})
    return types.SimpleNamespace(engine=engine)


def _context(schedule):
    shown, other = (datetime.datetime(2026, 10, 3, 19, 5, tzinfo=UTC),
                    datetime.datetime(2026, 10, 3, 19, 10, tzinfo=UTC))
    departure = {"trip_id": "SHOWN", "departure_time": shown,
                 "next_departures_trip_id": ["SHOWN", "OTHER"], "next_departures": [shown, other]}
    return types.SimpleNamespace(
        _data={"file": "src", "next_departure": departure, "schedule": schedule},
        _rt_group="route", _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://feed.invalid/rt",
        _route_id="R1", _trip_id="SHOWN", _trip_short_name="", _direction="0",
        _stop_id="S1", _stop_sequence=8, _trip_list=["SHOWN", "OTHER"])


def _delays(schedule, delays):
    """{trip: delay} the sensor reads, the feed giving {trip: {sequence: delay}}
    with no stop_id and no time."""
    feed = [{"id": trip, "trip_update": {
        "trip": {"trip_id": trip, "route_id": "R1", "direction_id": "0"},
        "stop_time_update": [{"stop_id": "", "stop_sequence": sequence,
                              "departure": {"time": 0, "delay": delay}}
                             for sequence, delay in calls.items()]}}
        for trip, calls in delays.items()]
    with freeze_time(NOW):
        found = gtfs_rt_helper.get_rt_route_trip_statuses(_context(schedule), feed)
    slot = found.get("R1", {}).get("0", {}).get("S1", {})
    return dict(zip(slot.get("trips", []), slot.get("delays", [])))


def test_each_board_trip_is_read_at_its_own_sequence(schedule):
    got = _delays(schedule, {"SHOWN": {8: 30}, "OTHER": {8: 82, 28: 45}})
    assert got == {"SHOWN": 30, "OTHER": 45}


def test_a_trip_off_the_board_is_read_at_its_own_sequence(schedule):
    assert _delays(schedule, {"OFF": {8: 10, 28: 60}}) == {"OFF": 60}


def test_a_trip_not_calling_here_says_nothing_here(schedule):
    assert _delays(schedule, {"BRANCH": {8: 90, 28: 120}}) == {}


def test_without_a_database_the_board_keeps_the_shown_sequence():
    # the captured upstream cases run without one
    got = _delays(None, {"SHOWN": {8: 30}, "OTHER": {8: 82, 28: 45}})
    assert got == {"SHOWN": 30, "OTHER": 82}
