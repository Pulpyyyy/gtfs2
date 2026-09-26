"""Stop times past 48:00 leave on the right day.

GTFS times a call by its service day, and a night bus, or a long train,
may call at 48:10: two days on. The departure queries lay each call on
its day with the whole day offset in their SELECT, but filtered it with
a test that only knew one day past midnight, so a call past 48:00 was
compared a day or two too early and dropped.
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

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.001,1.001\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,N1,Night,3\n",
    # one service day each, so a call laid on the wrong day cannot land on
    # the right time by chance
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D9,T48,0\nR,D10,T24,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T48,48:10:00,48:10:00,S1,1\nT48,48:20:00,48:20:00,S2,2\n"
                       "T24,24:10:00,24:10:00,S1,1\nT24,24:20:00,24:20:00,S2,2\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD9,1,1,1,1,1,1,1,20260609,20260609\n"
                     "D10,1,1,1,1,1,1,1,20260610,20260610\n"),
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


AT = datetime.datetime(2026, 6, 10, 23, 30, tzinfo=datetime.timezone.utc)


def test_the_route_query_keeps_a_call_past_48(tmp_path):
    schedule = _schedule(tmp_path)
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    with freeze_time(AT):
        rows, _ = gtfs_helper._fetch_departure_rows(
            "3", "S1: One", "S2: Two", schedule, route="R")
    trips = {(r["trip_id"], r["origin_depart_dt"]) for r in rows}
    # the 48:10 of June 9th's service leaves on June 11th at 00:10, as
    # does the 24:10 of June 10th's
    assert trips == {("T48", "2026-06-11 00:10:00"), ("T24", "2026-06-11 00:10:00")}
    schedule.engine.dispose()


def test_the_route_query_reads_back_as_far_as_its_calls(tmp_path):
    # past midnight, the 48:10 of June 9th's service is two service days
    # back: the days read have to reach it
    schedule = _schedule(tmp_path)
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    with freeze_time(datetime.datetime(2026, 6, 11, 0, 5, tzinfo=datetime.timezone.utc)):
        rows, _ = gtfs_helper._fetch_departure_rows(
            "3", "S1: One", "S2: Two", schedule, route="R")
    trips = {(r["trip_id"], r["origin_depart_dt"]) for r in rows}
    assert trips == {("T48", "2026-06-11 00:10:00"), ("T24", "2026-06-11 00:10:00")}
    schedule.engine.dispose()


def test_the_local_stops_query_keeps_a_call_past_48(tmp_path):
    schedule = _schedule(tmp_path)
    now = datetime.datetime(2026, 6, 11, 0, 5)
    rows = gtfs_helper._fetch_local_stop_rows(
        schedule, 47.0, 1.0, 0.001, "+60 minute", "-15 minute", now)
    listed = {(r["trip_id"], r["departure_dt"]) for r in rows if r["stop_id"] == "S1"}
    assert listed == {("T48", "2026-06-11 00:10:00"), ("T24", "2026-06-11 00:10:00")}
    schedule.engine.dispose()
