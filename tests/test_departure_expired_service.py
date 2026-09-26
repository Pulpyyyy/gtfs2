"""A service whose period is over runs no more, not even after midnight.

Publishers who cut their feed by period keep the period just over beside
the next one: same weekdays, same trips, same times, two service_ids. HSL
runs its night train K at 24:21 on Wednesdays, once for the week of
September 21st and once for the week after. On Thursday at 00:05, the
route query listed both 24:21s: the calendar was read from yesterday on,
and a period already over still gave yesterday as a day it ran.
"""
from __future__ import annotations

import datetime
import io
import zipfile

import pygtfs
from freezegun import freeze_time

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.001,1.001\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,K,Kerava,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,OLD,T_OLD,0\nR,NEW,T_NEW,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T_OLD,24:21:00,24:21:00,S1,1\nT_OLD,24:31:00,24:31:00,S2,2\n"
                       "T_NEW,24:21:00,24:21:00,S1,1\nT_NEW,24:31:00,24:31:00,S2,2\n"),
    # Wednesdays only: the old period's last one is September 23rd
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nOLD,0,0,1,0,0,0,0,20260921,20260926\n"
                     "NEW,0,0,1,0,0,0,0,20260927,20261004\n"),
}


def _schedule(tmp_path):
    archive = tmp_path / "feed.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    archive.write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(archive))
    return schedule


# Thursday October 1st, just after midnight: Wednesday's 24:21 is to come
AT = datetime.datetime(2026, 10, 1, 0, 5, tzinfo=datetime.timezone.utc)


def test_the_route_query_leaves_out_a_period_already_over(tmp_path):
    schedule = _schedule(tmp_path)
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    with freeze_time(AT):
        rows, _ = gtfs_helper._fetch_departure_rows(
            "3", "S1: One", "S2: Two", schedule, route="R")
    trips = {(r["trip_id"], r["origin_depart_dt"]) for r in rows}
    assert trips == {("T_NEW", "2026-10-01 00:21:00")}
    schedule.engine.dispose()


def test_the_local_stops_query_leaves_out_a_period_already_over(tmp_path):
    schedule = _schedule(tmp_path)
    rows = gtfs_helper._fetch_local_stop_rows(
        schedule, 47.0, 1.0, 0.001, "+60 minute", "-15 minute",
        AT.replace(tzinfo=None))
    listed = {(r["trip_id"], r["departure_dt"]) for r in rows if r["stop_id"] == "S1"}
    assert listed == {("T_NEW", "2026-10-01 00:21:00")}
    schedule.engine.dispose()
