"""A train entry on several lines and several stations at one end.

The train path held an entry to one line and one station at each end:
on a week of works (SNCF, October 2026) a K8+ train left from Les Aubrais,
not Orleans, and no "Orleans to Paris" sensor showed it, eight sensors to
cover what two should. An entry now lists its lines ("lines", [] for
every rail line; "line" alone on the entries made before) and may get on,
or off, at more than one station. A train calling at Orleans then at Les
Aubrais rides both pairs: it is listed once, where the rider first gets
on and last gets off.

The network: Orleans (O), Les Aubrais (A), Paris (P).
    K1  K8+  O 10:00, A 10:07, P 11:05
    K2  K8+  A 11:41, P 12:39            works: from Les Aubrais only
    P1  P8   O 12:00, A 12:07, P 13:30
    N1  560B A 16:30 sets down only, P 17:30
    K3  K8+  P 09:12, A 10:11            works: to Les Aubrais only
    K4  K8+  P 13:22, A 14:20, O 14:28
"""
from __future__ import annotations

import pytest
from freezegun import freeze_time

import feed_db
import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")
stations = ha_stub.load("stations")
stop_rules = ha_stub.load("stop_rules")

O, A, P = "Orleans", "Les Aubrais", "Paris Austerlitz"
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nS,SNCF,http://s,UTC\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nSO,Orleans,47.9,1.9\n"
                  "SA,Les Aubrais,47.92,1.9\nSP,Paris Austerlitz,48.8,2.36\n"),
    "routes.txt": ("route_id,agency_id,route_short_name,route_long_name,route_type\n"
                   "RK,S,K8+,Paris - Orleans,2\nRP,S,P8,Paris - Etampes - Orleans,2\n"
                   "RN,S,560B,Paris - Toulouse,2\n"),
    "trips.txt": ("route_id,service_id,trip_id,direction_id\nRK,D,K1,0\nRK,D,K2,0\nRP,D,P1,0\n"
                  "RN,D,N1,0\nRK,D,K3,1\nRK,D,K4,1\n"),
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
        "K1,10:00:00,10:00:00,SO,1,0,1\nK1,10:07:00,10:07:00,SA,2,0,0\nK1,11:05:00,11:05:00,SP,3,1,0\n"
        "K2,11:41:00,11:41:00,SA,1,0,1\nK2,12:39:00,12:39:00,SP,2,1,0\n"
        "P1,12:00:00,12:00:00,SO,1,0,1\nP1,12:07:00,12:07:00,SA,2,0,0\nP1,13:30:00,13:30:00,SP,3,1,0\n"
        "N1,16:30:00,16:30:00,SA,1,1,0\nN1,17:30:00,17:30:00,SP,2,1,0\n"
        "K3,09:12:00,09:12:00,SP,1,0,1\nK3,10:11:00,10:11:00,SA,2,1,0\n"
        "K4,13:22:00,13:22:00,SP,1,0,1\nK4,14:20:00,14:20:00,SA,2,0,0\nK4,14:28:00,14:28:00,SO,3,1,0\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}
DAY = "2026-10-05"


@pytest.fixture(scope="module")
def schedule(tmp_path_factory):
    built = feed_db.build(tmp_path_factory.mktemp("trains"), FEED)
    yield built
    built.engine.dispose()


def _rides(schedule, entry):
    """(trip, station got on at, time, station got off at) of the day's
    departures of a train entry."""
    with freeze_time(f"{DAY} 00:00:00"):
        rows, _start = gtfs_helper._fetch_departure_rows(
            "2", entry["origin"], entry["destination"], schedule, window=(DAY, DAY),
            **gtfs_helper.departure_query_args(entry))
    return [(r["trip_id"], r["origin_stop_name"], str(r["origin_depart_time"])[:5], r["dest_stop_name"])
            for r in rows]


def _entry(origin, destination, **extra):
    return {"origin": origin, "destination": destination, "route": "train", "route_type": "2", **extra}


def test_the_lines_an_entry_holds_to():
    assert stop_rules.entry_lines({"lines": ["K8+", "P8"], "line": None}) == ["K8+", "P8"]
    # every rail line, on an entry of this screen or before it
    assert stop_rules.entry_lines({"lines": [], "line": None}) == []
    assert stop_rules.entry_lines({}) == []
    # an entry made before: its one line
    assert stop_rules.entry_lines({"line": "K8+"}) == ["K8+"]
    assert stop_rules.entry_lines({"lines": None, "line": "K8+"}) == ["K8+"]


def test_the_sql_that_holds_a_query_to_lines():
    assert stop_rules.line_codes_where("r.route_short_name", None) == ("", {})
    assert stop_rules.line_codes_where("r.route_short_name", []) == ("", {})
    assert stop_rules.line_codes_where("r.route_short_name", "K8+") == (
        "AND r.route_short_name IN (:line_0)", {"line_0": "K8+"})
    # one scalar a code: the candidates' cache keys on them
    assert stop_rules.line_codes_where("r.route_short_name", ["K8+", "P8"]) == (
        "AND r.route_short_name IN (:line_0, :line_1)", {"line_0": "K8+", "line_1": "P8"})


def test_a_train_through_both_stations_is_listed_once_where_it_is_first_boarded(schedule):
    rides = _rides(schedule, _entry(O, P, origin_stations=[O, A], destination_stations=[P], lines=[]))
    # K1 and P1 call at both: once each, at Orleans; K2 from Les Aubrais;
    # N1 takes nobody on at Les Aubrais
    assert rides == [("K1", O, "10:00", P), ("K2", A, "11:41", P), ("P1", O, "12:00", P)]


def test_a_train_through_both_stations_is_listed_once_where_it_is_last_left(schedule):
    rides = _rides(schedule, _entry(P, O, origin_stations=[P], destination_stations=[O, A], lines=[]))
    # K3 ends at Les Aubrais; K4 goes on to Orleans, listed there
    assert rides == [("K3", P, "09:12", A), ("K4", P, "13:22", O)]


def test_the_lines_ticked_hold_the_departures(schedule):
    rides = _rides(schedule, _entry(O, P, origin_stations=[O, A], destination_stations=[P],
                                    lines=["P8"]))
    assert rides == [("P1", O, "12:00", P)]


def test_an_entry_made_before_reads_as_it_did(schedule):
    # one station a end, the line alone: no "lines", no station lists
    before = _rides(schedule, _entry(O, P, line="K8+"))
    ticked = _rides(schedule, _entry(O, P, line="K8+", lines=["K8+"],
                                     origin_stations=[O], destination_stations=[P]))
    every = _rides(schedule, _entry(O, P))
    assert before == [("K1", O, "10:00", P)]
    assert ticked == before
    # no line at all, as before: every rail line
    assert every == [("K1", O, "10:00", P), ("P1", O, "12:00", P)]


def test_the_options_screen_reads_the_stations_between_and_the_lines(schedule):
    between = stations.get_train_stations_between(schedule, O, P)
    lines = stations.get_train_lines_between(schedule, O, P, between, between)
    # from Les Aubrais too: K2 (K8+) is no new line, the 560B takes
    # nobody on there
    assert between == [A]
    assert lines == {"K8+": "Paris - Orleans", "P8": "Paris - Etampes - Orleans"}
    # every rail line boarding at Les Aubrais: to Paris, and to Orleans on K4
    assert stations.get_train_destination_list(schedule, None, A) == {O: {"train"}, P: {"train"}}
    assert stations.has_train_trip_between(schedule, [O, A], [P], ["K8+"])
    assert not stations.has_train_trip_between(schedule, [A], [P], ["560B"])
    assert A in stations.get_station_list(schedule)


def test_the_sensor_names_every_station_of_each_end():
    # the departure's own ends are the next train's alone; a card cuts the
    # leg from the first station to the last
    departure_attributes = ha_stub.load("departure_attributes")
    departure = {"origin_stop_name": A, "origin_stop_id": "SA", "destination_stop_name": P,
                 "destination_stop_id": "SP"}
    listed: dict = {}
    departure_attributes.station_attributes(listed, departure, None, None, None, "2",
                                            {"origin_stations": [O, A], "destination_stations": [P]})
    assert (listed["origin_stations"], listed["destination_stations"]) == ([O, A], [P])
    assert listed["origin_station_stop_name"] == A
    # an entry made before lists none, and its sensor says none
    before: dict = {}
    departure_attributes.station_attributes(before, departure, None, None, None, "2", {"origin": O})
    assert "origin_stations" not in before and "destination_stations" not in before
