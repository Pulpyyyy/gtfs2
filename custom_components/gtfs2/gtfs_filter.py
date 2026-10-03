"""Cut a GTFS zip down to chosen routes, before anything imports it.

pygtfs loads whatever the zip holds, row by row, through the ORM. On a
national feed that is the wrong place to choose: importing gtfs-nl.zip whole
means 15.1 million stop_times and a 2.6 GB file, when the routes actually
followed account for a few thousand rows. Filtering the zip first costs one
streaming pass over each table - measured at 40 seconds for the Dutch
national feed against minutes to hours for the full import - and everything
downstream (scratch database, route copy, intern, prune) then runs on a file
of the right size.

The filter keeps whole the tables that describe the network - agency.txt,
routes.txt, feed_info.txt - so the route selector keeps seeing every line of
the feed, exactly the invariant the prune preserves on the database side.
The tables that carry the weight are cut to the chosen routes: trips, then
stop_times, frequencies, stops (with their parent stations), calendar and
calendar_dates through the surviving service_ids. Tables the integration
strips before import anyway (shapes, transfers, fares, pathways, levels,
translations) are simply never copied.

Everything here reads and writes zips: no Home Assistant, no pygtfs, no
database, which is what keeps it loadable by the test harness on its own.
"""
from __future__ import annotations

from collections.abc import Callable, Container, Iterable, Iterator
import csv
import io
import logging
import os
import time
from typing import IO, Any, Literal, Self
import zipfile

_LOGGER = logging.getLogger(__name__)

# copied as they are: small, and the whole network must stay visible
KEPT_WHOLE = ("agency.txt", "routes.txt", "feed_info.txt")
# the zip is the only complete record of the feed, so the filter must never
# write into it: output always goes to a separate file


def _member(zin: zipfile.ZipFile, name: str) -> str | None:
    """The archive member for a table, wherever the feed nested it."""
    return next((n for n in zin.namelist()
                 if n.rsplit("/", 1)[-1] == name), None)


def _rows(zin: zipfile.ZipFile, member: str) -> Iterator[list[str]]:
    """Stream a member as csv rows, header first, byte order mark eaten.

    Blank lines are left out: a table ending with one, which plenty of
    feeds do, reads as a row with no column at all and breaks every index
    taken on the header.
    """
    reader = csv.reader(io.TextIOWrapper(
        zin.open(member), encoding="utf-8-sig", newline=""))
    return (row for row in reader if row)


def table_reader(raw: IO[bytes]) -> csv.DictReader[str]:
    """csv.DictReader over a feed table, its columns named as pygtfs names them.

    pygtfs strips every cell it imports, the header included. Renfe pads
    each line of its tables to a fixed width, so the last column of its
    calendar is "end_date" followed by a hundred spaces: read as written,
    row.get("end_date") found nothing, where the database has the date.
    """
    reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
    reader.fieldnames = [name.strip() for name in reader.fieldnames or []]
    return reader


def table_rows(zin: zipfile.ZipFile, name: str) -> Iterator[dict[str, str | None]]:
    """The rows of a table of an open feed, wherever the feed nested it,
    read by table_reader; a table the feed leaves out reads as no row."""
    member = _member(zin, name)
    if member is None:
        return
    with zin.open(member) as raw:
        yield from table_reader(raw)


def _header(rows: Iterator[list[str]], name: str) -> list[str]:
    """The header row of a table, or the end of the filtering.

    A table with not one line has no columns to filter on. Saying so as a
    ValueError puts it where a feed missing that table already lands: the
    caller keeps the feed whole rather than writing half of it. The names
    are stripped as table_reader strips them, and the filter strips the
    keys it compares, as pygtfs strips every cell: in a feed padded to a
    fixed width the last column carries the padding in its name and in
    its values, and a padded parent_station would otherwise leave every
    platform without its station. The rows are copied as they are.
    """
    header = next(rows, None)
    if header is None:
        raise ValueError(f"{name} carries no header")
    return [column.strip() for column in header]


class _Writer:
    """One filtered table on its way into the output zip, streamed.

    zipfile buffers nothing on an open("w") handle, so even the national
    stop_times never sits in memory: rows go straight through.
    """

    def __init__(self, zout: zipfile.ZipFile, name: str, header: list[str]) -> None:
        self._handle = zout.open(name, "w")
        self._wrapper = io.TextIOWrapper(
            self._handle, encoding="utf-8", newline="")
        self._csv = csv.writer(self._wrapper, lineterminator="\n")
        self._csv.writerow(header)

    def row(self, row: list[str]) -> None:
        self._csv.writerow(row)

    def close(self) -> None:
        self._wrapper.flush()
        self._wrapper.detach()
        self._handle.close()

    # used as a context manager so that a table breaking halfway still
    # closes its handle: a zip cannot even be closed, let alone deleted,
    # while one is open on it
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False


def _copy_filtered(zin: zipfile.ZipFile, zout: zipfile.ZipFile, member: str, name: str,
                   keep: Callable[..., bool]) -> tuple[int, int]:
    """Copy one table keeping the rows keep() accepts. Returns (kept, total)."""
    rows = _rows(zin, member)
    header = _header(rows, name)
    kept = total = 0
    with _Writer(zout, name, header) as out:
        for row in rows:
            total += 1
            if keep(row):
                out.row(row)
                kept += 1
    return kept, total


def _filter_trips(zin: zipfile.ZipFile, zout: zipfile.ZipFile, member: str,
                  route_ids: Container[str]) -> tuple[set[str], set[str], int]:
    """Copy the trips of the chosen routes: it decides everything else that
    survives. Returns (their trip_ids, their service_ids, trips read)."""
    trip_ids, service_ids = set(), set()
    rows = _rows(zin, member)
    header = _header(rows, "trips.txt")
    i_route = header.index("route_id")
    i_trip = header.index("trip_id")
    i_service = header.index("service_id")
    total = 0
    with _Writer(zout, "trips.txt", header) as out:
        for row in rows:
            total += 1
            if row[i_route].strip() in route_ids:
                out.row(row)
                trip_ids.add(row[i_trip].strip())
                service_ids.add(row[i_service].strip())
    return trip_ids, service_ids, total


def _filter_stop_times(zin: zipfile.ZipFile, zout: zipfile.ZipFile, member: str,
                       trip_ids: Container[str]) -> tuple[set[str], int, int]:
    """Copy the calls of the kept trips. stop_times is the weight of the
    feed: one pass, collecting the stops they call at. Returns (those
    stop_ids, calls kept, calls read)."""
    stop_ids = set()
    rows = _rows(zin, member)
    header = _header(rows, "stop_times.txt")
    i_trip = header.index("trip_id")
    i_stop = header.index("stop_id")
    kept = total = 0
    with _Writer(zout, "stop_times.txt", header) as out:
        for row in rows:
            total += 1
            if row[i_trip].strip() in trip_ids:
                out.row(row)
                stop_ids.add(row[i_stop].strip())
                kept += 1
    return stop_ids, kept, total


def _filter_stops(zin: zipfile.ZipFile, zout: zipfile.ZipFile, stop_ids: Container[str]) -> None:
    """Copy the stops called at, and their parent stations: a first pass
    finds the parents, so a platform never loses the station above it."""
    member = _member(zin, "stops.txt")
    if not member:
        return
    rows = _rows(zin, member)
    header = _header(rows, "stops.txt")
    i_stop = header.index("stop_id")
    parents = set()
    if "parent_station" in header:
        i_parent = header.index("parent_station")
        for row in rows:
            if row[i_stop].strip() in stop_ids and row[i_parent].strip():
                parents.add(row[i_parent].strip())
    _copy_filtered(
        zin, zout, member, "stops.txt",
        lambda row: (row[i_stop].strip() in stop_ids
                     or row[i_stop].strip() in parents))


def _filter_by_column(zin: zipfile.ZipFile, zout: zipfile.ZipFile, trip_ids: Container[str],
                      service_ids: Container[str]) -> None:
    """Copy the calendars of the kept services and the frequencies of the
    kept trips; a table without the column is left out."""
    for name, column, wanted in (
            ("calendar.txt", "service_id", service_ids),
            ("calendar_dates.txt", "service_id", service_ids),
            ("frequencies.txt", "trip_id", trip_ids)):
        if member := _member(zin, name):
            header = _header(_rows(zin, member), name)
            if column not in header:
                continue
            index = header.index(column)
            _copy_filtered(zin, zout, member, name,
                           lambda row, i=index, w=wanted: row[i].strip() in w)


def _copy_whole(zin: zipfile.ZipFile, zout: zipfile.ZipFile, drop_feed_info: bool) -> None:
    """Copy the tables that describe the network as they are."""
    for name in KEPT_WHOLE:
        if name == "feed_info.txt" and drop_feed_info:
            continue
        if member := _member(zin, name):
            zout.writestr(name, zin.read(member))


def filter_gtfs_zip(src: str, dst: str, route_ids: Iterable[str],
                    drop_feed_info: bool = False) -> dict[str, Any] | None:
    """Write to dst the part of the feed src that the chosen routes use.

    Returns {"trips": (kept, total), "stop_times": (kept, total),
    "seconds": elapsed} or None when src cannot be filtered - missing file,
    not a zip, or a table the format requires absent - in which case dst is
    removed and the caller falls back to importing the feed whole.
    """
    started = time.perf_counter()
    route_ids = set(route_ids)
    try:
        with zipfile.ZipFile(src) as zin,              zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            required = {name: _member(zin, name)
                        for name in ("routes.txt", "trips.txt", "stop_times.txt")}
            if missing := [n for n, m in required.items() if m is None]:
                _LOGGER.error("Cannot filter %s: no %s in the feed",
                              src, ", ".join(missing))
                raise ValueError("not a usable GTFS feed")
            trips, stop_times = required["trips.txt"], required["stop_times.txt"]
            if trips is None or stop_times is None:
                # said just above, written again for the type checker
                raise ValueError("not a usable GTFS feed")
            trip_ids, service_ids, trips_total = _filter_trips(
                zin, zout, trips, route_ids)
            stop_ids, st_kept, st_total = _filter_stop_times(
                zin, zout, stop_times, trip_ids)
            _filter_stops(zin, zout, stop_ids)
            _filter_by_column(zin, zout, trip_ids, service_ids)
            _copy_whole(zin, zout, drop_feed_info)
    except (OSError, LookupError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        # LookupError: a row shorter than its header, or a column the feed
        # names elsewhere; the feed is then kept whole rather than trimmed
        # on a guess
        _LOGGER.exception("Could not filter %s to %s routes: %s",
                      src, len(route_ids), ex)
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        return None

    stats = {"trips": (len(trip_ids), trips_total),
             "stop_times": (st_kept, st_total),
             "seconds": round(time.perf_counter() - started, 1)}
    _LOGGER.info(
        "Filtered %s to %s routes in %ss: %s of %s trips, %s of %s stop_times",
        os.path.basename(src), len(route_ids), stats["seconds"],
        len(trip_ids), trips_total, st_kept, st_total)
    return stats


def feed_info_unreadable(zip_path: str) -> bool:
    """Whether pygtfs would stop the whole import on feed_info.txt.

    feed_start_date and feed_end_date are optional in GTFS, and a feed may
    publish the columns with nothing in them (Krakow's trams). pygtfs reads
    every value of those columns as a YYYYMMDD date, and one that is not
    ends the import on strptime(None): the feed never loads. The import
    then leaves feed_info.txt out, as the clean_feed_info option always
    could. No query reads that table from the database; the Timetable
    sensor reads it from the zip, which the import never writes into, so
    the publisher and the version stay shown.
    """
    try:
        with zipfile.ZipFile(zip_path) as zin:
            for row in table_rows(zin, "feed_info.txt"):
                for column in ("feed_start_date", "feed_end_date"):
                    if column not in row:
                        continue
                    try:
                        time.strptime((row[column] or "").strip(), "%Y%m%d")
                    except ValueError:
                        return True
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read the feed_info of %s: %s", zip_path, ex)
    return False


def zip_only_future_dates(zip_path: str) -> bool:
    """Whether every service date of the feed lies in the future.

    The update service refuses such a feed: replacing today's timetable with
    one that starts next month would leave the sensors answering nothing
    until then. Reads calendar.txt start_date and calendar_dates.txt date,
    the same columns the legacy check read, without touching any database.

    Returns False when the dates cannot be read: an unreadable feed should
    fail the import loudly, not be silently kept out on a guess.
    """
    earliest: str | None = None
    try:
        with zipfile.ZipFile(zip_path) as zin:
            for name, column in (("calendar.txt", "start_date"),
                                 ("calendar_dates.txt", "date")):
                for row in table_rows(zin, name):
                    # a row short of the column says nothing about dates
                    value = (row.get(column) or "").strip()
                    if value and (earliest is None or value < earliest):
                        earliest = value
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read the dates of %s: %s", zip_path, ex)
        return False
    if earliest is None:
        return False
    return earliest > time.strftime("%Y%m%d")


def read_zip_routes(zip_path: str) -> list[dict[str, str | None]]:
    """The routes.txt rows of a feed, as dicts, or [] when unreadable.

    What the config flow needs to offer lines before any database exists:
    ids, names, types and agencies, straight from the record of the feed.
    """
    try:
        with zipfile.ZipFile(zip_path) as zin:
            member = _member(zin, "routes.txt")
            if member is None:
                _LOGGER.warning("No routes.txt in %s", zip_path)
                return []
            return [row for row in table_reader(zin.open(member)) if row.get("route_id")]
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read routes from %s: %s", zip_path, ex)
        return []


def read_zip_agencies(zip_path: str) -> list[dict[str, str | None]]:
    """The agency.txt rows of a feed, as dicts, or [] when unreadable."""
    try:
        with zipfile.ZipFile(zip_path) as zin:
            return [row for row in table_rows(zin, "agency.txt") if row.get("agency_name")]
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read agencies from %s: %s", zip_path, ex)
        return []
