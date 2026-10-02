"""A source's database as the readers open it: the schedule, or why there
is none (get_gtfs), whether something is writing to it (check_extracting),
and the indexes the queries lean on (check_datasource_index,
drop_import_indexes).
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
import os
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
import pygtfs
from sqlalchemy.sql import text

from .gtfs_db import feed_zip, file_edition, real_path

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


def get_gtfs(hass: HomeAssistant, path: str, data: Mapping[str, Any]) -> Schedule | str:
    """Open a datasource's database, or say why there is none to open.

    Answers the schedule, or one of the strings the callers know:
    "extracting" while something writes to the file (an import, an index
    build, an intern); for a source with no database, or one without a feed
    in it, "not_built" when its zip is there to build it from and
    "no_zip_file" when it is not.

    Nothing is downloaded or built here. A database is built by the flow's
    import or by a refresh of the source (refresh_datasource), both under
    the source's lock, into a file of their own swapped in once whole. This
    used to download a missing feed and import the whole network into the
    real file, in place, in a forked process that outlived the lock, from
    whichever sensor, service or screen found the database missing.
    """
    gtfs_dir = hass.config.path(path)
    filename = data["file"]
    if check_extracting(hass, gtfs_dir, filename):
        _LOGGER.debug("Cannot use this datasource as still unpacking: %s", filename)
        return "extracting"
    sqlite = real_path(gtfs_dir, filename)
    # not opened when missing: opening creates an empty file, taken for a
    # datasource next time
    if os.path.exists(sqlite):
        gtfs = pygtfs.Schedule(f"{sqlite}?check_same_thread=False&timeout=60")
        if gtfs.feeds:
            return gtfs
        gtfs.engine.dispose()
    _LOGGER.debug("Datasource %s has no timetable: a refresh of the source builds it", filename)
    if not os.path.exists(feed_zip(gtfs_dir, filename)):
        return "no_zip_file"
    return "not_built"


# the tables an import leaves out: the integration never reads them from
# the database (a line's shape is read from the zip), pygtfs pays for every
# row, and it models the old form of translations.txt (trans_id, lang) that
# today's feeds do not write. They stay in the zip, which is kept as the
# host sent it: pygtfs skips them on the way in
IMPORT_IGNORED = ("shapes.txt", "transfers.txt", "fare_attributes.txt",
                  "levels.txt", "pathways.txt", "translations.txt")


def check_extracting(hass: HomeAssistant, gtfs_dir: str, file: str) -> bool:
    _LOGGER.debug("Checking if extracting: %s", file)
    gtfs_dir = hass.config.path(gtfs_dir)
    filename = file
    journal = os.path.join(gtfs_dir, filename + ".sqlite-journal")
    # (a _temp.zip, the name the zip took while an older version rewrote
    # it in place, is left over from then and no sign of a write any more:
    # nothing produces it, and it held the source "extracting" for ever)
    if os.path.exists(journal):
        _LOGGER.debug("Extracting: yes")
        return True
    return False    


# the indexes the queries lean on, by table and column, under the names
# they have always been created with
DATASOURCE_INDEXES = (
    ("stop_times", "trip_id", "gtfs2_stop_times_trip_id"),
    ("stop_times", "stop_id", "gtfs2_stop_times_stop_id"),
    ("shapes", "shape_id", "gtfs2_shapes_shape_id"),
    ("stops", "stop_name", "gtfs2_stops_stop_name"),
    ("routes", "route_type", "gtfs2_routes_route_type"),
    ("trips", "route_id", "gtfs2_trips_route_id"),
)


# the database file each datasource was last checked as, (inode, mtime,
# size): the same file needs no second look, a rebuilt one gets one
_INDEX_CHECKED: dict[str, tuple[int, int, int]] = {}


def drop_import_indexes(schedule: Schedule) -> None:
    """Take the stop_times indexes off a database pygtfs is about to fill.

    From 0.1.10 on pygtfs declares trip_id and stop_id indexes on
    stop_times and creates them with the table, so SQLite would update both
    at every row an import inserts, millions on a large feed. Without them
    the rows go in bare and the indexes are built afterwards, in one pass
    each, as upstream chose ("apply indexes at end of extracting"): by
    check_datasource_index on a datasource, by the import's own
    _index_scratch on a scratch database. Only for a database still empty.
    """
    with schedule.engine.begin() as conn:
        names = [name for (name,) in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'stop_times' AND sql IS NOT NULL")).fetchall()]
        for name in names:
            conn.execute(text(f'DROP INDEX "{name}"'))


def check_datasource_index(hass: HomeAssistant, schedule: Schedule | str | None, gtfs_dir: str,
                           file: str) -> None:
    """Give a datasource the indexes the queries need, and its routes an agency.

    Runs before every refresh of every sensor, and asked sqlite_master
    seven times over as many connections each time. Now one connection
    reads it once, and a database file already checked is not read again
    until it changes.
    """
    _LOGGER.debug("Check datasource index for file: %s", file)
    if check_extracting(hass, gtfs_dir,file):
        _LOGGER.warning("Cannot check indexes on this datasource as still unpacking: %s", file)
        return
    # runs before get_next_departure on every refresh, so it meets the same
    # sentinels get_gtfs leaves in place of a schedule
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Cannot check indexes: datasource %s has no usable schedule (%s)", file, schedule or "empty")
        return
    db_file = real_path(hass.config.path(gtfs_dir), file)
    edition = file_edition(db_file)
    if edition is not None and _INDEX_CHECKED.get(db_file) == edition:
        return

    # A single-agency feed may leave agency_id out of routes.txt, and out
    # of agency.txt as well (TAO does): then there is nothing to copy, the
    # two tables already agree on the missing value, and copying it back
    # would only log the "fix" again at every refresh. So only count the
    # routes when the agency table has an id to give them.
    sql_check_route_agency = """
    SELECT count(*) as check_agency
    FROM routes where (agency_id='None' or agency_id is null)
    and exists (select 1 from agency
                where agency_id is not null and agency_id not in ('None', ''))
    """
    sql_fix_route_agency = """
    update routes set agency_id = (select agency_id from agency
                                   where agency_id is not null
                                   and agency_id not in ('None', '') limit 1)
        where agency_id='None' or agency_id is null
    """
    with schedule.engine.connect() as conn:
        master = conn.execute(text(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE type in ('index', 'view')")).fetchall()
        # an interned datasource exposes stop_times as a view: its indexes
        # live on gtfs2_stop_times and must not be recreated here
        views = {name for kind, name, _table in master if kind == "view"}
        indexed = [(table, name) for kind, name, table in master if kind == "index"]
        for table, column, index_name in DATASOURCE_INDEXES:
            if table in views or any(t == table and column in (n or "") for t, n in indexed):
                continue
            _LOGGER.info("Adding index %s to improve performance", index_name)
            conn.execute(text(f"create index {index_name} on {table}({column})"))  # noqa: S608
        if conn.execute(text(sql_check_route_agency)).scalar():
            _LOGGER.info("Fix missing agency_id in routes table")
            conn.execute(text(sql_fix_route_agency))
        conn.commit()
    # the edition the checks leave, indexes made
    edition = file_edition(db_file)
    if edition is not None:
        _INDEX_CHECKED[db_file] = edition
