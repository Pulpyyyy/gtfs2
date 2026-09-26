"""The ride between two places is the shortest one, over the calls a rider can use.

A trip passing a place twice offers the pair twice (Palm Bus 21 at Gare
SNCF de Cannes): the departure query keeps the ride with no other call at
either end in between. A call nobody may board or leave by counted as
one: London's Kennington, where a Northern line trip calls at the same
platform twice, the second time with pickup and drop-off forbidden, lost
the trip altogether (the 48-feed sweep, UK BODS). The first call could
not be used, the second was in the way.
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

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nK,Kennington,51.48,-0.10\nC,Camden,51.53,-0.14\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,N,Northern,1\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,LOOP,0\nR,D,TWICE,0\nR,D,SETDOWN,0\n",
    "stop_times.txt": HEAD + (
        # Kennington twice, the second call unusable: the ride leaves at 10:00
        "LOOP,10:00:00,10:00:00,K,1,0,0\nLOOP,10:01:00,10:01:00,K,2,1,1\n"
        "LOOP,10:20:00,10:20:00,C,3,0,0\n"
        # Kennington twice, both usable: the shortest ride leaves at 11:01
        "TWICE,11:00:00,11:00:00,K,1,0,0\nTWICE,11:01:00,11:01:00,K,2,0,0\n"
        "TWICE,11:20:00,11:20:00,C,3,0,0\n"
        # Camden twice, the first call set-down forbidden: the ride ends at 12:21
        "SETDOWN,12:00:00,12:00:00,K,1,0,0\nSETDOWN,12:20:00,12:20:00,C,2,0,1\n"
        "SETDOWN,12:21:00,12:21:00,C,3,0,0\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}

AT = datetime.datetime(2026, 9, 24, 9, 30, tzinfo=datetime.timezone.utc)


def _rides(tmp_path):
    archive = tmp_path / "feed.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    archive.write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(archive))
    dt_util.set_default_time_zone(dt_util.get_time_zone("UTC"))
    try:
        with freeze_time(AT):
            rows, _ = gtfs_helper._fetch_departure_rows(
                "1", "K: Kennington", "C: Camden", schedule, route="R")
    finally:
        schedule.engine.dispose()
    return {r["trip_id"]: (r["origin_depart_dt"][11:16], r["dest_arrival_dt"][11:16])
            for r in rows if r["origin_depart_dt"].startswith("2026-09-24")}


def test_a_call_nobody_can_use_is_not_in_the_way(tmp_path):
    rides = _rides(tmp_path)
    assert rides["LOOP"] == ("10:00", "10:20")
    assert rides["SETDOWN"] == ("12:00", "12:21")


def test_a_usable_call_between_still_makes_the_ride_shorter(tmp_path):
    assert _rides(tmp_path)["TWICE"] == ("11:01", "11:20")
