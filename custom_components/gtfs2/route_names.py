"""What the user reads for a line, and which lines a feed declares.

A route's label is its number then where it goes, built from routes in the
database (get_route_labels) or, before any database exists, from routes.txt
in the source zip (get_route_options_from_zip and the other *_zip readers).
When a feed leaves the long name empty the two ends of one trip stand in for
it (_route_endpoints), and labels sort the way a line number is read
(_natural). Called from the config flow and from gtfs_helper.get_route_list.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re

from sqlalchemy.sql import text

from . import zip_file as zipfile
from .gtfs_filter import read_zip_agencies, read_zip_routes

_LOGGER = logging.getLogger(__name__)


def get_routes_in_zip(gtfs_dir, filename):
    """The route_ids the source zip declares, read without unpacking it.

    The zip kept beside a datasource is the only complete record of the feed:
    a prune leaves routes and stops in place but empties everything that links
    them, so asking the database which lines exist can only ever return the
    lines it already knows. Reading the source answers for the whole network.

    routes.txt is small - tens to a few thousand lines - and only one column is
    needed, so the member is streamed and decoded on the fly rather than
    extracted to disk.

    Returns an empty set when the zip is gone or unreadable, which the caller
    treats as "cannot tell", not as "no routes".
    """
    path = os.path.join(gtfs_dir, filename + ".zip")
    if not os.path.exists(path):
        _LOGGER.debug("No source zip beside datasource %s", filename)
        return set()
    try:
        with zipfile.ZipFile(path) as zin:
            member = next((n for n in zin.namelist()
                           if n.rsplit("/", 1)[-1] == "routes.txt"), None)
            if member is None:
                _LOGGER.warning("No routes.txt in %s", path)
                return set()
            with zin.open(member) as fh:
                # utf-8-sig: GTFS files routinely carry a byte order mark, and
                # it would otherwise end up glued to the first column name
                reader = csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig"))
                if not reader.fieldnames or "route_id" not in reader.fieldnames:
                    _LOGGER.warning("No route_id column in %s", member)
                    return set()
                return {row["route_id"] for row in reader if row.get("route_id")}
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read routes from %s: %s", path, ex)
        return set()


def get_agencies_in_zip(gtfs_dir, filename):
    """The agencies of the source zip, shaped like get_agency_list's rows.

    What the agency step shows when no database exists yet: the feed is the
    only thing there is to read, and nothing may start importing before the
    lines are chosen.
    """
    rows = read_zip_agencies(os.path.join(gtfs_dir, filename + ".zip"))
    rows.sort(key=lambda row: str(row.get("agency_name")))
    return [f"{row.get('agency_id') or '0'}: {row['agency_name']}"
            for row in rows]


def get_route_options_from_zip(gtfs_dir, filename, agency=None):
    """The route selector options, read from the source zip.

    Same "route_type##route_id##label" values get_route_list builds from the
    database, and every one carries the "##pruned" flag: no timetable is
    loaded yet, and that flag is exactly what routes the flow through the
    screen that imports one. agency narrows to one agency_id; "0" and None
    mean the whole feed.
    """
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    if agency and agency != "0":
        rows = [row for row in rows if (row.get("agency_id") or "0") == agency]
    options = []
    for row in rows:
        label = _route_label(row.get("route_short_name"),
                             row.get("route_long_name"),
                             route_id=row["route_id"])
        options.append(
            f"{row.get('route_type') or '99'}##{row['route_id']}##{label}##pruned")
    return sorted(options, key=lambda value: _natural(value.split("##")[2]))


def get_route_labels_from_zip(gtfs_dir, filename, route_ids):
    """get_route_labels when there is no database to ask: names from the zip."""
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    known = {row["route_id"]: _route_label(row.get("route_short_name"),
                                           row.get("route_long_name"),
                                           route_id=row["route_id"])
             for row in rows}
    return {r: known.get(r, r) for r in route_ids}


def routes_in_zip_for_agency(gtfs_dir, filename, route_ids, agency=None):
    """Cut a list of route_ids down to one agency's, as routes.txt records it.

    The also-import list offers what the feed declares minus what is loaded;
    on a national feed that is thousands of lines from dozens of operators,
    when the operator was already named on the agency screen. "0" and None
    mean no cut.
    """
    if not agency or agency == "0":
        return route_ids
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    owned = {row["route_id"] for row in rows
             if (row.get("agency_id") or "0") == agency}
    return [r for r in route_ids if r in owned]


def _says_something(part):
    """Whether a name part carries anything a reader can use.

    A line number is often nothing but digits, so digits count. What does not
    count is punctuation on its own: SNCF publishes 54 lines whose long name is
    the string " -", the two ends of a route it did not fill in, and showing
    that to the user is worse than showing nothing.
    """
    return any(character.isalnum() for character in str(part or ""))


def _route_endpoints(schedule, route_ids):
    """Where each of these lines starts and ends, as "A > B".

    Read from one trip per line, which is what the direction step already does
    on the screen after this one. It is only asked for the lines whose name is
    unusable, so the query stays small even on a national feed.
    """
    if not route_ids:
        return {}
    route_ids = sorted(route_ids)
    placeholders = ", ".join(f":e{i}" for i in range(len(route_ids)))
    sql = f"""
    with picked as (
        select route_id, min(trip_id) as trip_id
        from trips where route_id in ({placeholders}) group by route_id
    )
    select p.route_id, st.stop_sequence, s.stop_name
    from picked p
    inner join stop_times st on st.trip_id = p.trip_id
    inner join stops s on s.stop_id = st.stop_id
    """  # noqa: S608
    ends = {}
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {f"e{i}": r for i, r in enumerate(route_ids)}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # without this the label falls back to the route_id, which is what it
        # did before: ugly, but never empty
        _LOGGER.warning("Could not read the ends of %s routes: %s", len(route_ids), ex)
        return {}
    for route_id, sequence, name in rows:
        if not name:
            continue
        first, last = ends.get(route_id, (None, None))
        if first is None or sequence < first[0]:
            first = (sequence, name)
        if last is None or sequence > last[0]:
            last = (sequence, name)
        ends[route_id] = (first, last)
    return {route_id: f"{first[1]} > {last[1]}"
            for route_id, (first, last) in ends.items()
            if first and last and first[1] != last[1]}


def _route_label(short, long_name, endpoints=None, route_id=None):
    """What the user reads for one line: its number, then where it goes.

    The two parts are kept only if they say something, so a line named
    "INCONNU" against a long name of " -" no longer reads "INCONNU :  -". When
    the long name is the one missing, the two ends of the route take its place,
    which is the thing the user was looking for in the first place.
    """
    parts = [str(p) for p in (short, long_name)
             if p and str(p) != "None" and _says_something(p)]
    if len(parts) < 2 and endpoints:
        parts = parts[:1] + [endpoints]
    if parts:
        return " : ".join(parts)
    return str(route_id or "")


def _natural(label):
    """Sort key that reads 2 before 10, the way a line number is read."""
    out = []
    for chunk in re.split(r"(\d+)", str(label)):
        out.append((1, int(chunk)) if chunk.isdigit() else (0, chunk.lower()))
    return out


def get_route_labels(schedule, route_ids):
    """Readable names for route_ids, as {route_id: "41 : GARE - ESAT RODIN"}.

    routes survives a prune even when its trips do not, so these names are
    available for lines the datasource no longer carries any timetable for -
    which is exactly when they need to be offered back.
    """
    if not route_ids:
        return {}
    out = {}
    placeholders = ", ".join(f":r{i}" for i in range(len(route_ids)))
    sql = ("select route_id, route_short_name, route_long_name from routes "
           f"where route_id in ({placeholders})")  # noqa: S608
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {f"r{i}": r for i, r in enumerate(route_ids)}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read route names: %s", ex)
        return {r: r for r in route_ids}
    rows = list(rows)
    needs_ends = [r[0] for r in rows if not _says_something(r[2])]
    endpoints = _route_endpoints(schedule, needs_ends)
    for route_id, short, long in rows:
        out[route_id] = _route_label(short, long, endpoints.get(route_id), route_id)
    # a route the feed declares but routes does not: keep it selectable
    for r in route_ids:
        out.setdefault(r, r)
    return {r: out[r] for r in route_ids}
