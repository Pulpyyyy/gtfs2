"""The lines a train entry's departures rode, read back from its database.

A train entry stores "train" for its line and rides whatever line serves
its two stations; its map files are named after those lines, and its
removal left them behind for good. The lines are read back from the
source's database when the entry goes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pygtfs

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "sncf" / "static.zip"


def _database(tmp_path):
    schedule = pygtfs.Schedule(str(tmp_path / "sncf.sqlite"))
    pygtfs.append_feed(schedule, str(FEED))
    schedule.engine.dispose()
    return tmp_path / "sncf.sqlite"


def test_the_lines_between_the_two_stations(tmp_path):
    db = _database(tmp_path)
    conn = sqlite3.connect(db)
    # a pair of stations one trip rides in that order, and the lines doing so
    origin, destination = conn.execute(
        "select so.stop_name, sd.stop_name from stop_times o "
        "join stop_times d on d.trip_id = o.trip_id and d.stop_sequence > o.stop_sequence "
        "join stops so on so.stop_id = o.stop_id join stops sd on sd.stop_id = d.stop_id "
        "where so.stop_name <> sd.stop_name limit 1").fetchone()
    expected = {r[0] for r in conn.execute(
        "select distinct t.route_id from trips t join stop_times o on o.trip_id = t.trip_id "
        "join stops so on so.stop_id = o.stop_id join stop_times d on d.trip_id = t.trip_id "
        "join stops sd on sd.stop_id = d.stop_id where so.stop_name = ? and sd.stop_name = ? "
        "and o.stop_sequence < d.stop_sequence", (origin, destination))}
    every = {r[0] for r in conn.execute("select route_id from routes")}
    conn.close()
    data = {"file": "sncf", "route": "train", "origin": origin, "destination": destination}
    got = set(gtfs_helper.train_entry_routes(str(tmp_path), data))
    assert got == expected and got
    assert got < every          # not every line of the source


def test_no_database_no_lines(tmp_path):
    assert gtfs_helper.train_entry_routes(str(tmp_path), {"file": "gone", "origin": "A",
                                                          "destination": "B"}) == []
