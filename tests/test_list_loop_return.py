"""A line out by one road and back by another lists its way back at the end.

The list is laid one ride at a time, and the places a ride brings go after
the place preceding them. A ride back that leaves the way out part way and
rides on, by its own road, to the line's first place closes a loop: its
places belong where the list wraps round, at the end. They were slotted
right after the place they leave from, in the middle of the way out, and
the other ride back, from the far end, then crossed the list twice: GTT
4155E, out from Samone to Burolo by one road, back by another. Set here on
a small feed with the answer written out.
"""
from __future__ import annotations

import feed_db
import ha_stub

ha_stub.install()

places = ha_stub.load("places")

RIDES = [
    ("OUT", 0, "SPQABCDE"),      # out by the main road, S to the end E
    ("OUT2", 0, "ZYXBCDE"),      # out from Z, joining it at B
    ("RET1", 1, "BCDRTZ"),       # back from the road by another, R and T, to Z
    ("RET2", 1, "EFRTS"),        # back from the end by it, to S
]


def _schedule(tmp_path):
    stops = sorted({s for _trip, _way, calls in RIDES for s in calls})
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},Stop {s},{45 + n / 100:.2f},{1 + n / 100:.2f}\n" for n, s in enumerate(stops)),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,S,{trip},{way}\n" for trip, way, _calls in RIDES),
        "stop_times.txt": feed_db.STOP_TIMES + "".join(
            feed_db.calls(trip, calls) for trip, _way, calls in RIDES),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    return feed_db.build(tmp_path, feed)


def test_the_way_back_by_another_road_comes_after_the_way_out(tmp_path):
    schedule = _schedule(tmp_path)
    try:
        stops = places.get_stop_list(schedule, "R", None)
        # each ride reads it one way, wrapping round once at most
        assert "".join(str(s).split(":")[0] for s in stops) == "ZYXSPQABCDEFRT"
    finally:
        schedule.engine.dispose()
