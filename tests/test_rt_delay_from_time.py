"""A feed that gives a time and no delay has the delay read off the timetable.

IDFM's gateway announces a metro two minutes late by its time and writes
0 in its delay field, TAO and Palm Bus leave the field out: the sensor
said next_delays_realtime 0 for a train running late. When the feed gives
a time and no delay, the delay is the gap between that time and the
timetable's for the trip, as the leg file already reads it; a delay the
feed gives stands as it is.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 27, 11, 30, tzinfo=UTC)
SCHEDULED = NOW + datetime.timedelta(minutes=8)
EXPECTED = int((NOW + datetime.timedelta(minutes=10)).timestamp())


def _context(on_board=True):
    departure = {"trip_id": "T1", "departure_time": SCHEDULED} if on_board else {}
    return types.SimpleNamespace(
        _data={"file": "src", "next_departure": departure}, _rt_group="trip", _headers={},
        _vehicle_position_url=None, _trip_update_url="http://feed.invalid/rt",
        _route_delimiter=None, _route_id="R1", _trip_id="T1", _trip_short_name="",
        _direction="0", _stop_id="S1", _stop_sequence=3, _trip_list=[])


def _delays(departure, on_board=True):
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T1", "route_id": "R1"},
        "stop_time_update": [{"stop_id": "S1", "stop_sequence": 3, "departure": departure}]}}]
    with freeze_time(NOW):
        found = gtfs_rt_helper.get_rt_route_trip_statuses(_context(on_board), feed)
    return found["R1"]["0"]["S1"]["delays"]


def test_a_zero_delay_beside_a_later_time_is_the_gap_to_the_timetable():
    assert _delays({"time": EXPECTED, "delay": 0}) == [120]


def test_a_time_with_no_delay_at_all_is_read_the_same():
    assert _delays({"time": EXPECTED}) == [120]


def test_a_delay_the_feed_gives_stands():
    assert _delays({"time": EXPECTED, "delay": 60}) == [60]


def test_without_the_timetable_s_time_the_feed_s_zero_stays():
    # a trip the board does not list: no time of its own to compare with
    assert _delays({"time": EXPECTED, "delay": 0}, on_board=False) == [0]
