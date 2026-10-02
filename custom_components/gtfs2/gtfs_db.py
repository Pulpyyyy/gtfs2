"""Everything that opens a gtfs2 database file directly.

pygtfs is a loader, not a database layer: `append_feed` imports a zip and the
`*_by_id` helpers read a few objects back, but it offers no way to delete, to
move rows between files, or to reshape a table. Measured on this codebase: 30
hand written queries and 18 raw writes, none of them going through pygtfs. So
that work already existed, scattered through gtfs_helper.py; this module gives
it one home.

Four things live here, or start from here:

  the two database model    real_path / scratch_path, built on in db_build.py
  reshaping a datasource    prune_gtfs_datasource / intern_gtfs_datasource
  the sources on disk       get_datasources / get_zipfiles / remove_datasource
  letting a schedule go     close_schedule

They belong together because they answer the same question - what is physically
in the file, and which files are there - and because the first largely replaces the second: once imports
stop rebuilding the real database, prune is only needed to drop a line that is
no longer followed.
"""
from __future__ import annotations

from collections.abc import Collection, Sequence
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # for the annotations only: this module runs without either
    from homeassistant.core import HomeAssistant
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


# suffix of the scratch database, alongside the real one so both live on the
# same filesystem and a rename never crosses a device boundary
IMPORT_SUFFIX = ".import"


def real_path(gtfs_dir: str, filename: str) -> str:
    """The database the sensors read."""
    return os.path.join(gtfs_dir, filename + ".sqlite")


def feed_zip(gtfs_dir: str, filename: str) -> str:
    """The feed a source was built from, kept beside its database."""
    return os.path.join(gtfs_dir, filename + ".zip")


def file_edition(path: str | None) -> tuple[int, int, int] | None:
    """Which file stands at path and as what, for a cache kept on it: its
    inode, its last write to the nanosecond, its size; None when there is
    none. A refresh swaps another file in under the same name, which the
    inode tells even when size and time come out the same."""
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def staging_name(filename: str) -> str:
    """The name a rebuilt database takes until it is swapped in."""
    return filename + ".refresh"


def scratch_path(gtfs_dir: str, filename: str) -> str:
    """The database an import builds, and which does not outlive it."""
    return os.path.join(gtfs_dir, filename + IMPORT_SUFFIX + ".sqlite")


def remove_files(*paths: str) -> None:
    """Remove each of these files that exists; one that cannot go is said,
    not raised."""
    for path in paths:
        if os.path.exists(path):
            try:
                os.remove(path)
                _LOGGER.debug("Removed %s", path)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", path, ex)


def routes_in(db_file: str) -> set[str] | None:
    """The route_ids a database actually carries trips for.

    An empty set means the file holds no trip; None means it could not be
    asked. The two must stay apart: a caller reading an unreadable database
    as "follows nothing" would go and build it from scratch.
    """
    if not os.path.exists(db_file):
        return set()
    conn = sqlite3.connect(db_file, timeout=60)
    try:
        return {r[0] for r in conn.execute("select distinct route_id from trips")}
    except sqlite3.Error as ex:
        _LOGGER.warning("Could not read routes from %s: %s", db_file, ex)
        return None
    finally:
        conn.close()


def route_name_in(db_file: str, route_id: str) -> str | None:
    """The name riders know a route by, its short name, else its long name,
    as the database lists it; None when the file does not say. routes.txt
    is kept whole, so a line with no trip left is still named."""
    if not os.path.exists(db_file):
        return None
    conn = sqlite3.connect(db_file, timeout=60)
    try:
        row = conn.execute("select route_short_name, route_long_name from routes "
                           "where route_id = ?", (route_id,)).fetchone()
    except sqlite3.Error as ex:
        _LOGGER.warning("Could not read the name of route %s in %s: %s", route_id, db_file, ex)
        return None
    finally:
        conn.close()
    return next((str(name).strip() for name in row or () if name and str(name).strip()), None)


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


def _rebuild_keep(cur: sqlite3.Cursor, table: str, keep_where: str, params: Sequence[object] = ()) -> None:
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
                f"where {keep_where}", params)
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
        if cur.execute("select count(*) from sqlite_master where type = 'view' "
                       "and name = 'stop_times'").fetchone()[0]:
            _LOGGER.info("Datasource %s is already interned, nothing to do", filename)
            return None

        columns = [row[1] for row in cur.execute("pragma table_info(stop_times)")]
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
        carried_ddl = ", ".join(f"{c} {_column_type(cur, 'stop_times', c)}" for c in carried)

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


def _column_type(cur: sqlite3.Cursor, table: str, column: str) -> str:
    """Return the declared type of a column, defaulting to no affinity."""
    for row in cur.execute(f"pragma table_info({table})"):
        if row[1] == column:
            return row[2] or ""
    return ""


# the databases a refresh or an import works in beside a source, never
# sources of their own: <file>.refresh.sqlite, <file>.import.sqlite and the
# filtered <file>.import.sqlite.zip
_WORK_FILE_PARTS = (".refresh", ".import")


def _list_gtfs_dir(gtfs_dir: str) -> list[str]:
    os.makedirs(gtfs_dir, exist_ok=True)
    return os.listdir(gtfs_dir)


async def get_datasources(hass: HomeAssistant, path: str) -> list[str]:
    """The datasources in the gtfs2 folder, by name.

    The whole name before ".sqlite": cut at the first dot, a name holding
    one came back short and named a source that does not exist, and the
    working files of a refresh or an import only folded into their source
    by the same accident.
    """
    _LOGGER.debug(f"Getting datasources for path: {path}")
    gtfs_dir = hass.config.path(path)
    files = await hass.async_add_executor_job(_list_gtfs_dir, gtfs_dir)
    datasources = sorted(
        file[:-len(".sqlite")] for file in files
        if file.endswith(".sqlite")
        and not file[:-len(".sqlite")].endswith(_WORK_FILE_PARTS))
    _LOGGER.debug(f"Datasources in folder: {datasources}")
    return datasources


async def get_zipfiles(hass: HomeAssistant, path: str) -> list[str]:
    """List the zip files sitting in the gtfs2 folder, without their extension.

    get_datasources lists datasources that were already extracted (.sqlite);
    this lists the archives still waiting to be extracted, so the user can pick
    one instead of typing its name.
    """
    gtfs_dir = hass.config.path(path)
    files = await hass.async_add_executor_job(_list_gtfs_dir, gtfs_dir)
    zipfiles = sorted(
        f[:-4] for f in files
        if f.endswith(".zip") and not f.endswith("_temp.zip")
        and not f.endswith("_temp_out.zip")
        # the filtered copy an import leaves while it runs
        and not f.endswith(".import.sqlite.zip")
    )
    _LOGGER.debug(f"Zip files in folder: {zipfiles}")
    return zipfiles


def remove_datasource(hass: HomeAssistant, path: str, filename: str, include_sqlite: bool) -> str:
    """Remove the files of a datasource."""
    gtfs_dir = hass.config.path(path)
    _LOGGER.info(f"Removing datasource: {os.path.join(gtfs_dir, filename)}.*")
    suffixes = [".sqlite"] if include_sqlite else []
    suffixes += ["_temp.zip", "_temp_out.zip", ".sqlite-journal", ".zip",
                 # the sidecar follows the zip it describes
                 ".zip.meta.json"]
    # what the fork keeps beside a source: the record of the installed
    # edition, and what a download, a refresh or an import stopped half way
    # leaves. Left behind, the record made a new source of the same name
    # look already built from an edition it never had
    # (.extracting: the marker of a legacy extract an older version left)
    suffixes += [".zip.new", ".extracting", ".refresh.sqlite", ".refresh.sqlite-journal",
                 ".import.sqlite", ".import.sqlite-journal", ".import.sqlite.zip"]
    if include_sqlite:
        suffixes += [".sqlite.meta.json", ".sqlite-wal", ".sqlite-shm"]
    # os.remove, not remove_files: a file that cannot go fails the removal,
    # which the config flow reports
    for suffix in suffixes:
        path = os.path.join(gtfs_dir, filename + suffix)
        if os.path.exists(path):
            os.remove(path)
    return "removed"


def close_schedule(schedule: Schedule | None) -> None:
    """Let a schedule go: its session, then its engine's connections."""
    if schedule and hasattr(schedule, "session"):
        try:
            schedule.session.close()
            schedule.engine.dispose()
        except Exception:  # pylint: disable=broad-except
            pass
