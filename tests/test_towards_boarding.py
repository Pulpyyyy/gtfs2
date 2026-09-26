"""The ways out of a stop hold every trip of a pattern, not its sample.

The line's rides are read from one trip per pattern of stops, the one
with the smallest id. Whether a rider can get on at the origin was read
from that trip's own call: when it only sets down there and the other
trips of the pattern pick up, the pattern's way out was lost (Amtrak's
Stockton: one Thruway bus of three does not pick up, and the way to
Sacramento went missing; the 48-feed sweep, 2026-09-26).
"""
from __future__ import annotations

import io
import zipfile

import pygtfs

import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nA,Alpha,45.0,1.0\nB,Bravo,45.1,1.1\n"
                  "C,Charlie,45.2,1.2\nD,Delta,45.3,1.0\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,S,T1,0\nR,S,T2,0\nR,S,T3,0\n",
    "stop_times.txt": HEAD + (
        # to Charlie: T1, the pattern's sample, only sets down at Bravo
        "T1,08:00:00,08:00:00,A,1,0,0\nT1,08:10:00,08:10:00,B,2,1,0\nT1,08:20:00,08:20:00,C,3,0,0\n"
        "T2,09:00:00,09:00:00,A,1,0,0\nT2,09:10:00,09:10:00,B,2,0,0\nT2,09:20:00,09:20:00,C,3,0,0\n"
        # to Delta
        "T3,10:00:00,10:00:00,A,1,0,0\nT3,10:10:00,10:10:00,B,2,0,0\nT3,10:20:00,10:20:00,D,3,0,0\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def test_a_way_whose_sample_only_sets_down_is_still_offered(tmp_path):
    archive = tmp_path / "feed.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    archive.write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(archive))
    try:
        ways = gtfs_helper.get_towards(schedule, "R", "B")
        assert sorted(way for way, _label in ways) == ["C", "D"]
        # and each way keeps its own destinations
        for way, place in (("C", "C"), ("D", "D")):
            listed = gtfs_helper.get_destination_stop_list(schedule, "R", None, "B", towards=way)
            assert [str(s).split(":")[0] for s in listed] == [place]
    finally:
        schedule.engine.dispose()
