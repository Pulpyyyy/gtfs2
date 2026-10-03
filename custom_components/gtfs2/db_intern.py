"""The trip_id and stop_id strings of a datasource's stop_times replaced by
integer keys, stop_times kept as a view with its original columns
(intern_gtfs_datasource, the intern_datasource service).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from .gtfs_db import real_path

_LOGGER = logging.getLogger(__name__)


def _is_interned(cur: sqlite3.Cursor) -> bool:
    """Whether this database keeps its stop_times interned behind a view."""
    return bool(cur.execute(
        "select 1 from sqlite_master where type = 'view' and name = 'stop_times'"
    ).fetchone())


def intern_gtfs_datasource(gtfs_dir: str, filename: str, dry_run: bool = False) -> dict[str, Any] | None:
    """Replace the repeated trip_id/stop_id strings of stop_times by integer keys.

    GTFS sources routinely emit very long identifiers - 75 characters is common
    for NeTEx-derived feeds - and SQLite stores each of them three times per
    stop_times row: in the table, in the primary key index and in the trip_id
    index. Interning them into two lookup tables removes the bulk of the file.

    stop_times is then re-exposed as a view with its original columns, so every
    query in this integration keeps working unchanged.

    Two details matter for performance, both measured:
      - the primary key is (tk, stop_sequence), NOT (feed_id, tk, ...): feed_id
        holds a single value in practice and leading with it forces a skip-scan
        that misleads the planner by an order of magnitude;
      - no ANALYZE. Stale or partial statistics push the planner into scanning
        stops first and building a temporary index. pygtfs does not analyze
        either, so the natural state is the fast one.
    """
    sqlite_file = real_path(gtfs_dir, filename)
    if not os.path.exists(sqlite_file):
        _LOGGER.error("Cannot intern, no such datasource: %s", sqlite_file)
        return None

    size_before = os.path.getsize(sqlite_file)
    conn = sqlite3.connect(sqlite_file, timeout=300)
    try:
        cur = conn.cursor()
        if _is_interned(cur):
            _LOGGER.info("Datasource %s is already interned, nothing to do", filename)
            return None

        # declared type of each column, none for no affinity
        types = {row[1]: row[2] or "" for row in cur.execute("pragma table_info(stop_times)")}
        columns = list(types)
        if not columns:
            _LOGGER.error("Cannot intern %s: no stop_times table", filename)
            return None
        missing = {"trip_id", "stop_id", "stop_sequence"} - set(columns)
        if missing:
            _LOGGER.error("Cannot intern %s: stop_times lacks %s", filename, missing)
            return None

        rows = cur.execute("select count(*) from stop_times").fetchone()[0]
        unique = cur.execute("select count(*) from (select 1 from stop_times "
                             "group by trip_id, stop_sequence)").fetchone()[0]
        if unique != rows:
            _LOGGER.error("Cannot intern %s: (trip_id, stop_sequence) is not unique "
                          "(%s rows for %s combinations), the datasource likely holds "
                          "several feeds", filename, rows, unique)
            return None

        stats: dict[str, Any] = {"file": filename, "dry_run": dry_run, "rows": rows,
                 "size_before_mb": round(size_before / 1048576, 1),
                 "trip_ids": cur.execute("select count(distinct trip_id) from stop_times").fetchone()[0],
                 "stop_ids": cur.execute("select count(distinct stop_id) from stop_times").fetchone()[0]}
        if dry_run:
            _LOGGER.info("Interning %s (dry run): %s", filename, stats)
            return stats

        # columns carried over as-is, in their original order minus the interned pair
        carried = [c for c in columns if c not in ("trip_id", "stop_id", "stop_sequence")]
        carried_ddl = ", ".join(f"{c} {types[c]}" for c in carried)

        # one transaction for the whole change: sqlite3 opens none before a
        # create table, so the first one was committed on its own, and a run
        # stopped half way left it behind for every later run to trip on.
        # Tables left by such a run, on a stop_times still a table, are
        # debris and go
        conn.isolation_level = None
        cur.execute("begin")
        for debris in ("gtfs2_trip_key", "gtfs2_stop_key", "gtfs2_stop_times"):
            cur.execute(f"drop table if exists {debris}")
        cur.execute("create table gtfs2_trip_key (tk integer primary key, "
                    "trip_id varchar not null unique)")
        cur.execute("insert into gtfs2_trip_key(trip_id) select distinct trip_id from stop_times")
        cur.execute("create table gtfs2_stop_key (sk integer primary key, "
                    "stop_id varchar not null unique)")
        cur.execute("insert into gtfs2_stop_key(stop_id) select distinct stop_id from stop_times")

        cur.execute(f"create table gtfs2_stop_times (tk integer not null, "  # noqa: S608
                    f"stop_sequence integer not null, sk integer not null, {carried_ddl}, "
                    f"primary key (tk, stop_sequence)) without rowid")
        # in key order: in the order the feed lists its calls, which need
        # not go trip by trip, the rows landed all over the key's tree and
        # each insert went looking for its page: interning 3 M rows took
        # 80 s, 47 s in order
        cur.execute(f"insert into gtfs2_stop_times select k.tk, st.stop_sequence, s.sk, "  # noqa: S608
                    f"{', '.join('st.' + c for c in carried)} from stop_times st "
                    f"join gtfs2_trip_key k on k.trip_id = st.trip_id "
                    f"join gtfs2_stop_key s on s.stop_id = st.stop_id "
                    f"order by k.tk, st.stop_sequence")

        cur.execute("drop table stop_times")
        cur.execute("create index gtfs2_stop_times_sk on gtfs2_stop_times(sk)")
        view_columns = ", ".join(
            "k.trip_id as trip_id" if c == "trip_id" else
            "s.stop_id as stop_id" if c == "stop_id" else
            f"st.{c} as {c}" for c in columns)
        cur.execute(f"create view stop_times as select {view_columns} "  # noqa: S608
                    f"from gtfs2_stop_times st "
                    f"join gtfs2_trip_key k on k.tk = st.tk "
                    f"join gtfs2_stop_key s on s.sk = st.sk")
        cur.execute("commit")
        cur.execute("drop table if exists sqlite_stat1")
        cur.execute("vacuum")
    except sqlite3.Error as ex:
        _LOGGER.exception("Failed to intern datasource %s: %s", filename, ex)
        if conn.in_transaction:
            cur.execute("rollback")
        return None
    finally:
        conn.close()

    stats["size_after_mb"] = round(os.path.getsize(sqlite_file) / 1048576, 1)
    _LOGGER.info("Interned datasource %s: %s MB -> %s MB, %s rows, %s trip_ids and %s stop_ids "
                 "stored once instead of three times per row", filename,
                 stats["size_before_mb"], stats["size_after_mb"], stats["rows"],
                 stats["trip_ids"], stats["stop_ids"])
    return stats
