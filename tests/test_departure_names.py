"""The journey sensor's lists name the line and where each departure goes,
whatever the feed fills.

A feed may leave every trip_headsign empty and put the destination on
each call (stop_headsign), and give some lines no route_short_name. The sensor
listed "(None)" as every departure's headsign and "(None/MAX Blue Line)"
as its line (vingerha/gtfs2#208).
"""
from __future__ import annotations

import datetime

import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")

UTC = datetime.timezone.utc
AT = datetime.datetime(2026, 10, 3, 6, 49, 20, tzinfo=UTC)


def _lists(**row):
    value = {"dest_arrival_dt": "2026-10-03 07:30:00", "trip_id": "T1", "origin_stop_id": "S1",
             "route_type": 0, "route_short_name": "20", "route_long_name": "Burnside/Stark",
             "trip_headsign": "Gresham", "origin_stop_headsign": "Gresham TC", **row}
    return gtfs_helper._next_departure_lists([(AT, value)], UTC)


def test_a_trip_with_no_headsign_takes_its_call_s():
    lists = _lists(trip_headsign=None)
    assert lists["next_departures_headsign"] == [f"{AT.isoformat()} (Gresham TC)"]


def test_a_line_with_no_short_name_reads_by_its_long_name():
    lists = _lists(route_short_name=None, route_long_name="MAX Blue Line")
    assert lists["next_departures_lines"] == [f"{AT.isoformat()} (MAX Blue Line)"]


def test_a_feed_filling_both_reads_as_before():
    lists = _lists()
    assert lists["next_departures_lines"] == [f"{AT.isoformat()} (20/Burnside/Stark)"]
    assert lists["next_departures_headsign"] == [f"{AT.isoformat()} (Gresham)"]
