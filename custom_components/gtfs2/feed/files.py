"""The files a gtfs2 source is made of, and what the modules that open its
database directly build on.

pygtfs is a loader, not a database layer: `append_feed` imports a zip and the
`*_by_id` helpers read a few objects back, but it offers no way to delete, to
move rows between files, or to reshape a table. Measured on this codebase: 30
hand written queries and 18 raw writes, none of them going through pygtfs. So
that work already existed, scattered through gtfs_helper.py; this module and
the db_* ones give it a home.

Four things live here, or start from here:

  the two database model    real_path / scratch_path / staging_name here,
                            the import and the swap in db_build.py
  reshaping a datasource    shrink.py
  the sources on disk       get_datasources / get_zipfiles / remove_datasource
  letting a schedule go     close_schedule

They belong together because they answer the same question - what is physically
in the file, and which files are there.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import TYPE_CHECKING

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


def remove_database(path: str) -> None:
    """Remove a database file and whatever SQLite left beside it."""
    remove_files(path, path + "-journal", path + "-wal", path + "-shm")


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
        # the filtered copy an import or a refresh leaves while it runs,
        # named after the database it builds
        and not f.endswith(".sqlite.zip")
    )
    _LOGGER.debug(f"Zip files in folder: {zipfiles}")
    return zipfiles


def remove_datasource(hass: HomeAssistant, path: str, filename: str, include_sqlite: bool) -> str:
    """Remove the files of a datasource."""
    gtfs_dir = hass.config.path(path)
    _LOGGER.info(f"Removing datasource: {os.path.join(gtfs_dir, filename)}.*")
    suffixes = [".sqlite"] if include_sqlite else []
    suffixes += ["_temp.zip", "_temp_out.zip", ".sqlite-journal", ".zip",
                 # the sidecar follows the zip it describes, and so does
                 # the index of its trains (stations.rail_index)
                 ".zip.meta.json", ".zip.rail", ".zip.rail.new",
                 # and the shapes of its lines the leg files read (geojson.route_shapes)
                 ".zip.shapes"]
    # what the fork keeps beside a source: the record of the installed
    # edition, and what a download, a refresh or an import stopped half way
    # leaves. Left behind, the record made a new source of the same name
    # look already built from an edition it never had
    # (.extracting: the marker of a legacy extract an older version left)
    suffixes += [".zip.new", ".extracting", ".refresh.sqlite", ".refresh.sqlite-journal",
                 ".import.sqlite", ".import.sqlite-journal", ".import.sqlite.zip",
                 # the network an envelope download took out, the filtered
                 # zip of a whole-feed refresh, and the scratch of a route by
                 # route one, an import under the staging name
                 ".zip.new.inner", ".refresh.sqlite.zip", ".refresh.import.sqlite",
                 ".refresh.import.sqlite-journal", ".refresh.import.sqlite.zip"]
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
