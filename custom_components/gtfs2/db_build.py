"""The two database model: an import goes into a scratch database, the
followed routes are copied from it into the real one (import_routes,
copy_route, create_real_from), and a rebuilt file is swapped in once whole
(swap_in, on_a_copy).

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

from collections.abc import Callable, Collection, Iterable
import logging
import os
import sqlite3
from typing import Any

from .db_intern import intern_gtfs_datasource
from .db_prune import prune_gtfs_datasource
from .gtfs_db import real_path, remove_database, remove_files, scratch_path, staging_name

_LOGGER = logging.getLogger(__name__)


# how long the swap waits for another writer to finish before giving up and
# leaving the current data in place: long enough for an index or an intern,
# short enough not to hold the refresh behind a VACUUM of several minutes
SWAP_TIMEOUT = 30


# copied whole: they describe the network, not a route, and stay small. Order
# matters only for readability, since pygtfs declares foreign keys but SQLite
# does not enforce them here (pragma foreign_keys is 0).
SHARED_TABLES = ("_feed", "agency", "stops", "calendar", "calendar_dates",
                 "feed_info", "routes", "shapes", "transfers",
                 "fare_attributes", "fare_rules", "translations")


def _tables(cur: sqlite3.Cursor) -> set[str]:
    return {r[0] for r in cur.execute(
        "select name from sqlite_master where type = 'table'")}


def _is_interned(cur: sqlite3.Cursor) -> bool:
    """Whether this database keeps its stop_times interned behind a view."""
    return bool(cur.execute(
        "select 1 from sqlite_master where type = 'view' and name = 'stop_times'"
    ).fetchone())


def create_real_from(scratch_file: str, real_file: str) -> bool:
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
        _LOGGER.exception("Could not create datasource %s: %s", real_file, ex)
        dst.close()
        # a half-built file would be taken for a real datasource
        os.remove(real_file)
        return False
    dst.close()
    return True


def _drop_side_files(real_file: str) -> None:
    """Remove what SQLite may have left beside a file just swapped out.

    Named after the file, not after the data: a journal still there once the
    rename is done belongs to the database that just lost its name, but the
    next reader would take it for the new one's and replay it into it. The
    exclusive lock is what makes this safe to do: no live transaction can be
    holding one.
    """
    remove_files(real_file + "-journal", real_file + "-wal", real_file + "-shm")


def swap_in(new_file: str, real_file: str, timeout: float = SWAP_TIMEOUT) -> bool:
    """Put a rebuilt database in place of the real one, no writer in between.

    A rename is invisible to SQLite: a writer holding a transaction on the
    old file goes on writing, and its journal, replayed against the new
    file, takes it back to the old contents. So the swap happens while
    holding SQLite's own exclusive lock, which every writer respects
    whatever process it runs in, the forked extract included: one already
    writing keeps us out, and the current data stays; one arriving later is
    kept out of a file that is about to lose its name, and writes into the
    unlinked old one, which harms nothing. Taking the lock also rolls back
    a journal left behind by a crash, so nothing hot survives the rename.

    Returns True when the swap happened.
    """
    conn: sqlite3.Connection | None
    conn = sqlite3.connect(real_file, timeout=timeout)
    try:
        try:
            conn.execute("begin exclusive")
        except sqlite3.Error as ex:
            _LOGGER.exception("Could not take %s to swap it, something is writing "
                          "to it: %s", real_file, ex)
            return False
        try:
            os.replace(new_file, real_file)
            _drop_side_files(real_file)
            return True
        except OSError as ex:
            # Windows refuses to replace a file this process still holds
            # open; Linux, where Home Assistant runs, renames over it. The
            # lock is dropped first, so the swap is unguarded for the few
            # microseconds of the rename, and only on a developer's box.
            _LOGGER.debug("Swapping %s under the lock failed (%s), letting go "
                          "of it first", real_file, ex)
        conn.rollback()
        conn.close()
        conn = None
        os.replace(new_file, real_file)
        _drop_side_files(real_file)
        return True
    except OSError as ex:
        _LOGGER.exception("Could not swap %s in: %s", new_file, ex)
        return False
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()


def copy_route(real_file: str, scratch_file: str, route_id: str, shared: bool = True) -> int | None:
    """Copy one route from the scratch database into the real one.

    Runs entirely in SQLite, through ATTACH: no round trip through pygtfs and
    no serialisation. The route's stop_times are interned on the way in when
    the real database is interned, which is what removes any key remapping.

    shared False leaves the network-wide tables alone: an import of several
    routes copies them with the first one, and every further pass read them
    whole again only to insert nothing.

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

        # the network-wide tables, harmless to re-run: insert or ignore leans
        # on the primary keys pygtfs already declares
        for table in SHARED_TABLES if shared else ():
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
            # the rows this insert added, where two full counts of the table
            # around it grew with every route already in
            added = cur.rowcount
        else:
            cur.execute("""
                insert or ignore into stop_times
                select st.* from scratch.stop_times st
                join scratch.trips t on t.trip_id = st.trip_id
                where t.route_id = ?
            """, (route_id,))
            added = cur.rowcount

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
        _LOGGER.exception("Could not copy route %s: %s", route_id, ex)
        conn.rollback()
        return None
    finally:
        try:
            conn.execute("detach database scratch")
        except sqlite3.Error:
            pass
        conn.close()

    _LOGGER.info("Copied route %s: %s stop_times added", route_id, added)
    return added


def import_routes(gtfs_dir: str, filename: str, route_ids: Iterable[str],
                  build_scratch: Callable[[str], object]) -> dict[str, int] | None:
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
        _index_scratch(scratch)

        added: dict[str, int] = {}
        for position, route_id in enumerate(route_ids):
            count = copy_route(real, scratch, route_id, shared=position == 0)
            if count is None:
                # the copy is one transaction per route: the routes already
                # brought in stay, and the caller is told which ones made it
                _LOGGER.error("Import of route %s failed, stopping there", route_id)
                break
            added[route_id] = count
        if fresh and not added:
            # nothing came into the file this import created: left with its
            # schema only, it read as a datasource that follows no line,
            # which the flows then sent down the legacy extract
            remove_database(real)
        return added
    finally:
        discard_scratch(gtfs_dir, filename)


def _index_scratch(scratch_file: str) -> None:
    """Index the scratch database for the copy, which reads it by route.

    pygtfs keys stop_times on (feed_id, trip_id, stop_sequence) and leaves
    trips.route_id bare, so each route copied scanned the whole of
    stop_times: measured on the Orleans feed, 41 routes and 2 M stop_times,
    206 s. With these two indexes, built in 9 s, the same copy takes 16 s.
    The scratch file goes away with the import, and its indexes with it:
    the real database's schema is untouched. A failure only costs speed.
    """
    conn = sqlite3.connect(scratch_file)
    try:
        conn.execute("create index if not exists gtfs2_scratch_trip on stop_times(trip_id)")
        conn.execute("create index if not exists gtfs2_scratch_route on trips(route_id)")
        conn.commit()
    except sqlite3.Error as ex:
        _LOGGER.warning("Could not index %s, copying without: %s", scratch_file, ex)
    finally:
        conn.close()


def discard_scratch(gtfs_dir: str, filename: str) -> None:
    """Delete the scratch database and whatever SQLite left beside it.

    Called when an import ends, whether it worked or not: the real database is
    untouched either way, which is the whole point of importing elsewhere.
    """
    remove_database(scratch_path(gtfs_dir, filename))


def on_a_copy[T](gtfs_dir: str, filename: str, work: Callable[..., T], *args: object,
                 done: Callable[[T], object] = bool) -> T | None:
    """Run a rewrite of a datasource on a copy of it, then swap the copy in.

    Prune and intern delete rows by the million and VACUUM, and on the live
    file each holds SQLite's exclusive lock for the whole rewrite: every
    sensor read waited behind it, minutes on a national feed. The copy is
    SQLite's own backup, a consistent snapshot whatever reads meanwhile;
    the work runs on it under the refresh's staging name, and the result
    takes the real file's place the way a refresh does, so the sensors see
    the old data or the new, never a file being rewritten.

    work(gtfs_dir, name, *args) is called with the staging name. done says,
    from what it returned, whether it changed anything: nothing changed,
    nothing is swapped. Returns what work returned, None when the copy or
    the swap failed.
    """
    real = real_path(gtfs_dir, filename)
    staging = staging_name(filename)
    copy = real_path(gtfs_dir, staging)
    remove_database(copy)
    try:
        src = sqlite3.connect(real, timeout=60)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        result = work(gtfs_dir, staging, *args)
        if not done(result):
            return result
        if not swap_in(copy, real):
            return None
    except (sqlite3.Error, OSError) as ex:
        _LOGGER.exception("Could not rewrite %s on a copy: %s", filename, ex)
        return None
    finally:
        remove_database(copy)
    # the stats name the file they were made on: the staging copy
    for stats in (result, *(result.values() if isinstance(result, dict) else ())):
        if isinstance(stats, dict) and stats.get("file") == staging:
            stats["file"] = filename
    return result


def optimise_datasource(gtfs_dir: str, filename: str,
                        keep_routes: Collection[str] | None = None) -> dict[str, dict[str, Any] | None]:
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
    out: dict[str, dict[str, Any] | None] = {"pruned": None, "interned": None}
    if keep_routes:
        out["pruned"] = prune_gtfs_datasource(gtfs_dir, filename, keep_routes)
    out["interned"] = intern_gtfs_datasource(gtfs_dir, filename)
    # an import leaves nothing behind, but a run interrupted between the two
    # steps might have, and this is the natural place to notice
    discard_scratch(gtfs_dir, filename)
    return out
