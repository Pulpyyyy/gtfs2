"""A loop's rotations tied in stops are told apart by time, over the calls a rider can use.

A trip leaving a loop's terminus reaches any stop of the loop both ways
round (TAO 22 at Zenith); when both rotations take as many stops, the
median ride time decides. A call nobody may leave by is no end of a ride,
as in the departure query (tests/test_departure_shortest_ride.py): timed
as one, it made its rotation look the quicker, and the entry kept a
direction whose departures never reach the destination that fast.
"""
from __future__ import annotations

import feed_db
import ha_stub

ha_stub.install()

places = ha_stub.load("places")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nZ,Zenith,47.0,0.0\n"
                  "P,Plissay,47.01,0.0\nM,Mairie,47.02,0.0\nQ,Quai,47.03,0.0\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,22,Loop,3\n",
    "trips.txt": ("route_id,service_id,trip_id,direction_id\n"
                  "R,D,CLOCK,0\nR,D,ANTI,1\nR,D,ANTI_EXPRESS,1\n"),
    "stop_times.txt": HEAD + (
        # one way round: Zenith to Mairie in 10 minutes
        "CLOCK,10:00:00,10:00:00,Z,1,0,0\nCLOCK,10:05:00,10:05:00,P,2,0,0\n"
        "CLOCK,10:10:00,10:10:00,M,3,0,0\nCLOCK,10:15:00,10:15:00,Q,4,0,0\n"
        "CLOCK,10:20:00,10:20:00,Z,5,0,0\n"
        # the other way round: 12 minutes
        "ANTI,11:00:00,11:00:00,Z,1,0,0\nANTI,11:06:00,11:06:00,Q,2,0,0\n"
        "ANTI,11:12:00,11:12:00,M,3,0,0\nANTI,11:18:00,11:18:00,P,4,0,0\n"
        "ANTI,11:24:00,11:24:00,Z,5,0,0\n"
        # the same stops that way, passing Mairie in 2 minutes with no way off
        "ANTI_EXPRESS,12:00:00,12:00:00,Z,1,0,0\nANTI_EXPRESS,12:01:00,12:01:00,Q,2,0,0\n"
        "ANTI_EXPRESS,12:02:00,12:02:00,M,3,0,1\nANTI_EXPRESS,12:03:00,12:03:00,P,4,0,0\n"
        "ANTI_EXPRESS,12:04:00,12:04:00,Z,5,0,0\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def _schedule(tmp_path):
    return feed_db.build(tmp_path, FEED)


def test_a_call_nobody_can_leave_by_does_not_time_a_rotation(tmp_path):
    schedule = _schedule(tmp_path)
    try:
        # the rider leaving Zenith reaches Mairie in 10 minutes one way
        # round, 12 the other: the 2 minutes of the express are no ride
        assert places.get_pair_direction(schedule, "R", "Z", "M") == "0"
    finally:
        schedule.engine.dispose()
