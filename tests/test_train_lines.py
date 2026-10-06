"""A train entry on several lines and several stations at one end.

The train path held an entry to one line and one station at each end:
on a week of works (SNCF, October 2026) a K8+ train left from Les Aubrais,
not Orleans, and no "Orleans to Paris" sensor showed it, eight sensors to
cover what two should. An entry now lists its lines ("lines", [] for
every rail line; "line" alone on the entries made before) and may get on,
or off, at more than one station. A train calling at Orleans then at Les
Aubrais rides both pairs: it is listed once, where the rider first gets
on and last gets off. The flow makes one entry a line ticked, with the
stations its own trains serve (train_line_ends).

The network: Orleans (O), Les Aubrais (A), Paris (P).
    K1  K8+  O 10:00, A 10:07, P 11:05
    K2  K8+  A 11:41, P 12:39            works: from Les Aubrais only
    P1  P8   O 12:00, A 12:07, P 13:30
    N1  560B A 16:30 sets down only, P 17:30
    K3  K8+  P 09:12, A 10:11            works: to Les Aubrais only
    K4  K8+  P 13:22, A 14:20, O 14:28
"""
from __future__ import annotations

import os

import pytest
from freezegun import freeze_time
from sqlalchemy import text

import feed_db
import ha_stub

ha_stub.install()

const = ha_stub.load("const")
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
    assert const.entry_lines({"lines": ["K8+", "P8"], "line": None}) == ["K8+", "P8"]
    # every rail line, on an entry of this screen or before it
    assert const.entry_lines({"lines": [], "line": None}) == []
    assert const.entry_lines({}) == []
    # an entry made before: its one line
    assert const.entry_lines({"line": "K8+"}) == ["K8+"]
    assert const.entry_lines({"lines": None, "line": "K8+"}) == ["K8+"]


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


def test_a_line_the_feed_names_nothing_is_named_by_its_ends(tmp_path):
    # SNCF files its night trains under "INCONNU", long name " -": the
    # options screen read "INCONNU (-)"
    feed = {**FEED,
            "stops.txt": FEED["stops.txt"] + "ST,Tarbes,43.2,0.07\n",
            "routes.txt": FEED["routes.txt"] + "RX,S,INCONNU, -,2\n",
            "trips.txt": FEED["trips.txt"] + "RX,D,X1,0\n",
            "stop_times.txt": FEED["stop_times.txt"] + (
                "X1,21:00:00,21:00:00,ST,1,0,1\nX1,29:30:00,29:30:00,SO,2,0,0\n"
                "X1,30:30:00,30:30:00,SP,3,1,0\n")}
    night = feed_db.build(tmp_path, feed)
    try:
        lines = stations.get_train_lines_between(night, O, P, [], [])
    finally:
        night.engine.dispose()
    assert lines["INCONNU"] == "Paris Austerlitz ↔ Tarbes"
    assert lines["K8+"] == "Paris - Orleans"


def test_the_stations_between_follow_the_ride_not_the_alphabet():
    # Orleans to Paris: the stopping train calls at each station, the
    # express skips Chevilly and Artenay; the screen lists them as ridden
    stopping = [A, "Chevilly", "Artenay", "Toury"]
    express = [A, "Toury"]
    assert stations.riding_order([express, stopping]) == stopping
    # two rides read each other's way round: every station still once
    assert sorted(stations.riding_order([["X", "Y"], ["Y", "X"]])) == ["X", "Y"]


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


def test_each_line_ticked_keeps_the_stations_its_trains_serve(schedule):
    # one sensor a line, with the stations of each end its own trains call
    # at, in the order ticked: K8+ (K1, K2) and P8 (P1) board at both
    assert stations.train_line_ends(schedule, [O, A], [P], "K8+") == ([O, A], [P])
    assert stations.train_line_ends(schedule, [O, A], [P], "P8") == ([O, A], [P])
    # the 560B takes nobody on at Les Aubrais: no sensor of it
    assert stations.train_line_ends(schedule, [O, A], [P], "560B") == ([], [])
    # the way back, read on its own: K3 ends at Les Aubrais, K4 goes on;
    # the P8 runs one way only
    assert stations.train_line_ends(schedule, [P], [O, A], "K8+") == ([P], [O, A])
    assert stations.train_line_ends(schedule, [P], [O, A], "P8") == ([], [])
    # a line the feed gives no code: every rail line
    assert stations.train_line_ends(schedule, [A, O], [P], None) == ([A, O], [P])


def test_the_trains_of_the_zip_answer_as_the_source_does(schedule):
    # a source holds the lines asked for; the stations picked first are
    # read from the trains of the zip kept beside it, with the same questions
    index = stations.rail_index(str(schedule.engine.url.database).replace("feed.sqlite", "feed.zip"))
    try:
        assert stations.get_station_list(index) == stations.get_station_list(schedule)
        assert stations.get_train_destination_list(index, None, A) == {O: {"train"}, P: {"train"}}
        assert stations.get_train_stations_between(index, O, P) == [A]
    finally:
        index.engine.dispose()


def test_the_stations_picked_first_import_the_lines_of_both_ways(schedule):
    # the return is the mirror: the lines riding back count too, the ones
    # riding out first (an import stops at the first line that fails)
    index = stations.rail_index(str(schedule.engine.url.database).replace("feed.sqlite", "feed.zip"))
    try:
        assert stations.train_routes_both_ways(index, O, P) == ["RK", "RP"]
        # from Les Aubrais: K2 and P1 out, K3 and K4 back; the 560B takes
        # nobody on there
        assert stations.train_routes_both_ways(index, A, P) == ["RK", "RP"]
        assert stations.train_routes_both_ways(index, P, A) == ["RK", "RP"]
    finally:
        index.engine.dispose()


def test_a_zip_that_cannot_be_read_gives_no_index(tmp_path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    assert stations.rail_index(str(broken)) is None
    assert stations.rail_index(str(tmp_path / "gone.zip")) is None
    # nothing kept beside it
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.zip"]


def test_the_index_is_read_once_an_edition(schedule, tmp_path, monkeypatch):
    # minutes on a national feed: kept beside the zip, read again only
    # when the zip is another edition
    zip_path = tmp_path / "feed.zip"
    zip_path.write_bytes(open(str(schedule.engine.url.database).replace("feed.sqlite", "feed.zip"), "rb").read())
    built = []
    read = stations._read_rail_tables
    monkeypatch.setattr(stations, "_read_rail_tables", lambda path: built.append(path) or read(path))
    for _ in range(2):
        index = stations.rail_index(str(zip_path))
        assert stations.get_train_stations_between(index, O, P) == [A]
        index.engine.dispose()
    assert len(built) == 1
    assert (tmp_path / "feed.zip.rail").exists() and not (tmp_path / "feed.zip.rail.new").exists()
    # a new edition of the zip: read again
    os.utime(zip_path, ns=(1, 1))
    index = stations.rail_index(str(zip_path))
    index.engine.dispose()
    assert len(built) == 2


def test_a_refreshed_source_reads_its_trains_again_only_when_read_before(schedule, tmp_path):
    zip_path = tmp_path / "feed.zip"
    zip_path.write_bytes(open(str(schedule.engine.url.database).replace("feed.sqlite", "feed.zip"), "rb").read())
    # no flow ever read its trains: nothing to keep fresh
    assert stations.refresh_rail_index(str(zip_path)) is False
    assert not (tmp_path / "feed.zip.rail").exists()
    stations.rail_index(str(zip_path)).engine.dispose()
    # the same edition: nothing to read
    assert stations.refresh_rail_index(str(zip_path)) is False
    # a new one: read again, and the station screens open on it at once
    os.utime(zip_path, ns=(2, 2))
    assert stations.refresh_rail_index(str(zip_path)) is True
    assert stations._index_stamp(str(zip_path) + ".rail") == stations._zip_stamp(str(zip_path))


def test_the_index_keeps_trips_by_number(schedule, tmp_path, monkeypatch):
    # a national feed's trip ids, a hundred characters on every call and
    # in their index, made the file 172 MB beside a 5.6 MB zip (SNCF): the
    # calls join their trip by a number, the answers are the same
    zip_path = tmp_path / "feed.zip"
    zip_path.write_bytes(open(str(schedule.engine.url.database).replace("feed.sqlite", "feed.zip"), "rb").read())
    index = stations.rail_index(str(zip_path))
    assert stations.get_train_stations_between(index, O, P) == [A]
    with index.engine.connect() as conn:
        kinds = {row[0] for row in conn.execute(text(
            "select typeof(trip_id) from stop_times union select typeof(trip_id) from trips"))}
    index.engine.dispose()
    assert kinds == {"integer"}
    # an index of the layout before is read again, not opened as it is
    built = []
    read = stations._read_rail_tables
    monkeypatch.setattr(stations, "_read_rail_tables", lambda path: built.append(path) or read(path))
    monkeypatch.setattr(stations, "RAIL_INDEX_LAYOUT", stations.RAIL_INDEX_LAYOUT + 1)
    stations.rail_index(str(zip_path)).engine.dispose()
    assert len(built) == 1


def test_the_kept_index_is_not_a_source(tmp_path):
    # the folder's lists of sources read .sqlite and .zip names only
    assert not stations.RAIL_INDEX_SUFFIX.endswith((".sqlite", ".zip"))
