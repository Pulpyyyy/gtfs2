"""A way asked offers the places its own rides set riders down at.

The destinations of a way (get_towards) were filtered on where any trip
through the origin sets riders down: a place the way's buses call at with
no way off was offered because the other way's buses set down there
(Zou's school runs towards Gare Routière; the 48-feed sweep, 2026-09-26).
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
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nO,Origin,45.0,1.0\nP,Pass,45.1,1.1\n"
                  "Q,Quebec,45.2,1.3\nX,Xray,45.3,1.4\nR,Romeo,45.2,0.9\nY,Yankee,45.3,0.8\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR1,A,1,One,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR1,S,T1,0\nR1,S,T2,0\n",
    "stop_times.txt": HEAD + (
        # towards Xray by Quebec: no way off at Pass
        "T1,08:00:00,08:00:00,O,1,0,0\nT1,08:10:00,08:10:00,P,2,0,1\n"
        "T1,08:20:00,08:20:00,Q,3,0,0\nT1,08:30:00,08:30:00,X,4,0,0\n"
        # towards Yankee by Romeo: Pass sets riders down
        "T2,09:00:00,09:00:00,O,1,0,0\nT2,09:10:00,09:10:00,P,2,0,0\n"
        "T2,09:20:00,09:20:00,R,3,0,0\nT2,09:30:00,09:30:00,Y,4,0,0\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def test_a_way_offers_only_its_own_set_downs(tmp_path):
    archive = tmp_path / "feed.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    archive.write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(archive))

    def listed(towards=None):
        found = gtfs_helper.get_destination_stop_list(schedule, "R1", None, "O", towards=towards)
        return sorted(str(s).split(":")[0] for s in found)

    try:
        assert sorted(way for way, _label in gtfs_helper.get_towards(schedule, "R1", "O")) == ["X", "Y"]
        assert listed("X") == ["Q", "X"]
        assert listed("Y") == ["P", "R", "Y"]
        # asked no way, every set-down of the line from here
        assert listed() == ["P", "Q", "R", "X", "Y"]
    finally:
        schedule.engine.dispose()
