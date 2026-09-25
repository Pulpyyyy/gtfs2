"""The indexes a datasource gets, once per database file.

check_datasource_index runs before every refresh of every sensor. It
creates the indexes the queries lean on when they are missing and gives
routes with no agency_id the feed's agency; a database file already
checked is not read again until it changes.
"""
from __future__ import annotations

import sqlite3
import types
import zipfile

from sqlalchemy import create_engine, event

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,Europe/Paris\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nR1,A,1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nR1,S,T1\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"),
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,47.9,1.9\nS2,Centre,47.91,1.91\n",
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260101,20261231\n"),
}


def _datasource(tmp_path, interned=False):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    conn = sqlite3.connect(gtfs_dir / "src.sqlite")
    conn.execute("create table stops (stop_id varchar, stop_name varchar)")
    conn.execute("create table routes (route_id varchar, agency_id varchar, route_type integer)")
    conn.execute("create table trips (trip_id varchar, route_id varchar)")
    conn.execute("create table shapes (shape_id varchar)")
    conn.execute("create table agency (agency_id varchar)")
    conn.execute("insert into agency values ('A')")
    conn.execute("insert into routes values ('R1', null, 3)")
    if interned:
        conn.execute("create table gtfs2_stop_times (tk integer, sk integer)")
        conn.execute("create view stop_times as select tk as trip_id, sk as stop_id from gtfs2_stop_times")
    else:
        conn.execute("create table stop_times (trip_id varchar, stop_id varchar)")
    conn.commit()
    conn.close()
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))
    engine = create_engine(f"sqlite:///{gtfs_dir / 'src.sqlite'}")
    return hass, types.SimpleNamespace(engine=engine), gtfs_dir / "src.sqlite"


def _indexes(db):
    conn = sqlite3.connect(db)
    try:
        return {name for (name,) in conn.execute("select name from sqlite_master where type = 'index'")}
    finally:
        conn.close()


def test_missing_indexes_are_made_and_routes_get_their_agency(tmp_path):
    hass, schedule, db = _datasource(tmp_path)
    gtfs_helper._INDEX_CHECKED.clear()
    gtfs_helper.check_datasource_index(hass, schedule, "gtfs2", "src")
    assert _indexes(db) == {name for _t, _c, name in gtfs_helper.DATASOURCE_INDEXES}
    conn = sqlite3.connect(db)
    assert conn.execute("select agency_id from routes").fetchone() == ("A",)
    conn.close()
    schedule.engine.dispose()


def test_an_interned_datasource_keeps_its_view(tmp_path):
    hass, schedule, db = _datasource(tmp_path, interned=True)
    gtfs_helper._INDEX_CHECKED.clear()
    gtfs_helper.check_datasource_index(hass, schedule, "gtfs2", "src")
    assert not {n for n in _indexes(db) if n.startswith("gtfs2_stop_times")}
    schedule.engine.dispose()


def test_the_same_file_is_not_read_again(tmp_path):
    hass, schedule, db = _datasource(tmp_path)
    gtfs_helper._INDEX_CHECKED.clear()
    gtfs_helper.check_datasource_index(hass, schedule, "gtfs2", "src")
    opened = []
    event.listen(schedule.engine, "connect", lambda *a: opened.append(1))
    schedule.engine.dispose()
    gtfs_helper.check_datasource_index(hass, schedule, "gtfs2", "src")
    gtfs_helper.check_datasource_index(hass, schedule, "gtfs2", "src")
    assert opened == []
    schedule.engine.dispose()


def _stop_times_indexes(db):
    """The indexes on stop_times besides its primary key."""
    conn = sqlite3.connect(db)
    try:
        return {name for (name,) in conn.execute(
            "select name from sqlite_master where type = 'index' "
            "and tbl_name = 'stop_times' and sql is not null")}
    finally:
        conn.close()


def test_an_import_fills_stop_times_before_indexing_it(tmp_path):
    # pygtfs 0.1.10 on creates two stop_times indexes with the table, which
    # SQLite would then update at every row imported: the import takes them
    # off the empty table, and the datasource check builds its own once
    import pygtfs
    source_zip = ha_stub.load("source_zip")
    feed = tmp_path / "feed.zip"
    with zipfile.ZipFile(feed, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    fresh = pygtfs.Schedule(str(tmp_path / "fresh.sqlite"))
    gtfs_helper.drop_import_indexes(fresh)
    fresh.engine.dispose()
    assert _stop_times_indexes(tmp_path / "fresh.sqlite") == set()

    scratch = tmp_path / "gtfs2" / "src.sqlite"
    scratch.parent.mkdir()
    assert source_zip.build_scratch_database(str(tmp_path), "feed.zip", str(scratch),
                                             only_routes=["R1"])
    assert _stop_times_indexes(scratch) == set()
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))
    schedule = types.SimpleNamespace(engine=create_engine(f"sqlite:///{scratch}"))
    gtfs_helper._INDEX_CHECKED.clear()
    gtfs_helper.check_datasource_index(hass, schedule, "gtfs2", "src")
    assert _stop_times_indexes(scratch) == {"gtfs2_stop_times_trip_id", "gtfs2_stop_times_stop_id"}
    conn = sqlite3.connect(scratch)
    assert conn.execute("select count(*) from stop_times").fetchone() == (2,)
    conn.close()
    schedule.engine.dispose()
