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

import csv
import io
import logging
import os
import time

# the same zip implementation the rest of the integration reads feeds with
from . import zip_file as zipfile

_LOGGER = logging.getLogger(__name__)

# copied as they are: small, and the whole network must stay visible
KEPT_WHOLE = ("agency.txt", "routes.txt", "feed_info.txt")
# the zip is the only complete record of the feed, so the filter must never
# write into it: output always goes to a separate file


def _member(zin, name):
    """The archive member for a table, wherever the feed nested it."""
    return next((n for n in zin.namelist()
                 if n.rsplit("/", 1)[-1] == name), None)


def _rows(zin, member):
    """Stream a member as csv rows, header first, byte order mark eaten."""
    return csv.reader(io.TextIOWrapper(
        zin.open(member), encoding="utf-8-sig", newline=""))


class _Writer:
    """One filtered table on its way into the output zip, streamed.

    zipfile buffers nothing on an open("w") handle, so even the national
    stop_times never sits in memory: rows go straight through.
    """

    def __init__(self, zout, name, header):
        self._handle = zout.open(name, "w")
        self._wrapper = io.TextIOWrapper(
            self._handle, encoding="utf-8", newline="")
        self._csv = csv.writer(self._wrapper, lineterminator="\n")
        self._csv.writerow(header)

    def row(self, row):
        self._csv.writerow(row)

    def close(self):
        self._wrapper.flush()
        self._wrapper.detach()
        self._handle.close()


def _copy_filtered(zin, zout, member, name, keep):
    """Copy one table keeping the rows keep() accepts. Returns (kept, total)."""
    rows = _rows(zin, member)
    header = next(rows)
    out = _Writer(zout, name, header)
    kept = total = 0
    for row in rows:
        total += 1
        if keep(row):
            out.row(row)
            kept += 1
    out.close()
    return kept, total


def filter_gtfs_zip(src, dst, route_ids, drop_feed_info=False):
    """Write to dst the part of the feed src that the chosen routes use.

    Returns {"trips": (kept, total), "stop_times": (kept, total),
    "seconds": elapsed} or None when src cannot be filtered - missing file,
    not a zip, or a table the format requires absent - in which case dst is
    removed and the caller falls back to importing the feed whole.
    """
    started = time.perf_counter()
    route_ids = set(route_ids)
    try:
        with zipfile.ZipFile(src) as zin, \
             zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            required = {name: _member(zin, name)
                        for name in ("routes.txt", "trips.txt", "stop_times.txt")}
            if missing := [n for n, m in required.items() if m is None]:
                _LOGGER.error("Cannot filter %s: no %s in the feed",
                              src, ", ".join(missing))
                raise ValueError("not a usable GTFS feed")

            # trips first: it decides everything else that survives
            trip_ids, service_ids = set(), set()
            rows = _rows(zin, required["trips.txt"])
            header = next(rows)
            i_route = header.index("route_id")
            i_trip = header.index("trip_id")
            i_service = header.index("service_id")
            out = _Writer(zout, "trips.txt", header)
            trips_total = 0
            for row in rows:
                trips_total += 1
                if row[i_route] in route_ids:
                    out.row(row)
                    trip_ids.add(row[i_trip])
                    service_ids.add(row[i_service])
            out.close()

            # stop_times is the weight of the feed: one pass, collecting the
            # stops the kept trips call at
            stop_ids = set()
            rows = _rows(zin, required["stop_times.txt"])
            header = next(rows)
            i_trip = header.index("trip_id")
            i_stop = header.index("stop_id")
            out = _Writer(zout, "stop_times.txt", header)
            st_kept = st_total = 0
            for row in rows:
                st_total += 1
                if row[i_trip] in trip_ids:
                    out.row(row)
                    stop_ids.add(row[i_stop])
                    st_kept += 1
            out.close()

            # stops: a first pass finds the parent stations of the kept
            # stops, so a platform never loses the station above it
            if member := _member(zin, "stops.txt"):
                rows = _rows(zin, member)
                header = next(rows)
                i_stop = header.index("stop_id")
                i_parent = (header.index("parent_station")
                            if "parent_station" in header else None)
                parents = set()
                if i_parent is not None:
                    for row in _skip_header(_rows(zin, member)):
                        if row[i_stop] in stop_ids and row[i_parent]:
                            parents.add(row[i_parent])
                _copy_filtered(
                    zin, zout, member, "stops.txt",
                    lambda row: row[i_stop] in stop_ids or row[i_stop] in parents)

            for name, column, wanted in (
                    ("calendar.txt", "service_id", service_ids),
                    ("calendar_dates.txt", "service_id", service_ids),
                    ("frequencies.txt", "trip_id", trip_ids)):
                if member := _member(zin, name):
                    rows = _rows(zin, member)
                    header = next(rows)
                    if column not in header:
                        continue
                    index = header.index(column)
                    _copy_filtered(zin, zout, member, name,
                                   lambda row, i=index, w=wanted: row[i] in w)

            for name in KEPT_WHOLE:
                if name == "feed_info.txt" and drop_feed_info:
                    continue
                if member := _member(zin, name):
                    zout.writestr(name, zin.read(member))
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.error("Could not filter %s to %s routes: %s",
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


def _skip_header(rows):
    next(rows)
    return rows


def zip_only_future_dates(zip_path):
    """Whether every service date of the feed lies in the future.

    The update service refuses such a feed: replacing today's timetable with
    one that starts next month would leave the sensors answering nothing
    until then. Reads calendar.txt start_date and calendar_dates.txt date,
    the same columns the legacy check read, without touching any database.

    Returns False when the dates cannot be read: an unreadable feed should
    fail the import loudly, not be silently kept out on a guess.
    """
    earliest = None
    try:
        with zipfile.ZipFile(zip_path) as zin:
            for name, column in (("calendar.txt", "start_date"),
                                 ("calendar_dates.txt", "date")):
                member = _member(zin, name)
                if member is None:
                    continue
                rows = _rows(zin, member)
                header = next(rows)
                if column not in header:
                    continue
                index = header.index(column)
                for row in rows:
                    value = row[index].strip()
                    if value and (earliest is None or value < earliest):
                        earliest = value
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read the dates of %s: %s", zip_path, ex)
        return False
    if earliest is None:
        return False
    return earliest > time.strftime("%Y%m%d")


def read_zip_routes(zip_path):
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
            reader = csv.DictReader(io.TextIOWrapper(
                zin.open(member), encoding="utf-8-sig", newline=""))
            return [row for row in reader if row.get("route_id")]
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read routes from %s: %s", zip_path, ex)
        return []


def read_zip_agencies(zip_path):
    """The agency.txt rows of a feed, as dicts, or [] when unreadable."""
    try:
        with zipfile.ZipFile(zip_path) as zin:
            member = _member(zin, "agency.txt")
            if member is None:
                return []
            reader = csv.DictReader(io.TextIOWrapper(
                zin.open(member), encoding="utf-8-sig", newline=""))
            return [row for row in reader if row.get("agency_name")]
    except (OSError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read agencies from %s: %s", zip_path, ex)
        return []
