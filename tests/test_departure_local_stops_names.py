"""A local stop's departure names its line and where it goes, whatever
the feed fills.

TriMet leaves every trip_headsign empty and puts the destination on each
call, as stop_headsign; its MAX lines have no route_short_name, only a
long name. Each departure the local stops listed read route None and
headsign None, and a card showed neither the line nor where it goes.
"""
from __future__ import annotations

import datetime
import types

import feed_db
import ha_stub

ha_stub.install()

local_stops = ha_stub.load("local_stops")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 24, 9, 30, tzinfo=UTC)

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.01,1.01\n",
    "routes.txt": ("route_id,agency_id,route_short_name,route_long_name,route_type\n"
                   "R,A,,MAX Blue Line,0\n"),
    "trips.txt": "route_id,service_id,trip_id,direction_id,trip_headsign\nR,D,T,0,\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence,stop_headsign\n"
                       "T,10:00:00,10:00:00,S1,1,Gresham\nT,10:20:00,10:20:00,S2,2,Gresham\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def test_a_line_with_no_short_name_and_trips_with_no_headsign(tmp_path):
    schedule = feed_db.build(tmp_path, FEED)
    try:
        [row] = [r for r in local_stops._fetch_local_stop_rows(
            schedule, 47.0, 1.0, 0.001, "+60 minute", "-15 minute", NOW.replace(tzinfo=None))]
        element = local_stops._build_local_stop_element(
            types.SimpleNamespace(_realtime=False, _icon="mdi:tram"), row,
            row["departure_dt"], UTC, UTC, NOW)
    finally:
        schedule.engine.dispose()
    assert element["route"] == "MAX Blue Line"
    assert element["headsign"] == "Gresham"
