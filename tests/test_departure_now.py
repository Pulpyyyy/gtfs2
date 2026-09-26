"""A departure is shown until its time, and gone from its time on.

The departure query pre-filters on the clock in SQL and then keeps the
departures strictly after now: a tram due at 10:00:00 is the next one at
09:59:59 and gone at 10:00:00, when the one after it takes its place.
Set here with the times written out, the provider sweep reading the next
ride the same way.
"""
from __future__ import annotations

import datetime
import io
import types
import zipfile

import pygtfs
from freezegun import freeze_time

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.1,1.1\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,Red,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T10,0\nR,D,T11,0\n",
    "stop_times.txt": (HEAD + "T10,10:00:00,10:00:00,S1,1\nT10,10:10:00,10:10:00,S2,2\n"
                       "T11,11:00:00,11:00:00,S1,1\nT11,11:10:00,11:10:00,S2,2\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def _next_trip(schedule, at, folder):
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: str(folder.joinpath(*parts)), time_zone="UTC"))
    data = {"schedule": schedule, "gtfs_dir": ".", "file": "feed", "route_type": "3",
            "origin": "S1: One", "destination": "S2: Two", "route": "R: Red",
            "direction": "0", "offset": 0, "include_tomorrow": False}
    with freeze_time(at):
        return (gtfs_helper.get_next_departure(hass, data) or {}).get("trip_id")


def test_a_departure_is_gone_from_its_time_on(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    day = datetime.date(2026, 9, 24)
    try:
        before = datetime.datetime.combine(day, datetime.time(9, 59, 59))
        on_time = datetime.datetime.combine(day, datetime.time(10, 0, 0))
        assert _next_trip(schedule, before, tmp_path) == "T10"
        assert _next_trip(schedule, on_time, tmp_path) == "T11"
    finally:
        schedule.engine.dispose()
