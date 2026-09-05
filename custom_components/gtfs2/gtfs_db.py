"""Everything that opens a gtfs2 database file directly.

pygtfs is a loader, not a database layer: `append_feed` imports a zip and the
`*_by_id` helpers read a few objects back, but it offers no way to delete, to
move rows between files, or to reshape a table. Measured on this codebase: 30
hand written queries and 18 raw writes, none of them going through pygtfs. So
that work already existed, scattered through gtfs_helper.py; this module gives
it one home.

Two things live here:

  the two database model    real_path / scratch_path / create_real_from /
                            copy_route / discard_scratch
  reshaping a datasource    prune_gtfs_datasource / intern_gtfs_datasource

They belong together because they answer the same question - what is physically
in the file - and because the first largely replaces the second: once imports
stop rebuilding the real database, prune is only needed to drop a line that is
no longer followed.

A datasource used to be a single file playing two incompatible parts. It was
the source of truth the sensors query, and it was also the workspace an import
rebuilt from scratch. So every re-extraction threw away whatever prune and
intern had reclaimed - measured on a live install: 259 MB back to 1.1 GB - and
the sensors read a half-built file for as long as it took, going unknown for
five minutes.

Splitting the two parts fixes both at once:

    real       <file>.sqlite         minimal, only the followed routes, and
                                     the only thing the sensors ever open
    scratch    <file>.import.sqlite  the raw output of pygtfs, holding the
                                     whole network, deleted when the import ends

The scratch database is deliberately raw. Interning it too would mean two sets
of integer keys to reconcile, and those keys are local to a file: a measured
second import found 31 stop keys already taken. Interning on the way in instead
mints every key in the real database, so there is never a second set to remap.

Measured on the Orleans feed (43 routes, 82 962 trips):

    add route 41    1.5 s     3.6 MB
    add route 40    2.5 s    13.3 MB
    add route A     5.5 s    44.8 MB     against 1085 MB for the full feed
"""
from __future__ import annotations

import logging
import os
import sqlite3

_LOGGER = logging.getLogger(__name__)


# suffix of the scratch database, alongside the real one so both live on the
# same filesystem and a rename never crosses a device boundary
IMPORT_SUFFIX = ".import"

# copied whole: they describe the network, not a route, and stay small. Order
# matters only for readability, since pygtfs declares foreign keys but SQLite
# does not enforce them here (pragma foreign_keys is 0).
SHARED_TABLES = ("_feed", "agency", "stops", "calendar", "calendar_dates",
                 "feed_info", "routes", "shapes", "transfers",
                 "fare_attributes", "fare_rules", "translations")


def real_path(gtfs_dir, filename):
    """The database the sensors read."""
    return os.path.join(gtfs_dir, filename + ".sqlite")


def scratch_path(gtfs_dir, filename):
    """The database an import builds, and which does not outlive it."""
    return os.path.join(gtfs_dir, filename + IMPORT_SUFFIX + ".sqlite")


def _tables(cur):
    return {r[0] for r in cur.execute(
        "select name from sqlite_master where type = 'table'")}


def _is_interned(cur):
    """Whether this database keeps its stop_times interned behind a view."""
    return bool(cur.execute(
        "select 1 from sqlite_master where type = 'view' and name = 'stop_times'"
    ).fetchone())


def create_real_from(scratch_file, real_file):
    """Build an empty real database carrying the scratch one's schema.

    Only the schema is taken: the point is a file the sensors can open and
    query, that no route has been copied into yet.
    """
    if os.path.exists(real_file):
        _LOGGER.error("Refusing to overwrite an existing datasource: %s", real_file)
        return False
    src = sqlite3.connect(scratch_file)
    try:
        statements = [r[0] for r in src.execute(
            "select sql from sqlite_master where sql is not null "
            "and name not like 'sqlite_%'")]
    finally:
        src.close()
    dst = sqlite3.connect(real_file)
    try:
        for stmt in statements:
            dst.execute(stmt)
        dst.commit()
    except sqlite3.Error as ex:
        _LOGGER.error("Could not create datasource %s: %s", real_file, ex)
        dst.close()
        # a half-built file would be taken for a real datasource
        os.remove(real_file)
        return False
    dst.close()
    return True


def copy_route(real_file, scratch_file, route_id):
    """Copy one route from the scratch database into the real one.

    Runs entirely in SQLite, through ATTACH: no round trip through pygtfs and
    no serialisation. The route's stop_times are interned on the way in when
    the real database is interned, which is what removes any key remapping.

    Returns the number of stop_times added, or None on failure.
    """
    if not os.path.exists(real_file) or not os.path.exists(scratch_file):
        _LOGGER.error("Cannot copy route %s: missing database", route_id)
        return None

    conn = sqlite3.connect(real_file, timeout=60)
    try:
        cur = conn.cursor()
        cur.execute("attach database ? as scratch", (scratch_file,))
        present = _tables(cur)
        interned = _is_interned(cur)
        before = cur.execute(
            "select count(*) from %s" %
            ("gtfs2_stop_times" if interned else "stop_times")).fetchone()[0]

        # the network-wide tables, harmless to re-run: insert or ignore leans
        # on the primary keys pygtfs already declares
        for table in SHARED_TABLES:
            if table in present:
                cur.execute(
                    f"insert or ignore into {table} select * from scratch.{table}")  # noqa: S608

        cur.execute("insert or ignore into trips select * from scratch.trips "
                    "where route_id = ?", (route_id,))

        if interned:
            # mint the keys here, above what this database already uses, and
            # only for identifiers it does not know yet
            cur.execute("""
                insert into gtfs2_trip_key(tk, trip_id)
                select (select coalesce(max(tk), 0) from gtfs2_trip_key)
                       + row_number() over (order by t.trip_id), t.trip_id
                from scratch.trips t
                where t.route_id = ?
                  and t.trip_id not in (select trip_id from gtfs2_trip_key)
            """, (route_id,))
            cur.execute("""
                insert into gtfs2_stop_key(sk, stop_id)
                select (select coalesce(max(sk), 0) from gtfs2_stop_key)
                       + row_number() over (order by x.stop_id), x.stop_id
                from (select distinct st.stop_id
                      from scratch.stop_times st
                      join scratch.trips t on t.trip_id = st.trip_id
                      where t.route_id = ?) x
                where x.stop_id not in (select stop_id from gtfs2_stop_key)
            """, (route_id,))
            cur.execute("""
                insert or ignore into gtfs2_stop_times(
                    tk, stop_sequence, sk, feed_id, arrival_time, departure_time,
                    stop_headsign, pickup_type, drop_off_type,
                    shape_dist_traveled, timepoint)
                select k.tk, st.stop_sequence, sk.sk, st.feed_id,
                       st.arrival_time, st.departure_time, st.stop_headsign,
                       st.pickup_type, st.drop_off_type,
                       st.shape_dist_traveled, st.timepoint
                from scratch.stop_times st
                join scratch.trips t on t.trip_id = st.trip_id and t.route_id = ?
                join gtfs2_trip_key k on k.trip_id = st.trip_id
                join gtfs2_stop_key sk on sk.stop_id = st.stop_id
            """, (route_id,))
            after = cur.execute("select count(*) from gtfs2_stop_times").fetchone()[0]
        else:
            cur.execute("""
                insert or ignore into stop_times
                select st.* from scratch.stop_times st
                join scratch.trips t on t.trip_id = st.trip_id
                where t.route_id = ?
            """, (route_id,))
            after = cur.execute("select count(*) from stop_times").fetchone()[0]

        # the tables that hang off trips, when the feed carries them
        for table, column in (("frequencies", "trip_id"),
                              ("_trip_shapes", "trip_id")):
            if table in present:
                cur.execute(
                    f"insert or ignore into {table} select * from scratch.{table} "  # noqa: S608
                    f"where {column} in (select trip_id from scratch.trips "
                    "where route_id = ?)", (route_id,))

        conn.commit()
    except sqlite3.Error as ex:
        _LOGGER.error("Could not copy route %s: %s", route_id, ex)
        conn.rollback()
        return None
    finally:
        try:
            conn.execute("detach database scratch")
        except sqlite3.Error:
            pass
        conn.close()

    added = after - before
    _LOGGER.info("Copied route %s: %s stop_times added", route_id, added)
    return added



def routes_in(db_file):
    """The route_ids a database actually carries trips for."""
    if not os.path.exists(db_file):
        return set()
    conn = sqlite3.connect(db_file, timeout=60)
    try:
        return {r[0] for r in conn.execute("select distinct route_id from trips")}
    except sqlite3.Error as ex:
        _LOGGER.warning("Could not read routes from %s: %s", db_file, ex)
        return set()
    finally:
        conn.close()


def import_routes(gtfs_dir, filename, route_ids, build_scratch):
    """Bring routes into the real database, through the scratch one.

    The whole point of the two file model lives here: the feed is unpacked into
    a file the sensors never open, the wanted routes are copied across, and the
    scratch file goes away. Whatever happens, the real database is either
    untouched or has gained routes - it is never left half built.

    build_scratch is a callable taking the scratch path and doing the actual
    unpacking; it is passed in rather than imported so this module keeps no
    dependency on pygtfs or on Home Assistant.

    Returns {route_id: stop_times added}, or None when the scratch build failed.
    """
    real = real_path(gtfs_dir, filename)
    scratch = scratch_path(gtfs_dir, filename)
    # a scratch file left by an interrupted run holds an unknown state
    discard_scratch(gtfs_dir, filename)

    try:
        if not build_scratch(scratch):
            _LOGGER.error("Could not build the import database for %s", filename)
            return None
        if not os.path.exists(scratch):
            _LOGGER.error("The import database was not created: %s", scratch)
            return None

        fresh = not os.path.exists(real)
        if fresh and not create_real_from(scratch, real):
            return None

        added = {}
        for route_id in route_ids:
            count = copy_route(real, scratch, route_id)
            if count is None:
                # the copy is one transaction per route: the routes already
                # brought in stay, and the caller is told which ones made it
                _LOGGER.error("Import of route %s failed, stopping there", route_id)
                break
            added[route_id] = count
        return added
    finally:
        discard_scratch(gtfs_dir, filename)


def discard_scratch(gtfs_dir, filename):
    """Delete the scratch database and whatever SQLite left beside it.

    Called when an import ends, whether it worked or not: the real database is
    untouched either way, which is the whole point of importing elsewhere.
    """
    base = scratch_path(gtfs_dir, filename)
    for path in (base, base + "-journal", base + "-wal", base + "-shm"):
        if os.path.exists(path):
            try:
                os.remove(path)
                _LOGGER.debug("Removed scratch file: %s", path)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", path, ex)


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


def optimise_datasource(gtfs_dir, filename, keep_routes=None):
    """Shrink a datasource: drop what is not followed, then intern the rest.

    Two steps that only make sense together, and in this order. Pruning first
    means interning has less to rewrite; interning first would mint keys for
    rows about to be deleted.

    Measured after importing two Orleans lines: 85.7 MB down to 32.1 MB in
    four seconds, and stop_times comes back as a view over the interned rows,
    so every query in the integration keeps working unchanged.

    keep_routes is optional: without it nothing is pruned and only the
    interning runs, which is the right thing right after an import that
    brought in exactly what was wanted.

    Returns {"pruned": stats or None, "interned": stats or None}.
    """
    out = {"pruned": None, "interned": None}
    if keep_routes:
        out["pruned"] = prune_gtfs_datasource(gtfs_dir, filename, keep_routes)
    out["interned"] = intern_gtfs_datasource(gtfs_dir, filename)
    # an import leaves nothing behind, but a run interrupted between the two
    # steps might have, and this is the natural place to notice
    discard_scratch(gtfs_dir, filename)
    return out


def prune_gtfs_datasource(gtfs_dir, filename, keep_routes, dry_run=False):
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
    sqlite_file = os.path.join(gtfs_dir, filename + ".sqlite")
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
            _LOGGER.error("Cannot prune %s: routes %s match no trips, aborting to avoid data loss",
                          filename, keep_routes)
            return None

        # the services those trips run on, collected once and keyed, so the
        # calendar rebuilds below probe an index instead of scanning trips
        cur.execute("create temp table gtfs2_keep_services("
                    "feed_id integer, service_id varchar, "
                    "primary key(feed_id, service_id)) without rowid")
        cur.execute("insert into gtfs2_keep_services "
                    "select distinct t.feed_id, t.service_id from trips t "
                    "inner join gtfs2_keep k "
                    "on k.feed_id = t.feed_id and k.trip_id = t.trip_id")

        stats = {"file": filename, "routes": sorted(keep_routes), "dry_run": dry_run,
                 "trips_before": total_trips, "trips_after": kept_trips,
                 "size_before_mb": round(size_before / 1048576, 1)}

        for table, feed_col in PRUNE_TRIP_DEPENDENTS:
            if not _table_has_columns(cur, table, feed_col, "trip_id"):
                _LOGGER.debug("Pruning %s: skipping absent or unexpected table %s", filename, table)
                continue
            before = cur.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
            if dry_run:
                after = cur.execute(
                    f"select count(*) from {table} t inner join gtfs2_keep k "  # noqa: S608
                    f"on k.feed_id = t.{feed_col} and k.trip_id = t.trip_id").fetchone()[0]
            else:
                _rebuild_keep(cur, table, "exists (select 1 from gtfs2_keep k "
                              f"where k.feed_id = src.{feed_col} and k.trip_id = src.trip_id)")
                after = cur.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
            stats[f"{table}_before"], stats[f"{table}_after"] = before, after

        for table, feed_col in PRUNE_SERVICE_DEPENDENTS:
            if not _table_has_columns(cur, table, feed_col, "service_id"):
                _LOGGER.debug("Pruning %s: skipping absent or unexpected table %s",
                              filename, table)
                continue
            orphan = _ORPHAN_SERVICE.format(table=table, feed_col=feed_col)
            before = cur.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
            if dry_run:
                kept = cur.execute(
                    f"select count(*) from {table} where not {orphan}").fetchone()[0]  # noqa: S608
                after = kept
            else:
                _rebuild_keep(cur, table, "not " + _ORPHAN_SERVICE.format(
                    table="src", feed_col=feed_col))
                after = cur.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
            stats[f"{table}_before"], stats[f"{table}_after"] = before, after

        # an interned datasource keeps its stop_times in gtfs2_stop_times, keyed
        # by tk, and exposes the original shape as a view. _table_has_columns
        # skips the view, so the rows have to be removed here instead.
        if _table_has_columns(cur, "gtfs2_stop_times", "tk"):
            keep_tk = ("select k.tk from gtfs2_trip_key k "
                       "inner join gtfs2_keep g on g.trip_id = k.trip_id")
            before = cur.execute("select count(*) from gtfs2_stop_times").fetchone()[0]
            if dry_run:
                after = cur.execute(
                    f"select count(*) from gtfs2_stop_times where tk in ({keep_tk})"  # noqa: S608
                ).fetchone()[0]
            else:
                _rebuild_keep(cur, "gtfs2_stop_times", f"src.tk in ({keep_tk})")
                # the key tables hold the long identifiers interning removed
                # from every row: leaving them behind keeps most of the weight
                _rebuild_keep(cur, "gtfs2_trip_key",
                              "src.tk in (select tk from gtfs2_stop_times)")
                _rebuild_keep(cur, "gtfs2_stop_key",
                              "src.sk in (select sk from gtfs2_stop_times)")
                after = cur.execute("select count(*) from gtfs2_stop_times").fetchone()[0]
            stats["gtfs2_stop_times_before"] = before
            stats["gtfs2_stop_times_after"] = after

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
        _LOGGER.error("Failed to prune datasource %s: %s", filename, ex)
        conn.rollback()
        return None
    finally:
        conn.close()

    stats["size_after_mb"] = round(os.path.getsize(sqlite_file) / 1048576, 1)
    _LOGGER.info("Pruned datasource %s: %s MB -> %s MB, %s of %s trips kept",
                 filename, stats["size_before_mb"], stats["size_after_mb"],
                 stats["trips_after"], stats["trips_before"])
    return stats


def _rebuild_keep(cur, table, keep_where, params=()):
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


def _table_has_columns(cur, table, *columns):
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


def intern_gtfs_datasource(gtfs_dir, filename, dry_run=False):
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
    sqlite_file = os.path.join(gtfs_dir, filename + ".sqlite")
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

        stats = {"file": filename, "dry_run": dry_run, "rows": rows,
                 "size_before_mb": round(size_before / 1048576, 1),
                 "trip_ids": cur.execute("select count(distinct trip_id) from stop_times").fetchone()[0],
                 "stop_ids": cur.execute("select count(distinct stop_id) from stop_times").fetchone()[0]}
        if dry_run:
            _LOGGER.info("Interning %s (dry run): %s", filename, stats)
            return stats

        # columns carried over as-is, in their original order minus the interned pair
        carried = [c for c in columns if c not in ("trip_id", "stop_id", "stop_sequence")]
        carried_ddl = ", ".join(f"{c} {_column_type(cur, 'stop_times', c)}" for c in carried)

        cur.execute("create table gtfs2_trip_key (tk integer primary key, "
                    "trip_id varchar not null unique)")
        cur.execute("insert into gtfs2_trip_key(trip_id) select distinct trip_id from stop_times")
        cur.execute("create table gtfs2_stop_key (sk integer primary key, "
                    "stop_id varchar not null unique)")
        cur.execute("insert into gtfs2_stop_key(stop_id) select distinct stop_id from stop_times")

        cur.execute(f"create table gtfs2_stop_times (tk integer not null, "  # noqa: S608
                    f"stop_sequence integer not null, sk integer not null, {carried_ddl}, "
                    f"primary key (tk, stop_sequence)) without rowid")
        cur.execute(f"insert into gtfs2_stop_times select k.tk, st.stop_sequence, s.sk, "  # noqa: S608
                    f"{', '.join('st.' + c for c in carried)} from stop_times st "
                    f"join gtfs2_trip_key k on k.trip_id = st.trip_id "
                    f"join gtfs2_stop_key s on s.stop_id = st.stop_id")

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
        conn.commit()
        conn.isolation_level = None
        cur.execute("drop table if exists sqlite_stat1")
        cur.execute("vacuum")
    except sqlite3.Error as ex:
        _LOGGER.error("Failed to intern datasource %s: %s", filename, ex)
        conn.rollback()
        return None
    finally:
        conn.close()

    stats["size_after_mb"] = round(os.path.getsize(sqlite_file) / 1048576, 1)
    _LOGGER.info("Interned datasource %s: %s MB -> %s MB, %s rows, %s trip_ids and %s stop_ids "
                 "stored once instead of three times per row", filename,
                 stats["size_before_mb"], stats["size_after_mb"], stats["rows"],
                 stats["trip_ids"], stats["stop_ids"])
    return stats


def _column_type(cur, table, column):
    """Return the declared type of a column, defaulting to no affinity."""
    for row in cur.execute(f"pragma table_info({table})"):
        if row[1] == column:
            return row[2] or ""
    return ""
