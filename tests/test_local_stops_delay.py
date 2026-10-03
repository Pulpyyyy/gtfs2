"""A local stop departure's delay, by the line sensors' rule.

IDFM's gateway writes a zero delay beside the realtime time of a bus two
minutes late. The line sensors read the delay as the gap to the
timetable then (b50e267); the local stops kept the feed's zero, shown as
"-", and the gap only in delay_realtime_derived, as text. Both now go
through gtfs_rt_helper.delay_of: the feed's delay, else the gap, in
seconds.
"""
from __future__ import annotations

import datetime
import types
import zoneinfo

import pytest

import ha_stub

ha_stub.install()

local_stops = ha_stub.load("local_stops")
gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
NOW = datetime.datetime(2026, 9, 29, 8, 0, tzinfo=PARIS)
ROW = {
    "trip_id": "T1", "direction_id": 0, "trip_short_name": None,
    "route_id": "R1", "stop_id": "S1", "stop_sequence": 3,
    "stop_name": "Radiguey", "route_short_name": "126",
    "route_long_name": "Bus 126", "trip_headsign": "Gare",
}
SCHEDULED = "2026-09-29 08:10:00"


def _element(monkeypatch, realtime=None, delay=0):
    """The element of the 08:10, the feed saying realtime (an aware
    datetime, or nothing) with delay."""
    statuses = {}
    if realtime is not None:
        statuses = {"R1": {"0": {"S1": {"departures": [realtime], "delays": [delay]}}}}
    monkeypatch.setattr(local_stops, "get_rt_route_trip_statuses", lambda self, feed=None: statuses)
    monkeypatch.setattr(local_stops, "struck_trips", lambda self: {})
    me = types.SimpleNamespace(_realtime=True, _icon="mdi:bus")
    return local_stops._build_local_stop_element(me, ROW, SCHEDULED, PARIS, PARIS, NOW)


def test_a_zero_delay_beside_a_later_time_is_the_gap(monkeypatch):
    element = _element(monkeypatch, datetime.datetime(2026, 9, 29, 8, 12, tzinfo=PARIS), 0)
    assert element["delay_realtime"] == 120
    assert element["departure_realtime"] == "08:12"


def test_the_feed_s_own_delay_is_kept(monkeypatch):
    element = _element(monkeypatch, datetime.datetime(2026, 9, 29, 8, 12, tzinfo=PARIS), 90)
    assert element["delay_realtime"] == 90


def test_an_early_bus_has_a_negative_delay(monkeypatch):
    element = _element(monkeypatch, datetime.datetime(2026, 9, 29, 8, 9, 30, tzinfo=PARIS), 0)
    assert element["delay_realtime"] == -30


@pytest.mark.parametrize("realtime", [datetime.datetime(2026, 9, 29, 8, 10, tzinfo=PARIS), None])
def test_on_time_or_no_realtime_says_no_delay(monkeypatch, realtime):
    assert _element(monkeypatch, realtime, 0)["delay_realtime"] == "-"


def test_one_rule_for_the_line_sensors_and_the_local_stops():
    assert gtfs_rt_helper.delay_of(0, 1_000_120, 1_000_000) == 120
    assert gtfs_rt_helper.delay_of(None, 1_000_120, 1_000_000) == 120
    assert gtfs_rt_helper.delay_of(90, 1_000_120, 1_000_000) == 90
    # nothing to measure the gap against: the feed's word stands
    assert gtfs_rt_helper.delay_of(0, None, 1_000_000) == 0
    assert gtfs_rt_helper.delay_of(0, 1_000_120, None) == 0
