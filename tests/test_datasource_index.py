"""The indexes a datasource gets, once per database file.

check_datasource_index runs before every refresh of every sensor. It
creates the indexes the queries lean on when they are missing and gives
routes with no agency_id the feed's agency; a database file already
checked is not read again until it changes.
"""
from __future__ import annotations

import sqlite3
import types

from sqlalchemy import create_engine, event

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


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
