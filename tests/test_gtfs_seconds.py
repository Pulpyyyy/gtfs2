"""One reader of stop times, whatever form they come in.

The geojson export, the realtime window and the rotation timing each read
stop times their own way: the first refused the bare text a database from
another pygtfs build holds, the second a timedelta, the third raised on
anything but text. They share gtfs_seconds now.
"""
from __future__ import annotations

import datetime

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")
geojson = ha_stub.load("geojson")
rt_window = ha_stub.load("rt_window")

CASES = [
    ("1970-01-01 08:15:00", 8 * 3600 + 15 * 60),
    ("1970-01-01 08:15:00.000000", 8 * 3600 + 15 * 60),
    ("1970-01-02 01:15:00", 25 * 3600 + 15 * 60),
    ("1970-01-03 00:10:00", 48 * 3600 + 10 * 60),
    ("25:15:00", 25 * 3600 + 15 * 60),
    ("08:15:00", 8 * 3600 + 15 * 60),
    (datetime.timedelta(hours=24, minutes=36), 24 * 3600 + 36 * 60),
    (29700, 29700),
    ("29700", 29700),
    (None, None),
    ("", None),
    ("soon", None),
    ("08:15", None),
]


def test_every_form_reads_the_same():
    for value, seconds in CASES:
        assert gtfs_helper.gtfs_seconds(value) == seconds, value


def test_the_modules_read_through_it():
    assert geojson.gtfs_seconds is gtfs_helper.gtfs_seconds
    assert rt_window.gtfs_seconds is gtfs_helper.gtfs_seconds
    # the line file writes the feed's own clock, past 24:00 after midnight
    assert geojson._fmt_gtfs_time("25:15:00") == "25:15:00"
    assert geojson._fmt_gtfs_time("1970-01-02 00:36:00") == "24:36:00"
