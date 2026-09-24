"""A feed whose lines are padded to a fixed width reads as pygtfs reads it.

Renfe pads every line of its tables with spaces, the header's included,
so the last column of its calendar is named "end_date" and a hundred
spaces. pygtfs strips each cell and imports it whole; the readers that go
to the zip themselves looked the column up by its name and found nothing:
the timetable had no last day, the lines had no dates, and a padded
parent_station would have cut every platform off its station in the
import filter.
"""
from __future__ import annotations

import zipfile

import ha_stub

feed_window = ha_stub.load("feed_window")
gtfs_filter = ha_stub.load("gtfs_filter")
route_names = ha_stub.load("route_names")

WIDTH = 80
FEED = {
    "agency.txt": ["agency_id,agency_name,agency_url,agency_timezone",
                   "A,A,http://a,Europe/Madrid"],
    "routes.txt": ["route_id,agency_id,route_short_name,route_type",
                   "R1,A,AVE,2", "R2,A,AVE,2"],
    "trips.txt": ["route_id,service_id,trip_id", "R1,S1,T1", "R2,S2,T2"],
    "stop_times.txt": ["trip_id,arrival_time,departure_time,stop_id,stop_sequence",
                       "T1,08:00:00,08:00:00,Q1,1", "T1,09:00:00,09:00:00,B,2",
                       "T2,10:00:00,10:00:00,B,1", "T2,11:00:00,11:00:00,Q1,2"],
    "stops.txt": ["stop_id,stop_name,parent_station",
                  "P,Madrid,", "Q1,Madrid via 1,P", "B,Sevilla,"],
    "calendar.txt": ["service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date",
                     "S1,1,1,1,1,1,1,1,20260919,20261011",
                     "S2,1,1,1,1,1,1,1,20261012,20261213"],
    # a removal after the last day and an addition before the first
    "calendar_dates.txt": ["service_id,date,exception_type",
                           "S2,20261220,2", "S1,20260918,1"],
}


def _padded(tmp_path):
    with zipfile.ZipFile(tmp_path / "feed.zip", "w") as zout:
        for name, lines in FEED.items():
            zout.writestr(name, "".join(line.ljust(WIDTH) + "\r\n" for line in lines))
    return tmp_path / "feed.zip"


def test_the_timetable_knows_its_last_day(tmp_path):
    window = feed_window.read_feed_window(_padded(tmp_path))
    assert (window["first_service_day"], window["last_service_day"]) == \
        ("2026-09-18", "2026-12-13")


def test_the_lines_know_their_days(tmp_path):
    _padded(tmp_path)
    assert route_names.route_spans(str(tmp_path), "feed", ["R1", "R2"]) == {
        "R1": ("20260918", "20261011"), "R2": ("20261012", "20261213")}


def test_the_route_list_reads_the_last_column(tmp_path):
    routes = gtfs_filter.read_zip_routes(_padded(tmp_path))
    assert [(r["route_id"], r["route_type"].strip()) for r in routes] == [("R1", "2"), ("R2", "2")]


def test_the_filter_keeps_the_station_of_a_platform(tmp_path):
    cut = tmp_path / "cut.zip"
    gtfs_filter.filter_gtfs_zip(_padded(tmp_path), cut, ["R1"])
    with zipfile.ZipFile(cut) as zin:
        stops = zin.read("stops.txt").decode().splitlines()[1:]
    assert sorted(line.split(",")[0] for line in stops) == ["B", "P", "Q1"]
