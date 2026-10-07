"""A stop ticked at both ends of a journey is a connection.

"Also board at" and "Also get off at" may name the same stop or station:
the rider changes there, getting on or off as each run allows. Each run is
still listed once, from where the rider first gets on to where they last
get off, and a run that comes back to the connection is not a ride from
it to itself.

The line, from A to D with C on the way (E only on the loop):
    T1  A 08:00, C 08:10, D 08:20            through the connection
    T2  C 09:00, D 09:10                     starts at the connection
    T3  A 10:00, C 10:10                     ends at the connection
    T4  C 11:00, D 11:10, E 11:20, C 11:30   a loop back to the connection
    T5  A 12:00, D 12:20                     does not call at the connection
The same calls are played by a bus (stops) and a train (stations).
"""
from __future__ import annotations

import datetime

from freezegun import freeze_time
import pytest

import feed_db
import ha_stub

ha_stub.install()

departures = ha_stub.load("data.departures")

TRIPS = {
    "T1": [("A", "08:00"), ("C", "08:10"), ("D", "08:20")],
    "T2": [("C", "09:00"), ("D", "09:10")],
    "T3": [("A", "10:00"), ("C", "10:10")],
    "T4": [("C", "11:00"), ("D", "11:10"), ("E", "11:20"), ("C", "11:30")],
    "T5": [("A", "12:00"), ("D", "12:20")],
}
DAY = "2026-10-05"


def _feed(route_type, trips=TRIPS, stops="ACDE"):
    return {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nT,Town,http://t,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},Stop {s},45.0{n},5.0\n" for n, s in enumerate(stops)),
        "routes.txt": f"route_id,agency_id,route_short_name,route_type\nL,T,L,{route_type}\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"L,D,{trip},0\n" for trip in trips),
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(
            f"{trip},{t}:00,{t}:00,{stop},{n}\n"
            for trip, calls in trips.items() for n, (stop, t) in enumerate(calls, 1)),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
    }


@pytest.fixture(scope="module")
def bus(tmp_path_factory):
    built = feed_db.build(tmp_path_factory.mktemp("bus"), _feed(3))
    yield built
    built.engine.dispose()


@pytest.fixture(scope="module")
def train(tmp_path_factory):
    built = feed_db.build(tmp_path_factory.mktemp("train"), _feed(2))
    yield built
    built.engine.dispose()


def _rides(schedule, route_type, data):
    """(trip, got on at, time, got off at) of the day's departures."""
    with freeze_time(f"{DAY} 00:00:00"):
        rows, _start = departures._fetch_departure_rows(
            route_type, data["origin"], data["destination"], schedule, window=(DAY, DAY),
            **departures.departure_query_args(data))
    return [(r["trip_id"], r["origin_stop_name"], str(r["origin_depart_time"])[:5], r["dest_stop_name"])
            for r in rows]


EXPECTED = [("T1", "Stop A", "08:00", "Stop D"),    # once, not A -> C nor C -> D
            ("T2", "Stop C", "09:00", "Stop D"),    # got on at the connection
            ("T3", "Stop A", "10:00", "Stop C"),    # got off at the connection
            ("T4", "Stop C", "11:00", "Stop D"),    # not round the loop to C
            ("T5", "Stop A", "12:00", "Stop D")]    # passes the connection by


def test_a_bus_stop_at_both_ends_is_a_connection(bus):
    a, c, d = "A: Stop A", "C: Stop C", "D: Stop D"
    data = {"route": "L: L", "route_type": "3", "origin": a, "destination": d,
            "origin_stations": [a, c], "destination_stations": [d, c]}
    assert _rides(bus, "3", data) == EXPECTED


def test_a_station_at_both_ends_is_a_connection(train):
    data = {"route": "train", "route_type": "2", "origin": "Stop A", "destination": "Stop D",
            "origin_stations": ["Stop A", "Stop C"], "destination_stations": ["Stop D", "Stop C"]}
    assert _rides(train, "2", data) == EXPECTED


def test_without_a_connection_the_ends_read_as_they_did(bus, train):
    # C a boarding stop only: T3, which ends there, is no ride to D
    bus_data = {"route": "L: L", "route_type": "3", "origin": "A: Stop A", "destination": "D: Stop D",
                "origin_stations": ["A: Stop A", "C: Stop C"], "destination_stations": ["D: Stop D"]}
    train_data = {"route": "train", "route_type": "2", "origin": "Stop A", "destination": "Stop D",
                  "origin_stations": ["Stop A", "Stop C"], "destination_stations": ["Stop D"]}
    without_t3 = [ride for ride in EXPECTED if ride[0] != "T3"]
    assert _rides(bus, "3", bus_data) == without_t3
    assert _rides(train, "2", train_data) == without_t3


def test_each_run_says_where_it_sets_the_rider_down(bus):
    # the sensor's list beside next_departures: a card tells T3, which
    # ends at the connection, from the runs reaching the destination
    a, c, d = "A: Stop A", "C: Stop C", "D: Stop D"
    data = {"route": "L: L", "route_type": "3", "origin": a, "destination": d,
            "origin_stations": [a, c], "destination_stations": [d, c]}
    with freeze_time(f"{DAY} 00:00:00"):
        rows, _start = departures._fetch_departure_rows(
            "3", a, d, bus, window=(DAY, DAY), **departures.departure_query_args(data))
    at = datetime.datetime(2026, 10, 5, tzinfo=datetime.timezone.utc)
    lists = departures._next_departure_lists([(at, row) for row in rows], datetime.timezone.utc)
    assert lists["next_departures_origin_stop_id"] == ["A", "C", "A", "C", "A"]
    assert lists["next_departures_destination_stop_id"] == ["D", "D", "C", "D", "D"]


# Two connections, B and C, on the way from A to D. A run the other way
# rides from C to B, both at each end: listed in the journey A -> D until
# 2026-10-07 (Palm Bus 2, Gare SNCF de Cannes to Meridien by Hotel de Ville
# and Les Pins, listed the runs back from Les Pins to Hotel de Ville)
WAYS = {
    "W1": [("A", "08:00"), ("B", "08:10"), ("C", "08:20"), ("D", "08:30")],
    "W2": [("D", "09:00"), ("C", "09:10"), ("B", "09:20"), ("A", "09:30")],
    "W3": [("B", "10:00"), ("C", "10:10")],
    "W4": [("C", "11:00"), ("B", "11:10")],
}
BOTH_WAYS = [("W1", "Stop A", "08:00", "Stop D"),   # A to D, once
             ("W3", "Stop B", "10:00", "Stop C"),   # between the connections, the journey's way
             # between them the other way, at neither end: nothing in the
             # run tells its way, still listed
             ("W4", "Stop C", "11:00", "Stop B")]   # W2 passes D before C: not a ride


@pytest.mark.parametrize("route_type", ["3", "2"])
def test_a_run_the_other_way_between_two_connections_is_no_ride(tmp_path, route_type):
    built = feed_db.build(tmp_path, _feed(int(route_type), WAYS, "ABCD"))
    try:
        if route_type == "3":
            a, b, c, d = "A: Stop A", "B: Stop B", "C: Stop C", "D: Stop D"
            data = {"route": "L: L", "route_type": "3", "origin": a, "destination": d,
                    "origin_stations": [a, b, c], "destination_stations": [d, b, c]}
        else:
            data = {"route": "train", "route_type": "2", "origin": "Stop A", "destination": "Stop D",
                    "origin_stations": ["Stop A", "Stop B", "Stop C"],
                    "destination_stations": ["Stop D", "Stop B", "Stop C"]}
        assert _rides(built, route_type, data) == BOTH_WAYS
    finally:
        built.engine.dispose()
