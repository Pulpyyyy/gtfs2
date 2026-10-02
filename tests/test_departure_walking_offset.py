"""The walking time is asked of the query, not cut from its answer.

The offset is the time to reach the stop: a departure before now plus the
offset is out of reach. The query read the next 30 departures from now and
the offset was applied afterwards, so on a line every 2 minutes an offset
over an hour left nothing at all: the 30 departures read were all out of
reach. The local stops already asked the query from now plus the offset.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time

import feed_db
import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")


def _every_two_minutes():
    calls = []
    for n in range(200):
        leaves = 6 * 60 + 2 * n
        board = f"{leaves // 60:02d}:{leaves % 60:02d}:00"
        alight = f"{(leaves + 5) // 60:02d}:{(leaves + 5) % 60:02d}:00"
        calls.append(f"T{n},{board},{board},S1,1\nT{n},{alight},{alight},S2,2\n")
    return {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.01,1.01\n",
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,Red,1\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,D,T{n},0\n" for n in range(200)),
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(calls),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
    }


def _next(schedule, offset, monkeypatch):
    monkeypatch.setattr(gtfs_helper, "check_extracting", lambda *args: False)
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone="UTC"))
    data = {"offset": offset, "schedule": schedule, "gtfs_dir": "gtfs2", "file": "f",
            "route_type": "1", "origin": "S1: One", "destination": "S2: Two",
            "route": "R: 1", "name": "n"}
    with freeze_time(datetime.datetime(2026, 9, 24, 8, 0, tzinfo=datetime.timezone.utc)):
        return gtfs_helper.get_next_departure(hass, data)


def test_a_long_walk_to_a_frequent_line_still_finds_its_departure(tmp_path, monkeypatch):
    schedule = feed_db.build(tmp_path, _every_two_minutes())
    try:
        departure = _next(schedule, 90, monkeypatch)
    finally:
        schedule.engine.dispose()
    assert departure["departure_time"].strftime("%H:%M") == "09:32"


def test_no_walk_reads_from_now(tmp_path, monkeypatch):
    schedule = feed_db.build(tmp_path, _every_two_minutes())
    try:
        departure = _next(schedule, 0, monkeypatch)
    finally:
        schedule.engine.dispose()
    assert departure["departure_time"].strftime("%H:%M") == "08:02"
