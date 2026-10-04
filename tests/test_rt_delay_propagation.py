"""A trip's delay holds for the stops after its latest update.

GTFS-RT: when a trip update says nothing of a stop, the delay of its
latest update before that stop holds there, until the next update. SEPTA
rail gives one update per trip, at the train's next stop, by
stop_sequence and arrival delay: the sensor showed a delay only on the
stop the train was heading to, and the timetable everywhere after it.
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
NOW = datetime.datetime(2026, 10, 4, 18, 0, tzinfo=UTC)
SHOWN = datetime.datetime(2026, 10, 4, 18, 20, tzinfo=UTC)
# (trip, sequence at S1, time there): ON the board, OFF beyond it
CALLS = [("ON", 10, "18:20:00"), ("OFF", 10, "19:50:00")]


def _stamp(clock):
    hh, mm, ss = (int(x) for x in clock.split(":"))
    return (datetime.datetime(1970, 1, 1) + datetime.timedelta(hours=hh, minutes=mm, seconds=ss)
            ).strftime("%Y-%m-%d %H:%M:%S.000000")


@pytest.fixture
def schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stop_times (trip_id text, stop_id text, stop_sequence int, departure_time text)"))
        for trip, sequence, clock in CALLS:
            conn.execute(text("insert into stop_times values (:t, 'S1', :q, :d)"),
                         {"t": trip, "q": sequence, "d": _stamp(clock)})
    return types.SimpleNamespace(engine=engine)


def _read(schedule, trip, updates):
    """(departures, delays) the sensor reads at S1 from one trip update."""
    departure = {"trip_id": "ON", "departure_time": SHOWN,
                 "next_departures_trip_id": ["ON"], "next_departures": [SHOWN]}
    me = types.SimpleNamespace(
        _data={"file": "src", "next_departure": departure, "schedule": schedule},
        _rt_group="route", _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://feed.invalid/rt",
        _route_id="R1", _trip_id="ON", _trip_short_name="", _direction="0",
        _stop_id="S1", _stop_sequence=10, _trip_list=["ON"])
    feed = [{"id": trip, "trip_update": {"trip": {"trip_id": trip, "route_id": "R1", "direction_id": "0"},
                                          "stop_time_update": updates}}]
    with freeze_time(NOW):
        slot = gtfs_rt_helper.get_rt_route_trip_statuses(me, feed).get("R1", {}).get("0", {}).get("S1", {})
    return slot.get("departures", []), slot.get("delays", [])


def _by_sequence(sequence, delay=None, time=0, relationship=None):
    update = {"stop_id": "", "stop_sequence": sequence, "arrival": {"delay": delay, "time": time},
              "departure": {"delay": None, "time": 0}}
    if relationship:
        update["schedule_relationship"] = relationship
    return update


def test_the_delay_at_the_next_stop_holds_further_on(schedule):
    assert _read(schedule, "ON", [_by_sequence(5, 300)]) == (
        [SHOWN + datetime.timedelta(minutes=5)], [300])


def test_a_trip_off_the_board_carries_its_delay_too(schedule):
    assert _read(schedule, "OFF", [_by_sequence(5, -60)]) == (
        [datetime.datetime(2026, 10, 4, 19, 49, tzinfo=UTC)], [-60])


def test_the_latest_update_before_the_stop_is_the_one(schedule):
    _departures, delays = _read(schedule, "ON", [_by_sequence(3, 60), _by_sequence(6, 240), _by_sequence(12, 30)])
    assert delays == [240]


def test_the_stop_s_own_update_comes_first(schedule):
    _departures, delays = _read(schedule, "ON", [_by_sequence(6, 240), _by_sequence(10, 120)])
    assert delays == [120]


def test_a_skipped_stop_is_left_aside(schedule):
    _departures, delays = _read(schedule, "ON", [_by_sequence(3, 60), _by_sequence(6, 240, relationship="SKIPPED")])
    assert delays == [60]


def test_nothing_is_carried_past_a_stop_without_data(schedule):
    assert _read(schedule, "ON", [_by_sequence(3, 60), _by_sequence(6, relationship="NO_DATA")]) == ([], [])


def test_nothing_is_carried_from_an_update_after_the_stop(schedule):
    assert _read(schedule, "ON", [_by_sequence(12, 300)]) == ([], [])


def test_a_time_without_a_delay_is_not_carried(schedule):
    # the timetable's time at that stop is not read: no delay to carry
    assert _read(schedule, "ON", [_by_sequence(5, time=int(NOW.timestamp()) + 600)]) == ([], [])


def test_updates_naming_their_stops_without_a_sequence_carry_nothing(schedule):
    updates = [{"stop_id": "S0", "stop_sequence": None, "arrival": {"delay": 300, "time": 0}}]
    assert _read(schedule, "ON", updates) == ([], [])
