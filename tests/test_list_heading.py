"""The stop list starts where the trips of direction 0 start.

A line's places are listed in riding order, and of its two ends the one
the trips labelled 0 leave from comes first: most of them ride the list
forward, weighed by how many run each pattern. Set here on a small feed
with the answer written out, rather than recomputed in the provider
sweep, which only checks the list against the rides.
"""
from __future__ import annotations

import pytest

import feed_db
import ha_stub

ha_stub.install()

places = ha_stub.load("places")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"


def _calls(trip, stops):
    return "".join(f"{trip},08:{n:02d}:00,08:{n:02d}:00,{s},{n}\n" for n, s in enumerate(stops, 1))


def _schedule(tmp_path, zero, one):
    """Two trips labelled 0 riding `zero`, one labelled 1 riding `one`."""
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nA,Alpha,45.0,1.0\n"
                      "B,Bravo,45.1,1.1\nC,Charlie,45.2,1.2\n"),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\nR,S,Z1,0\nR,S,Z2,0\nR,S,O1,1\n",
        "stop_times.txt": HEAD + _calls("Z1", zero) + _calls("Z2", zero) + _calls("O1", one),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    return feed_db.build(tmp_path, feed)


@pytest.mark.parametrize(("zero", "one", "listed"), [
    ("ABC", "CBA", ["A", "B", "C"]),
    ("CBA", "ABC", ["C", "B", "A"]),
])
def test_the_list_starts_where_direction_0_starts(tmp_path, zero, one, listed):
    schedule = _schedule(tmp_path, zero, one)
    try:
        stops = places.get_stop_list(schedule, "R", None)
        assert [str(s).split(":")[0] for s in stops] == listed
    finally:
        schedule.engine.dispose()
