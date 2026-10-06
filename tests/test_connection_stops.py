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
The same calls are played by a bus (stops) and a train (stations).
"""
from __future__ import annotations

from freezegun import freeze_time
import pytest

import feed_db
import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")

TRIPS = {
    "T1": [("A", "08:00"), ("C", "08:10"), ("D", "08:20")],
    "T2": [("C", "09:00"), ("D", "09:10")],
    "T3": [("A", "10:00"), ("C", "10:10")],
    "T4": [("C", "11:00"), ("D", "11:10"), ("E", "11:20"), ("C", "11:30")],
}
DAY = "2026-10-05"


def _feed(route_type):
    return {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nT,Town,http://t,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},Stop {s},45.0{n},5.0\n" for n, s in enumerate("ACDE")),
        "routes.txt": f"route_id,agency_id,route_short_name,route_type\nL,T,L,{route_type}\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"L,D,{trip},0\n" for trip in TRIPS),
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(
            f"{trip},{t}:00,{t}:00,{stop},{n}\n"
            for trip, calls in TRIPS.items() for n, (stop, t) in enumerate(calls, 1)),
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
        rows, _start = gtfs_helper._fetch_departure_rows(
            route_type, data["origin"], data["destination"], schedule, window=(DAY, DAY),
            **gtfs_helper.departure_query_args(data))
    return [(r["trip_id"], r["origin_stop_name"], str(r["origin_depart_time"])[:5], r["dest_stop_name"])
            for r in rows]


EXPECTED = [("T1", "Stop A", "08:00", "Stop D"),    # once, not A -> C nor C -> D
            ("T2", "Stop C", "09:00", "Stop D"),    # got on at the connection
            ("T3", "Stop A", "10:00", "Stop C"),    # got off at the connection
            ("T4", "Stop C", "11:00", "Stop D")]    # not round the loop to C


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
