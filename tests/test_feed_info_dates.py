"""A feed_info.txt with dates pygtfs cannot read no longer stops the import.

feed_start_date and feed_end_date are optional in GTFS. Krakow's trams
publish the columns with nothing in them, and pygtfs reads every value of
those columns as a YYYYMMDD date: strptime(None) ended the import and the
feed never loaded, unless the user had ticked clean_feed_info. The import
now leaves feed_info.txt out by itself when its dates do not read.
"""
from __future__ import annotations

import sqlite3
import zipfile

import ha_stub

ha_stub.install()

gtfs_filter = ha_stub.load("gtfs_filter")
source_zip = ha_stub.load("source_zip")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nZ,ZTP,http://z,Europe/Warsaw\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nroute_23,Z,23,0\n",
    "trips.txt": "route_id,service_id,trip_id\nroute_23,S,T1\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T1,08:00:00,08:00:00,A,1\nT1,08:10:00,08:10:00,B,2\n"),
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nA,Alpha,50.0,19.9\nB,Bravo,50.01,19.91\n",
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260918,20261216\n"),
}
EMPTY_DATES = ("feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,"
               "feed_end_date,feed_version\n\"ZTP\",\"http://ztp.krakow.pl/\",pl,,,20260918\n")


def _zip(path, feed_info=None):
    with zipfile.ZipFile(path, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
        if feed_info is not None:
            zout.writestr("feed_info.txt", feed_info)
    return path


def test_dates_pygtfs_cannot_read_are_told(tmp_path):
    assert gtfs_filter.feed_info_unreadable(_zip(tmp_path / "a.zip", EMPTY_DATES))
    assert gtfs_filter.feed_info_unreadable(_zip(
        tmp_path / "b.zip", "feed_publisher_name,feed_start_date\nP,2026-09-18\n"))


def test_readable_or_absent_dates_are_left_alone(tmp_path):
    assert not gtfs_filter.feed_info_unreadable(_zip(
        tmp_path / "a.zip", "feed_publisher_name,feed_start_date,feed_end_date\n"
                            "P,20260918,20261216\n"))
    # the columns left out: pygtfs sets nothing, nothing to fear
    assert not gtfs_filter.feed_info_unreadable(_zip(
        tmp_path / "b.zip", "feed_publisher_name,feed_lang\nP,pl\n"))
    assert not gtfs_filter.feed_info_unreadable(_zip(tmp_path / "c.zip"))
    assert not gtfs_filter.feed_info_unreadable(tmp_path / "missing.zip")


def _imported(tmp_path, only_routes):
    _zip(tmp_path / "krakow.zip", EMPTY_DATES)
    scratch = str(tmp_path / "krakow.import.sqlite")
    ok = source_zip.build_scratch_database(str(tmp_path), "krakow.zip", scratch,
                                           only_routes=only_routes)
    with sqlite3.connect(scratch) as conn:
        trips = conn.execute("select count(*) from trips").fetchone()[0]
    return ok, trips


def test_the_filtered_import_loads_the_feed(tmp_path):
    assert _imported(tmp_path, ["route_23"]) == (True, 1)


def test_the_whole_feed_import_loads_the_feed(tmp_path):
    assert _imported(tmp_path, None) == (True, 1)
