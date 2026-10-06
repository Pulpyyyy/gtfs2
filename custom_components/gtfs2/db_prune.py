"""A datasource trimmed down to the lines it follows (prune_gtfs_datasource,
the prune_datasource service): their trips and what hangs off them are
kept, the rest removed.

Since an import stopped rebuilding the real database (db_build.py), prune
is only needed to drop a line that is no longer followed.
"""
from __future__ import annotations

from collections.abc import Collection
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any

from .feed.files import real_path
from .feed.source_entries import source_train_lines
from .stop_rules import rail_line_of

if TYPE_CHECKING:
    # for the annotations only
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


# Tables carrying a trip_id that must follow trips when pruning, with the
# column naming their feed: pygtfs' own join tables do not use "feed_id".
PRUNE_TRIP_DEPENDENTS = (
    ("stop_times", "feed_id"),
    ("frequencies", "feed_id"),
    ("_trip_shapes", "trip_feed_id"),
)


# the calendar hangs off a service, not off a trip, so it survives a prune that
# only follows trips. On a national feed that is what the pruned file is made
# of: 199584 calendar_dates rows for the 1276 that the kept trips run on
PRUNE_SERVICE_DEPENDENTS = (
    ("calendar", "feed_id"),
    ("calendar_dates", "feed_id"),
)


# a service no surviving trip runs on. Asked of gtfs2_keep_services, which is
# collected once from the kept trips, so it reads the same before and after the
# trips rows are deleted - and so that the answer is one indexed probe.
#
# Reaching through trips here instead was measured to be the wrong shape: with
# no index on trips(service_id), SQLite could only narrow on feed_id, so every
# row of calendar_dates walked every trip of the feed. On the SNCF feed that is
# 175557 x 40055 probes, and the prune did not come back within ten minutes.
_ORPHAN_SERVICE = """not exists (
    select 1 from gtfs2_keep_services s
    where s.feed_id = {table}.{feed_col} and s.service_id = {table}.service_id)"""


def routes_of_lines(db_file: str, codes: Collection[str]) -> set[str]:
    """The route_ids of the rail lines wearing these codes in a database:
    what a train sensor holding to them keeps through a prune, as a
    refresh brings them in (source_train_lines). Blocking, for the
    executor; an empty set when the file does not say."""
    if not codes or not os.path.exists(db_file):
        return set()
    conn = sqlite3.connect(db_file, timeout=60)
    try:
        rows = conn.execute("select route_id, route_type, route_short_name from routes").fetchall()
    except sqlite3.Error as ex:
        _LOGGER.warning("Could not read the lines of %s: %s", db_file, ex)
        return set()
    finally:
        conn.close()
    return {str(route_id) for route_id, route_type, short_name in rows
            if rail_line_of(route_type, short_name, codes)}


async def async_train_routes(hass: HomeAssistant, gtfs_dir: str, filename: str,
                             exclude: str | None = None) -> set[str]:
    """The route_ids the source's train sensors keep: the rail lines
    wearing their codes in its database, read in the executor, and none
    without asking it when no train sensor holds to a code."""
    codes = source_train_lines(hass, filename, exclude)
    if not codes:
        return set()
    return await hass.async_add_executor_job(routes_of_lines, real_path(gtfs_dir, filename), codes)


def prune_gtfs_datasource(gtfs_dir: str, filename: str, keep_routes: Collection[str],
                          dry_run: bool = False) -> dict[str, Any] | None:
    """Trim a datasource down to the routes actually in use.

    pygtfs loads the complete feed, so a datasource holds every route of the
    network even when only a handful are configured. Dropping the unused trips
    and their stop_times reclaims most of the file: on a mid-size network this
    is typically a 4x reduction, and it compounds with the fact that stop_times
    is by far the largest table.

    What hangs off a trip goes with it, and so does the calendar of a service
    no surviving trip runs on: that one used to stay whole, which on a national
    feed left almost the entire pruned file behind.

    Only rows are removed: the tables are rebuilt from their own DDL, so
    schema, primary keys and indexes come back identical, the datasource stays
    a valid pygtfs database and routes remains complete for the config flow
    selector.
    """
    sqlite_file = real_path(gtfs_dir, filename)
    if not os.path.exists(sqlite_file):
        _LOGGER.error("Cannot prune, no such datasource: %s", sqlite_file)
        return None
    if not keep_routes:
        _LOGGER.error("Cannot prune %s: no routes to keep, this would empty the datasource", filename)
        return None
    if "train" in keep_routes:
        # "train" is the marker a train sensor stores instead of a route_id:
        # it matches city pairs across the whole feed, so its datasource must
        # keep every route. Guarded here, the last gate before deletion, so
        # every caller is covered - the prune service and the optimise step
        # both collect the marker into their keep set on a mixed source, and
        # would otherwise prune the train sensors blind.
        _LOGGER.warning("Not pruning %s: a train sensor reads the whole feed", filename)
        return None

    size_before = os.path.getsize(sqlite_file)
    conn = sqlite3.connect(sqlite_file, timeout=300)
    try:
        cur = conn.cursor()
        kept_trips, total_trips = _collect_keep(cur, filename, keep_routes)
        if not kept_trips:
            _LOGGER.error("Cannot prune %s: routes %s match no trips, aborting to avoid data loss",
                          filename, keep_routes)
            return None

        stats: dict[str, Any] = {"file": filename, "routes": sorted(keep_routes), "dry_run": dry_run,
                 "trips_before": total_trips, "trips_after": kept_trips,
                 "size_before_mb": round(size_before / 1048576, 1)}
        _prune_dependents(cur, filename, dry_run, stats)
        _prune_interned(cur, dry_run, stats)

        if dry_run:
            conn.rollback()
            _LOGGER.info("Pruning %s (dry run): %s", filename, stats)
            return stats

        _rebuild_keep(cur, "trips", "exists (select 1 from gtfs2_keep k "
                      "where k.feed_id = src.feed_id and k.trip_id = src.trip_id)")
        conn.commit()
        # VACUUM cannot run inside a transaction and is what actually shrinks the file
        conn.isolation_level = None
        cur.execute("vacuum")
    except sqlite3.Error as ex:
        _LOGGER.exception("Failed to prune datasource %s: %s", filename, ex)
        conn.rollback()
        return None
    finally:
        conn.close()

    stats["size_after_mb"] = round(os.path.getsize(sqlite_file) / 1048576, 1)
    _LOGGER.info("Pruned datasource %s: %s MB -> %s MB, %s of %s trips kept",
                 filename, stats["size_before_mb"], stats["size_after_mb"],
                 stats["trips_after"], stats["trips_before"])
    return stats


def _collect_keep(cur: sqlite3.Cursor, filename: str, keep_routes: Collection[str]) -> tuple[int, int]:
    """Fill the temp tables gtfs2_keep, the trips of keep_routes, and
    gtfs2_keep_services, the services they run on. Returns (trips kept,
    trips in the datasource)."""
    placeholders = ",".join("?" * len(keep_routes))
    known = {r[0] for r in cur.execute(
        f"select route_id from routes where route_id in ({placeholders})",  # noqa: S608
        tuple(keep_routes))}
    if unknown := set(keep_routes) - known:
        _LOGGER.warning("Pruning %s: these routes are not in the datasource: %s", filename, unknown)

    cur.execute("create temp table gtfs2_keep(feed_id integer, trip_id varchar, "
                "primary key(feed_id, trip_id)) without rowid")
    cur.execute(f"insert into gtfs2_keep select feed_id, trip_id from trips "  # noqa: S608
                f"where route_id in ({placeholders})", tuple(keep_routes))
    kept_trips = cur.execute("select count(*) from gtfs2_keep").fetchone()[0]
    total_trips = cur.execute("select count(*) from trips").fetchone()[0]
    if not kept_trips:
        return kept_trips, total_trips

    # the services those trips run on, collected once and keyed, so the
    # calendar rebuilds below probe an index instead of scanning trips
    cur.execute("create temp table gtfs2_keep_services("
                "feed_id integer, service_id varchar, "
                "primary key(feed_id, service_id)) without rowid")
    cur.execute("insert into gtfs2_keep_services "
                "select distinct t.feed_id, t.service_id from trips t "
                "inner join gtfs2_keep k "
                "on k.feed_id = t.feed_id and k.trip_id = t.trip_id")
    return kept_trips, total_trips


def _prune_dependents(cur: sqlite3.Cursor, filename: str, dry_run: bool, stats: dict[str, Any]) -> None:
    """Keep, of each table hanging off a trip or a service, the rows of the
    kept ones (only count them on a dry run), their counts put in stats."""
    for table, feed_col in PRUNE_TRIP_DEPENDENTS:
        if not _table_has_columns(cur, table, feed_col, "trip_id"):
            _LOGGER.debug("Pruning %s: skipping absent or unexpected table %s", filename, table)
            continue
        stats[f"{table}_before"], stats[f"{table}_after"] = _keep_rows(
            cur, table, "exists (select 1 from gtfs2_keep k "
            f"where k.feed_id = src.{feed_col} and k.trip_id = src.trip_id)", dry_run)

    for table, feed_col in PRUNE_SERVICE_DEPENDENTS:
        if not _table_has_columns(cur, table, feed_col, "service_id"):
            _LOGGER.debug("Pruning %s: skipping absent or unexpected table %s",
                          filename, table)
            continue
        stats[f"{table}_before"], stats[f"{table}_after"] = _keep_rows(
            cur, table, "not " + _ORPHAN_SERVICE.format(table="src", feed_col=feed_col),
            dry_run)


def _prune_interned(cur: sqlite3.Cursor, dry_run: bool, stats: dict[str, Any]) -> None:
    """The same for an interned datasource's stop_times and key tables.

    An interned datasource keeps its stop_times in gtfs2_stop_times, keyed
    by tk, and exposes the original shape as a view. _table_has_columns
    skips the view, so the rows have to be removed here instead.
    """
    if not _table_has_columns(cur, "gtfs2_stop_times", "tk"):
        return
    keep_tk = ("select k.tk from gtfs2_trip_key k "
               "inner join gtfs2_keep g on g.trip_id = k.trip_id")
    stats["gtfs2_stop_times_before"], stats["gtfs2_stop_times_after"] = _keep_rows(
        cur, "gtfs2_stop_times", f"src.tk in ({keep_tk})", dry_run)
    if not dry_run:
        # the key tables hold the long identifiers interning removed
        # from every row: leaving them behind keeps most of the weight
        _rebuild_keep(cur, "gtfs2_trip_key",
                      "src.tk in (select tk from gtfs2_stop_times)")
        _rebuild_keep(cur, "gtfs2_stop_key",
                      "src.sk in (select sk from gtfs2_stop_times)")


def _keep_rows(cur: sqlite3.Cursor, table: str, keep_where: str, dry_run: bool) -> tuple[int, int]:
    """(rows before, rows after) of a table keeping the rows keep_where
    accepts, aliased as src: kept by _rebuild_keep, or only counted on a
    dry run, the same condition either way."""
    before = cur.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
    if dry_run:
        after = cur.execute(
            f"select count(*) from {table} src where {keep_where}").fetchone()[0]  # noqa: S608
    else:
        _rebuild_keep(cur, table, keep_where)
        after = cur.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
    return before, after


def _rebuild_keep(cur: sqlite3.Cursor, table: str, keep_where: str) -> None:
    """Rebuild a table with only the rows keep_where accepts, aliased as src.

    A prune drops almost every row of the big tables, and a DELETE pays for
    the dropped rows: each one updates every index as it goes, and the
    journal receives the original content of nearly every page touched.
    Measured on the Dutch national feed (15.1 M stop_times, 99.8 % of them
    to drop), the journal passed 2.4 GB and the statement never came back.

    Copying the kept rows into a fresh table and dropping the old one costs
    O(kept) instead: the copy fills new pages, which need no journaling, and
    the drop only rewrites the freelist bookkeeping. Schema, primary keys
    and indexes are preserved because the table is recreated from its own
    DDL and the indexes from theirs - after the copy, so each index is
    built in one pass rather than maintained row by row.
    """
    table_sql = cur.execute(
        "select sql from sqlite_master where type = 'table' and name = ?",
        (table,)).fetchone()[0]
    index_sqls = [r[0] for r in cur.execute(
        "select sql from sqlite_master where type = 'index' and tbl_name = ? "
        "and sql is not null", (table,))]
    # a process killed mid-rebuild rolls back on the next open, but a stale
    # throwaway table costs nothing to clear and would fail the rename
    cur.execute("drop table if exists gtfs2_prune_old")
    # the rename is transient, so nothing that mentions the table may follow
    # it: without legacy mode the rename rewrites the bodies of views (an
    # interned datasource exposes stop_times as one) to the throwaway name
    cur.execute("pragma legacy_alter_table = ON")
    cur.execute(f"alter table {table} rename to gtfs2_prune_old")  # noqa: S608
    cur.execute("pragma legacy_alter_table = OFF")
    cur.execute(table_sql)
    cur.execute(f"insert into {table} select * from gtfs2_prune_old src "  # noqa: S608
                f"where {keep_where}")
    cur.execute("drop table gtfs2_prune_old")
    for sql in index_sqls:
        cur.execute(sql)


def _table_has_columns(cur: sqlite3.Cursor, table: str, *columns: str) -> bool:
    """Return True when a real table exists and carries every one of columns.

    Views are rejected on purpose: pragma table_info answers for them too, so
    an interned datasource, where stop_times is a view, used to reach a DELETE
    and fail with "cannot modify stop_times because it is a view".
    """
    try:
        kind = cur.execute(
            "select type from sqlite_master where name = ?", (table,)).fetchone()
        if not kind or kind[0] != "table":
            return False
        present = {row[1] for row in cur.execute(f"pragma table_info({table})")}
    except sqlite3.Error:
        return False
    return bool(present) and set(columns) <= present
