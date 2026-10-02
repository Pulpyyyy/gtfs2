"""The local stops read their window on the network's clock.

The stored stop times are the local time where the network runs. The
journey sensor reads them against now in the agency's zone (_feed_now);
the local stops read them against Home Assistant's wall clock, so a feed
in another zone than Home Assistant's had its window shifted by the gap:
a UTC feed seen from Paris listed the departures of two hours later.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time

import feed_db
import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

local_stops = ha_stub.load("local_stops")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.01,1.01\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,Red,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T,10:00:00,10:00:00,S1,1\nT,10:20:00,10:20:00,S2,2\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def test_the_window_starts_at_now_on_the_feed_s_clock(tmp_path, monkeypatch):
    schedule = feed_db.build(tmp_path, FEED)
    asked = []
    monkeypatch.setattr(local_stops, "check_extracting", lambda *args: False)
    monkeypatch.setattr(local_stops, "_tracker_position", lambda hass, tracker: (47.0, 1.0))
    monkeypatch.setattr(local_stops, "_fetch_local_stop_rows",
                        lambda *args: asked.append(args[-1]) or [])
    monkeypatch.setattr(local_stops, "_interpret_local_stop_rows", lambda me, rows: rows)
    me = types.SimpleNamespace(hass=None, _data={
        "schedule": schedule, "gtfs_dir": "gtfs2", "file": "f", "offset": 5,
        "device_tracker_id": "person.me"})
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))
    try:
        with freeze_time(datetime.datetime(2026, 9, 24, 9, 30, tzinfo=datetime.timezone.utc)):
            local_stops.get_local_stops_next_departures(me)
    finally:
        dt_util.set_default_time_zone(datetime.timezone.utc)
        schedule.engine.dispose()
    # 09:30 in UTC, the feed's zone, and the 5 minutes' walk
    assert asked == [datetime.datetime(2026, 9, 24, 9, 35)]
