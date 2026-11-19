"""A datasource gets an index on the dates of calendar_dates.

The stops around a person are read with the services added on three
days, looked up in calendar_dates by date. pygtfs indexes that table by
service only: SQLite built an index of its own over the whole table at
every reading, 3 s of the 3.1 a reading took on Sofia's feed, 529 000
dates, whatever the radius. With the index the same reading takes 0.04 s.

Every index of calendar_dates carries "date" in its name, the table's
own: the check that an index exists looks for the column past it.
"""
from __future__ import annotations

import os
import types

import feed_db
import ha_stub

ha_stub.install()

datasource = ha_stub.load("data.datasource")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.0005,1.0005\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,10,Ten,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T,06:00:00,06:00:00,S1,1\nT,06:10:00,06:10:00,S2,2\n"),
    "calendar_dates.txt": "service_id,date,exception_type\nD,20261119,1\nD,20261120,1\n",
}


def _indexes(schedule):
    with schedule.engine.connect() as conn:
        return {row[0]: row[1] for row in conn.exec_driver_sql(
            "select name, sql from sqlite_master where type = 'index' and tbl_name = 'calendar_dates'")}


def test_the_dates_get_their_index_beside_the_one_by_service(tmp_path, monkeypatch):
    schedule = feed_db.build(tmp_path, FEED)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: os.path.join(str(tmp_path), *parts)))
    monkeypatch.setattr(datasource, "check_extracting", lambda *args: False)
    try:
        before = _indexes(schedule)
        # pygtfs's own, by service, with the table's "date" in its name
        assert "ix_calendar_dates_service_id" in before
        assert "gtfs2_calendar_dates_date" not in before
        datasource.check_datasource_index(hass, schedule, "", "feed")
        after = _indexes(schedule)
        assert after["gtfs2_calendar_dates_date"].lower().endswith("calendar_dates(date)")
        # and a second check of the same file makes nothing again
        datasource.check_datasource_index(hass, schedule, "", "feed")
        assert _indexes(schedule) == after
    finally:
        schedule.engine.dispose()


def test_two_entries_checking_at_once_both_get_through(tmp_path, monkeypatch):
    # the entries of a source check it together when Home Assistant
    # starts: both read the list of indexes before either has made one,
    # and the second to create it failed, its first reading with it
    schedule = feed_db.build(tmp_path, FEED)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: os.path.join(str(tmp_path), *parts)))
    monkeypatch.setattr(datasource, "check_extracting", lambda *args: False)
    try:
        with schedule.engine.connect() as conn:
            # made by the other entry, after this one read the list
            conn.exec_driver_sql("create index gtfs2_calendar_dates_date on calendar_dates(date)")
            conn.commit()
        monkeypatch.setattr(datasource, "DATASOURCE_INDEXES",
                            (("calendar_dates", "day", "gtfs2_calendar_dates_date"),))
        datasource._INDEX_CHECKED.clear()
        # "day" is in no index name: the check goes on to create, as the
        # second entry did, and the index is there already
        datasource.check_datasource_index(hass, schedule, "", "feed")
        assert list(_indexes(schedule)).count("gtfs2_calendar_dates_date") == 1
    finally:
        schedule.engine.dispose()
