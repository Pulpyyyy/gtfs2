"""Interning stop_times: all or nothing.

intern_gtfs_datasource swaps the stop_times table for two key tables and a
view. A run that fails half way must leave the database as it found it,
and a database left with the key tables of an older failed run must still
intern. Each case builds a small sqlite with just what interning reads.
"""
from __future__ import annotations

import sqlite3

import ha_stub

gtfs_db = ha_stub.load("gtfs_db")
intern_gtfs_datasource = gtfs_db.intern_gtfs_datasource


def make_db(path, extra_column=None):
    conn = sqlite3.connect(path)
    extra = f", {extra_column} varchar" if extra_column else ""
    conn.execute(f"create table stop_times (feed_id integer, trip_id varchar, "
                 f"stop_id varchar, stop_sequence integer, arrival_time varchar{extra})")
    rows = [(1, "T1", "S1", 1, "08:00:00"), (1, "T1", "S2", 2, "08:10:00"),
            (1, "T2", "S1", 1, "09:00:00"), (1, "T2", "S2", 2, "09:10:00")]
    if extra_column:
        rows = [r + ("x",) for r in rows]
    marks = ", ".join("?" * len(rows[0]))
    conn.executemany(f"insert into stop_times values ({marks})", rows)
    conn.commit()
    conn.close()


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return {name: kind for name, kind in conn.execute(
            "select name, type from sqlite_master where type in ('table', 'view')")}
    finally:
        conn.close()


def test_intern_replaces_stop_times_by_a_view(tmp_path):
    make_db(tmp_path / "src.sqlite")
    stats = intern_gtfs_datasource(str(tmp_path), "src")
    assert stats and stats["rows"] == 4
    found = tables(tmp_path / "src.sqlite")
    assert found["stop_times"] == "view"
    conn = sqlite3.connect(tmp_path / "src.sqlite")
    rows = conn.execute("select trip_id, stop_id, stop_sequence from stop_times "
                        "order by trip_id, stop_sequence").fetchall()
    conn.close()
    assert rows == [("T1", "S1", 1), ("T1", "S2", 2), ("T2", "S1", 1), ("T2", "S2", 2)]


def test_a_failed_intern_leaves_nothing_behind(tmp_path):
    # a column named after a keyword makes the stop_times copy fail, after
    # the key tables are already made
    make_db(tmp_path / "src.sqlite", extra_column='"group"')
    assert intern_gtfs_datasource(str(tmp_path), "src") is None
    found = tables(tmp_path / "src.sqlite")
    assert found == {"stop_times": "table"}


def test_debris_of_an_older_run_does_not_block(tmp_path):
    make_db(tmp_path / "src.sqlite")
    conn = sqlite3.connect(tmp_path / "src.sqlite")
    conn.execute("create table gtfs2_trip_key (tk integer primary key, trip_id varchar)")
    conn.commit()
    conn.close()
    assert intern_gtfs_datasource(str(tmp_path), "src")
    assert tables(tmp_path / "src.sqlite")["stop_times"] == "view"


def test_intern_on_a_copy_swaps_the_result_in(tmp_path):
    make_db(tmp_path / "src.sqlite")
    stats = gtfs_db.on_a_copy(str(tmp_path), "src", intern_gtfs_datasource, False)
    assert stats and stats["file"] == "src"
    assert tables(tmp_path / "src.sqlite")["stop_times"] == "view"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src.sqlite"]


def test_the_live_file_stays_readable_during_the_work(tmp_path):
    make_db(tmp_path / "src.sqlite")
    seen = []

    def work(gtfs_dir, name, *args):
        # what a sensor does meanwhile: no wait allowed
        conn = sqlite3.connect(tmp_path / "src.sqlite", timeout=0)
        seen.append(conn.execute("select count(*) from stop_times").fetchone()[0])
        conn.close()
        return intern_gtfs_datasource(gtfs_dir, name)

    assert gtfs_db.on_a_copy(str(tmp_path), "src", work)
    assert seen == [4]


def test_nothing_done_nothing_swapped(tmp_path):
    make_db(tmp_path / "src.sqlite")
    stamp = (tmp_path / "src.sqlite").stat().st_mtime_ns
    assert gtfs_db.on_a_copy(str(tmp_path), "src", lambda d, n: None) is None
    assert (tmp_path / "src.sqlite").stat().st_mtime_ns == stamp
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src.sqlite"]


def make_scratch(path):
    """A scratch database of two routes, in the columns copy_route reads."""
    conn = sqlite3.connect(path)
    conn.execute("create table stops (feed_id integer, stop_id varchar, stop_name varchar, "
                 "primary key (feed_id, stop_id))")
    conn.execute("create table trips (feed_id integer, trip_id varchar, route_id varchar, "
                 "primary key (feed_id, trip_id))")
    conn.execute("create table stop_times (feed_id integer, trip_id varchar, stop_id varchar, "
                 "stop_sequence integer, arrival_time varchar, departure_time varchar, "
                 "stop_headsign varchar, pickup_type integer, drop_off_type integer, "
                 "shape_dist_traveled float, timepoint integer, "
                 "primary key (feed_id, trip_id, stop_sequence))")
    conn.executemany("insert into stops values (1, ?, ?)", [("S1", "One"), ("S2", "Two"), ("S3", "Three")])
    conn.executemany("insert into trips values (1, ?, ?)", [("A1", "A"), ("A2", "A"), ("B1", "B")])
    rows = [("A1", "S1", 1), ("A1", "S2", 2), ("A2", "S1", 1), ("A2", "S2", 2),
            ("B1", "S2", 1), ("B1", "S3", 2)]
    conn.executemany("insert into stop_times values (1, ?, ?, ?, '08:00:00', '08:00:00', "
                     "null, 0, 0, null, 1)", rows)
    conn.commit()
    conn.close()


def _import(tmp_path, routes):
    def build(scratch_file):
        make_scratch(scratch_file)
        return True
    return gtfs_db.import_routes(str(tmp_path), "src", routes, build)


def test_import_counts_what_each_route_brings(tmp_path):
    assert _import(tmp_path, ["A", "B"]) == {"A": 4, "B": 2}
    conn = sqlite3.connect(tmp_path / "src.sqlite")
    assert conn.execute("select count(*) from stops").fetchone()[0] == 3
    assert conn.execute("select count(*) from stop_times").fetchone()[0] == 6
    conn.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src.sqlite"]


def test_import_into_an_interned_database(tmp_path):
    assert _import(tmp_path, ["A"]) == {"A": 4}
    assert intern_gtfs_datasource(str(tmp_path), "src")
    # a second import: the stops are already there, B brings its own rows
    assert _import(tmp_path, ["A", "B"]) == {"A": 0, "B": 2}
    conn = sqlite3.connect(tmp_path / "src.sqlite")
    rows = conn.execute("select trip_id, stop_id from stop_times order by trip_id, stop_sequence").fetchall()
    conn.close()
    assert rows == [("A1", "S1"), ("A1", "S2"), ("A2", "S1"), ("A2", "S2"), ("B1", "S2"), ("B1", "S3")]


def test_the_real_file_keeps_its_own_schema(tmp_path):
    # the scratch indexes speed the copy up and go away with the scratch file
    _import(tmp_path, ["A", "B"])
    conn = sqlite3.connect(tmp_path / "src.sqlite")
    names = [r[0] for r in conn.execute("select name from sqlite_master where type = 'index'")]
    conn.close()
    assert not [n for n in names if n.startswith("gtfs2_scratch")]
