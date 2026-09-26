"""A call the feed left untimed is not listed, and breaks nothing.

GTFS asks for times at the timepoints only: a call between two of them
may leave arrival_time and departure_time empty, for the reader to
interpolate. Clemson's CATbus leaves three calls in four so. An entry
whose destination was such a call made get_next_departure fail on
strptime(None); one whose origin was simply found nothing.
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
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\n"
                  "S2,Two,47.01,1.01\nS3,Three,47.02,1.02\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,Red,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T,0\n",
    # S2 lies between two timepoints, untimed
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence,timepoint\n"
                       "T,10:00:00,10:00:00,S1,1,1\nT,,,S2,2,0\nT,10:20:00,10:20:00,S3,3,1\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}

AT = datetime.datetime(2026, 9, 24, 9, 30, tzinfo=datetime.timezone.utc)


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


def _rows(schedule, origin, destination):
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    with freeze_time(AT):
        rows, _ = gtfs_helper._fetch_departure_rows(
            "3", origin, destination, schedule, route="R")
    return rows


def test_an_untimed_end_gives_no_row(tmp_path):
    schedule = _schedule(tmp_path)
    assert _rows(schedule, "S1: One", "S2: Two") == []
    assert _rows(schedule, "S2: Two", "S3: Three") == []
    schedule.engine.dispose()


def test_the_timed_calls_of_the_same_trip_still_ride(tmp_path):
    schedule = _schedule(tmp_path)
    rows = _rows(schedule, "S1: One", "S3: Three")
    assert (rows[0]["trip_id"], rows[0]["origin_depart_dt"], rows[0]["dest_arrival_dt"]) == \
        ("T", "2026-09-24 10:00:00", "2026-09-24 10:20:00")
    # and every row carries the four times the answer is built from
    assert all(r[key] for r in rows for key in (
        "origin_arrival_dt", "origin_depart_dt", "dest_arrival_dt", "dest_depart_dt"))
    schedule.engine.dispose()


def test_the_local_stops_query_leaves_an_untimed_call_out(tmp_path):
    schedule = _schedule(tmp_path)
    rows = gtfs_helper._fetch_local_stop_rows(
        schedule, 47.01, 1.01, 0.03, "+60 minute", "-15 minute", AT.replace(tzinfo=None))
    assert {(r["trip_id"], r["stop_id"]) for r in rows} == {("T", "S1"), ("T", "S3")}
    schedule.engine.dispose()
