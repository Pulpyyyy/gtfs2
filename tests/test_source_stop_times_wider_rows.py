"""A table whose rows carry more cells than its header names still imports.

Riga's feed (data.gov.lv, marsrutusaraksti10_2026.zip, 2026-11-14) names 7
columns in stop_times.txt and every one of its rows carries 8: a timepoint
column the header forgot, 0 throughout. pygtfs reads each row into the
header's columns and stopped the whole import on the first row, "Expected
7 arguments, got 8": no line of the network could be followed. The filter
the import runs first now cuts such rows to the header; the whole-feed
import, which hands pygtfs the kept zip itself, reads a copy cut the same
way when a row is wider, and the zip itself when none is.
"""
from __future__ import annotations

import sqlite3
import zipfile

import ha_stub

zip_filter = ha_stub.load("data.zip_filter")
source_zip = ha_stub.load("data.source_zip")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nRS,Rigas Satiksme,http://r,Europe/Riga\n",
    "routes.txt": ("route_id,route_short_name,route_long_name,route_type\n"
                   "riga_trol_3,3,Centraltirgus - Sarkandaugava,11\nriga_bus_2,2,Abrenes iela - Vecmilgravis,3\n"),
    "trips.txt": "route_id,service_id,trip_id\nriga_trol_3,S,T1\nriga_bus_2,S,T2\n",
    # 7 columns named, 8 cells a row
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence,pickup_type,drop_off_type\n"
                       "T1,08:00:00,08:00:00,S1,1,0,0,0\nT1,08:10:00,08:10:00,S2,2,0,0,0\n"
                       "T2,09:00:00,09:00:00,S1,1,0,0,0\nT2,09:20:00,09:20:00,S3,2,0,0,0\n"),
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nS1,Centraltirgus,56.94,24.11\n"
                  "S2,Sarkandaugava,56.99,24.13\nS3,Vecmilgravis,57.03,24.10\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20261101,20261231\n"),
}


def _zip(path):
    with zipfile.ZipFile(path, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    return path


def _imported(tmp_path, only_routes):
    _zip(tmp_path / "riga.zip")
    scratch = str(tmp_path / "riga.import.sqlite")
    ok = source_zip.build_scratch_database(str(tmp_path), "riga.zip", scratch,
                                           only_routes=only_routes)
    if not ok:
        return ok, None
    with sqlite3.connect(scratch) as conn:
        calls = conn.execute("select trip_id, stop_id from stop_times order by trip_id, stop_sequence").fetchall()
    return ok, calls


def test_the_filter_cuts_the_rows_to_the_header(tmp_path):
    cut = tmp_path / "cut.zip"
    zip_filter.filter_gtfs_zip(_zip(tmp_path / "riga.zip"), cut, ["riga_trol_3"])
    with zipfile.ZipFile(cut) as zin:
        lines = zin.read("stop_times.txt").decode().splitlines()
    assert [len(line.split(",")) for line in lines] == [7, 7, 7]


def test_the_filtered_import_loads_the_trolleybus(tmp_path):
    assert _imported(tmp_path, ["riga_trol_3"]) == (True, [("T1", "S1"), ("T1", "S2")])


def test_the_whole_feed_import_loads_the_feed(tmp_path):
    # reached when no line is asked for, or when the filter cannot run
    assert _imported(tmp_path, None) == (
        True, [("T1", "S1"), ("T1", "S2"), ("T2", "S1"), ("T2", "S3")])
    assert not (tmp_path / "riga.import.sqlite.zip").exists()


def test_a_single_wider_row_deep_in_a_table_is_found(tmp_path, monkeypatch):
    # pygtfs stops on the first wider row wherever it lies: the last call
    # alone carries a cell too many
    monkeypatch.setitem(FEED, "stop_times.txt", (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"
        "T2,09:00:00,09:00:00,S1,1\nT2,09:20:00,09:20:00,S3,2,0\n"))
    assert zip_filter.table_wider_than_header(str(_zip(tmp_path / "riga.zip"))) == "stop_times.txt"
    assert _imported(tmp_path, None)[0]


def test_a_feed_whose_rows_fit_their_headers_is_read_from_the_zip_itself(tmp_path, monkeypatch):
    monkeypatch.setitem(FEED, "stop_times.txt", (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"))
    assert zip_filter.table_wider_than_header(str(_zip(tmp_path / "riga.zip"))) is None
