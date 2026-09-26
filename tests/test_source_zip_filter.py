"""The zip filter an import runs before pygtfs reads anything.

filter_gtfs_zip cuts a feed down to the chosen lines: the tables that
describe the network stay whole, so the line picker still lists every
line, and the tables that carry the weight keep only what the chosen
trips use, a platform's parent station included. A feed it cannot read
gives None and no output, and the caller imports it whole.
zip_only_future_dates is the update service's refusal of a feed that
starts after today, and read_zip_routes what the flow lists before any
database exists.
"""
from __future__ import annotations

import csv
import io
import zipfile

import ha_stub

gtfs_filter = ha_stub.load("gtfs_filter")

FEED = {
    "agency.txt": [["agency_id", "agency_name"], ["A", "Agency"]],
    "routes.txt": [["route_id", "agency_id", "route_short_name", "route_type"],
                   ["R1", "A", "1", "3"], ["R2", "A", "2", "3"]],
    "trips.txt": [["route_id", "service_id", "trip_id"],
                  ["R1", "S1", "T1"], ["R2", "S2", "T2"]],
    "stop_times.txt": [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                       ["T1", "08:00:00", "08:00:00", "Q1", "1"],
                       ["T1", "08:10:00", "08:10:00", "B", "2"],
                       ["T2", "09:00:00", "09:00:00", "C", "1"],
                       ["T2", "09:10:00", "09:10:00", "D", "2"]],
    "stops.txt": [["stop_id", "stop_name", "parent_station"],
                  ["P", "Station", ""], ["Q1", "Station quay 1", "P"],
                  ["B", "Bee", ""], ["C", "Sea", ""], ["D", "Dee", ""]],
    "calendar.txt": [["service_id", "monday", "start_date", "end_date"],
                     ["S1", "1", "20260101", "20261231"], ["S2", "1", "20260101", "20261231"]],
    "calendar_dates.txt": [["service_id", "date", "exception_type"],
                           ["S1", "20260704", "2"], ["S2", "20260705", "2"]],
    "frequencies.txt": [["trip_id", "start_time", "end_time", "headway_secs"],
                        ["T1", "08:00:00", "10:00:00", "600"],
                        ["T2", "09:00:00", "11:00:00", "600"]],
    "feed_info.txt": [["feed_publisher_name", "feed_lang"], ["Pub", "fr"]],
    "shapes.txt": [["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
                   ["SH", "0", "0", "1"]],
}


def _zip(path, tables, bom=False):
    with zipfile.ZipFile(path, "w") as zout:
        for name, rows in tables.items():
            out = io.StringIO()
            csv.writer(out, lineterminator="\n").writerows(rows)
            zout.writestr(name, ("﻿" if bom else "") + out.getvalue())
    return path


def _read(path):
    with zipfile.ZipFile(path) as zin:
        return {name: list(csv.reader(io.TextIOWrapper(zin.open(name), encoding="utf-8-sig")))[1:]
                for name in zin.namelist()}


def test_the_chosen_line_and_what_it_uses(tmp_path):
    src = _zip(tmp_path / "feed.zip", FEED)
    stats = gtfs_filter.filter_gtfs_zip(src, tmp_path / "cut.zip", ["R1"])
    assert stats["trips"] == (1, 2)
    assert stats["stop_times"] == (2, 4)
    cut = _read(tmp_path / "cut.zip")
    # the network whole, for the line picker
    assert cut["routes.txt"] == FEED["routes.txt"][1:]
    assert cut["agency.txt"] == FEED["agency.txt"][1:]
    assert cut["feed_info.txt"] == FEED["feed_info.txt"][1:]
    # the weight, cut to the chosen trips
    assert [r[2] for r in cut["trips.txt"]] == ["T1"]
    assert {r[0] for r in cut["stop_times.txt"]} == {"T1"}
    # the quay's station comes along
    assert sorted(r[0] for r in cut["stops.txt"]) == ["B", "P", "Q1"]
    assert [r[0] for r in cut["calendar.txt"]] == ["S1"]
    assert [r[0] for r in cut["calendar_dates.txt"]] == ["S1"]
    assert [r[0] for r in cut["frequencies.txt"]] == ["T1"]
    # what the import strips anyway is never copied
    assert "shapes.txt" not in cut


def test_feed_info_dropped_on_request(tmp_path):
    src = _zip(tmp_path / "feed.zip", FEED)
    gtfs_filter.filter_gtfs_zip(src, tmp_path / "cut.zip", ["R2"], drop_feed_info=True)
    assert "feed_info.txt" not in _read(tmp_path / "cut.zip")


def test_a_feed_it_cannot_read_leaves_nothing(tmp_path):
    no_stop_times = {k: v for k, v in FEED.items() if k != "stop_times.txt"}
    for tables in (no_stop_times, None):
        src = tmp_path / "feed.zip"
        if tables is None:
            src.write_bytes(b"not a zip")
        else:
            _zip(src, tables)
        dst = tmp_path / "cut.zip"
        assert gtfs_filter.filter_gtfs_zip(src, dst, ["R1"]) is None
        assert not dst.exists()


def test_only_future_dates(tmp_path):
    def feed(calendar, dates):
        return _zip(tmp_path / "f.zip", {
            "calendar.txt": [["service_id", "start_date"]] + [["S", d] for d in calendar],
            "calendar_dates.txt": [["service_id", "date"]] + [["S", d] for d in dates]})
    assert gtfs_filter.zip_only_future_dates(feed(["29990101"], ["29990102"]))
    assert not gtfs_filter.zip_only_future_dates(feed(["29990101"], ["20000101"]))
    assert not gtfs_filter.zip_only_future_dates(feed(["20000101"], []))
    # nothing to read the dates from: never refused on a guess
    assert not gtfs_filter.zip_only_future_dates(feed([], []))
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    assert not gtfs_filter.zip_only_future_dates(bad)


def test_the_routes_before_any_database(tmp_path):
    src = _zip(tmp_path / "feed.zip", FEED, bom=True)
    assert [r["route_id"] for r in gtfs_filter.read_zip_routes(src)] == ["R1", "R2"]
    assert [r["agency_id"] for r in gtfs_filter.read_zip_agencies(src)] == ["A"]
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    assert gtfs_filter.read_zip_routes(bad) == []
