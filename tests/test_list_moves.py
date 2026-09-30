"""A place laid on the stop list moves when the rides say it sits elsewhere.

The list is built one ride at a time, the longest first, and a stop a ride
brings is slotted after the place preceding it. When that ride knows
nothing of the places between its neighbours, the slot is a guess: on Rome
8, the long outbound ride brings a pole of Gianicolense/Colli Portuensi
without the two stops the way back passes there, and it landed after them.
A shorter ride, laid later, calls at it before them. Once the list is laid,
each place moves to the slot the rides through it contradict least. Set
here on a small feed with the answer written out.
"""
from __future__ import annotations

import feed_db
import ha_stub

ha_stub.install()

places = ha_stub.load("places")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
STOPS = "ABCDEFGHX"


def _calls(trip, stops):
    return "".join(f"{trip},08:{n:02d}:00,08:{n:02d}:00,{s},{n}\n" for n, s in enumerate(stops, 1))


def _schedule(tmp_path, rides):
    """One trip per (trip_id, direction_id, stops) of rides."""
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},Stop {s},{45 + n / 100:.2f},{1 + n / 100:.2f}\n" for n, s in enumerate(STOPS)),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,S,{trip},{way}\n" for trip, way, _stops in rides),
        "stop_times.txt": HEAD + "".join(_calls(trip, stops) for trip, _way, stops in rides),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    return feed_db.build(tmp_path, feed)


def test_a_stop_a_long_ride_brings_moves_where_a_short_one_calls_at_it(tmp_path):
    schedule = _schedule(tmp_path, [
        ("BACK", 1, "HGFEDCBA"),    # the longest, laid first, never calls at X
        ("LONG", 0, "AXDEFGH"),     # brings X between A and D, knows neither B nor C
        ("SHORT", 0, "AXBCD"),      # calls at X before B and C
    ])
    try:
        stops = places.get_stop_list(schedule, "R", None)
        assert [str(s).split(":")[0] for s in stops] == list("AXBCDEFGH")
    finally:
        schedule.engine.dispose()
