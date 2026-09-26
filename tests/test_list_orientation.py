"""A ride is laid on the stop list the way most of its places run it.

The list is built from the longest ride, and every other ride is read
forward or backward along it, whichever way the places it shares with the
list agree, before its own places are slotted in. A way back that rides a
one-way loop in the outbound sense (Autolinee Toscane 93 round Albereto and
Val di Denari) makes many short steps up the list after a few long ones
down it: counted step by step it reads forward, and its own stop lands on
the wrong side of its neighbours. Counted over every pair of its places it
reads backward. Set here on a small feed with the answer written out.
"""
from __future__ import annotations

import io
import zipfile

import pygtfs

import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
STOPS = "ABCDEFGHIJKW"


def _calls(trip, stops):
    return "".join(f"{trip},08:{n:02d}:00,08:{n:02d}:00,{s},{n}\n" for n, s in enumerate(stops, 1))


def _schedule(tmp_path, out, back):
    """One trip labelled 0 riding `out`, one labelled 1 riding `back`."""
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},Stop {s},{45 + n / 100:.2f},{1 + n / 100:.2f}\n" for n, s in enumerate(STOPS)),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\nR,S,OUT,0\nR,S,BACK,1\n",
        "stop_times.txt": HEAD + _calls("OUT", out) + _calls("BACK", back),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in feed.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    return schedule


def test_a_way_back_through_a_one_way_loop_keeps_its_stop_between_its_neighbours(tmp_path):
    # back: K J W I down the list, then B C D E F up it, round the loop
    schedule = _schedule(tmp_path, "ABCDEFGHIJK", "KJWIBCDEF")
    try:
        stops = gtfs_helper.get_stop_list(schedule, "R", None)
        assert [str(s).split(":")[0] for s in stops] == list("ABCDEFGHIWJK")
    finally:
        schedule.engine.dispose()
