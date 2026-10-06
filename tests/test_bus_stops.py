"""A bus or tram entry getting on, or off, at more than one stop.

As a train entry gets on at Orleans or at Les Aubrais, a bus entry may
list the stops of each end it takes ("origin_stations" and
"destination_stations", the entry's own "stop_id: name" form): each run
is listed once, where the rider first gets on and last gets off. One stop
a end reads as it always did, the shortest ride of a loop included.

The line B: Home A (HA), Home B (HB), Market (M), Work (W).
    B1  HA 08:00, HB 08:05, M 08:10, W 08:20      through both homes
    B2  HB 09:00, M 09:10, W 09:20                 from Home B only
    B3  HA 10:00, W 10:10, HA 10:20, HB 10:25, W 10:35   a loop
    B4  HA 11:00 sets down only, HB 11:05, W 11:15
"""
from __future__ import annotations

from freezegun import freeze_time
import pytest

import feed_db
import ha_stub

ha_stub.install()

departures = ha_stub.load("data.departures")

HA, HB, W = "HA: Home A", "HB: Home B", "W: Work"
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nT,Town,http://t,UTC\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nHA,Home A,45.0,5.0\nHB,Home B,45.01,5.0\n"
                  "M,Market,45.02,5.0\nW,Work,45.03,5.0\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nB,T,B,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nB,D,B1,0\nB,D,B2,0\nB,D,B3,0\nB,D,B4,0\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
        "B1,08:00:00,08:00:00,HA,1,0,0\nB1,08:05:00,08:05:00,HB,2,0,0\nB1,08:10:00,08:10:00,M,3,0,0\n"
        "B1,08:20:00,08:20:00,W,4,0,0\n"
        "B2,09:00:00,09:00:00,HB,1,0,0\nB2,09:10:00,09:10:00,M,2,0,0\nB2,09:20:00,09:20:00,W,3,0,0\n"
        "B3,10:00:00,10:00:00,HA,1,0,0\nB3,10:10:00,10:10:00,W,2,0,0\nB3,10:20:00,10:20:00,HA,3,0,0\n"
        "B3,10:25:00,10:25:00,HB,4,0,0\nB3,10:35:00,10:35:00,W,5,0,0\n"
        "B4,11:00:00,11:00:00,HA,1,1,0\nB4,11:05:00,11:05:00,HB,2,0,0\nB4,11:15:00,11:15:00,W,3,0,0\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}
DAY = "2026-10-05"


@pytest.fixture(scope="module")
def schedule(tmp_path_factory):
    built = feed_db.build(tmp_path_factory.mktemp("bus"), FEED)
    yield built
    built.engine.dispose()


def _rides(schedule, **entry):
    """(trip, stop got on at, time, stop got off at) of the day's departures."""
    data = {"route": "B: B", "route_type": "3", "origin": HA, "destination": W, **entry}
    with freeze_time(f"{DAY} 00:00:00"):
        rows, _start = departures._fetch_departure_rows(
            "3", data["origin"], data["destination"], schedule, window=(DAY, DAY),
            **departures.departure_query_args(data))
    return [(r["trip_id"], r["origin_stop_name"], str(r["origin_depart_time"])[:5], r["dest_stop_name"])
            for r in rows]


def test_one_stop_a_end_reads_as_it_did(schedule):
    # the loop's two rides from Home A, each the shortest; B4 takes nobody on there
    assert _rides(schedule) == [("B1", "Home A", "08:00", "Work"), ("B3", "Home A", "10:00", "Work"),
                                ("B3", "Home A", "10:20", "Work")]
    assert _rides(schedule, origin_stations=[HA], destination_stations=[W]) == _rides(schedule)


def test_a_run_through_both_stops_is_listed_once_where_it_is_first_boarded(schedule):
    rides = _rides(schedule, origin_stations=[HA, HB], destination_stations=[W])
    # B1 once, at Home A; B2 from Home B; the loop's two rides from Home A,
    # not a third from Home B; B4 from Home B, where it takes riders on
    assert rides == [("B1", "Home A", "08:00", "Work"), ("B2", "Home B", "09:00", "Work"),
                     ("B3", "Home A", "10:00", "Work"), ("B3", "Home A", "10:20", "Work"),
                     ("B4", "Home B", "11:05", "Work")]


def test_a_run_is_listed_once_where_it_is_last_left(schedule):
    rides = _rides(schedule, origin=HB, destination=W, origin_stations=[HB],
                   destination_stations=["M: Market", W])
    # got off at Work, the last of the two, not at Market
    assert rides == [("B1", "Home B", "08:05", "Work"), ("B2", "Home B", "09:00", "Work"),
                     ("B3", "Home B", "10:25", "Work"), ("B4", "Home B", "11:05", "Work")]


def test_the_sensor_names_every_stop_of_each_end():
    # by their names, as a train's stations: a card cuts the leg on them;
    # the flow's " #n" told two places of one name apart, not the rider
    attributes = ha_stub.load("domain.attributes")
    listed: dict = {}
    attributes.station_attributes(listed, {}, None, None, None, "3", {
        "origin_stations": ["HA: Home A (1)", "HB: Home B #2 (2)"], "destination_stations": ["W: Work (4)"]})
    assert (listed["origin_stations"], listed["destination_stations"]) == (["Home A", "Home B"], ["Work"])
    # an entry made before lists none, and its sensor says none
    before: dict = {}
    attributes.station_attributes(before, {}, None, None, None, "3", {"origin": HA})
    assert "origin_stations" not in before
