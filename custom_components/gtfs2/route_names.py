"""Which lines a feed declares, labelled for the config flow.

A route's label is its number then where it goes, built from routes in the
database (get_route_labels) or, before any database exists, from routes.txt
in the source zip (get_route_options_from_zip and the other *_zip readers).
The config flow lists a database's lines and agencies from here too
(get_route_list, get_route_count, get_agency_list). The label itself, and how lines that
read the same are told apart, are line_labels.py's, where a line goes
line_ends.py's.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from .feed.files import feed_zip
from .data.zip_filter import read_zip_agencies, read_zip_routes
from .line_ends import headsign_ends, route_ends
from .line_labels import _adds_to, _route_label, set_lines_apart

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


def get_routes_in_zip(gtfs_dir: str, filename: str) -> set[str]:
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
    path = feed_zip(gtfs_dir, filename)
    if not os.path.exists(path):
        _LOGGER.debug("No source zip beside datasource %s", filename)
        return set()
    # the one reader of routes.txt, which the flow uses too
    return {row["route_id"] for row in read_zip_routes(path) if row["route_id"]}


def get_agencies_in_zip(gtfs_dir: str, filename: str) -> list[str]:
    """The agencies of the source zip, shaped like get_agency_list's rows.

    What the agency step shows when no database exists yet: the feed is the
    only thing there is to read, and nothing may start importing before the
    lines are chosen.
    """
    rows = read_zip_agencies(feed_zip(gtfs_dir, filename))
    rows.sort(key=lambda row: str(row.get("agency_name")))
    return [f"{row.get('agency_id') or '0'}: {row['agency_name']}"
            for row in rows]


def get_route_options_from_zip(gtfs_dir: str, filename: str, agency: str | None = None) -> list[str]:
    """The route selector options, read from the source zip.

    Same "route_type##route_id##label" values get_route_list builds from the
    database, and every one carries the "##pruned" flag: no timetable is
    loaded yet, and that flag is exactly what routes the flow through the
    screen that imports one. agency narrows to one agency_id; "0" and None
    mean the whole feed.
    """
    zip_path = feed_zip(gtfs_dir, filename)
    rows = read_zip_routes(zip_path)
    if agency and agency != "0":
        rows = [row for row in rows if (row.get("agency_id") or "0") == agency]
    agencies = read_zip_agencies(zip_path)
    # agency_id may be left out when the feed has a single agency
    names = {str(a.get("agency_id") or ""): a["agency_name"] for a in agencies}
    only = agencies[0]["agency_name"] if len(agencies) == 1 else ""
    labels = _zip_labels(gtfs_dir, filename, rows)
    options = [f"{row.get('route_type') or '99'}##{row['route_id']}##{labels[row['route_id']]}##pruned"
               for row in rows if row["route_id"]]
    return set_lines_apart(
        options, [names.get(str(row.get("agency_id") or ""), only) for row in rows if row["route_id"]],
        None, gtfs_dir, filename, [row["route_id"] for row in rows if row["route_id"]])


def get_route_labels_from_zip(gtfs_dir: str, filename: str, route_ids: Collection[str]) -> dict[str, str]:
    """get_route_labels when there is no database to ask: names from the zip."""
    wanted = set(route_ids)
    known = _zip_labels(gtfs_dir, filename, [
        row for row in read_zip_routes(feed_zip(gtfs_dir, filename)) if row["route_id"] in wanted])
    return {r: known.get(r, r) for r in route_ids}


def _zip_labels(gtfs_dir: str, filename: str, rows: list[dict[str, str | None]]) -> dict[str, str]:
    """{route_id: label} of these routes.txt rows; a line whose long name
    says nothing is named by the ends its headsigns give."""
    ends = headsign_ends(gtfs_dir, filename, [
        row["route_id"] for row in rows
        if row["route_id"] and not _adds_to(row.get("route_short_name"), row.get("route_long_name"))])
    return {row["route_id"]: _route_label(row.get("route_short_name"),
                                          row.get("route_long_name"),
                                          ends.get(row["route_id"]), row["route_id"])
            for row in rows if row["route_id"]}


def routes_in_zip_for_agency(gtfs_dir: str, filename: str, route_ids: list[str],
                             agency: str | None = None) -> list[str]:
    """Cut a list of route_ids down to one agency's, as routes.txt records it.

    The also-import list offers what the feed declares minus what is loaded;
    on a national feed that is thousands of lines from dozens of operators,
    when the operator was already named on the agency screen. "0" and None
    mean no cut.
    """
    if not agency or agency == "0":
        return route_ids
    rows = read_zip_routes(feed_zip(gtfs_dir, filename))
    owned = {row["route_id"] for row in rows
             if (row.get("agency_id") or "0") == agency}
    return [r for r in route_ids if r in owned]


def get_route_labels(schedule: Schedule, route_ids: Sequence[str], gtfs_dir: str | None = None,
                     filename: str | None = None) -> dict[str, str]:
    """Readable names for route_ids, as {route_id: "41 : GARE - ESAT RODIN"}.

    routes survives a prune even when its trips do not, so these names are
    available for lines the datasource no longer carries any timetable for -
    which is exactly when they need to be offered back. gtfs_dir and filename
    let the source zip's trips name where a line goes (see route_ends).
    """
    if not route_ids:
        return {}
    out: dict[str, str] = {}
    sql = ("select route_id, route_short_name, route_long_name from routes "
           "where route_id in (select value from json_each(:routes))")
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(text(sql), {"routes": json.dumps(list(route_ids))}).fetchall()
    except SQLAlchemyError as ex:
        _LOGGER.warning("Could not read route names: %s", ex)
        return {r: r for r in route_ids}
    rows = list(rows)
    needs_ends = [r[0] for r in rows if not _adds_to(r[1], r[2])]
    endpoints = route_ends(schedule, gtfs_dir, filename, needs_ends)
    for route_id, short, long in rows:
        out[route_id] = _route_label(short, long, endpoints.get(route_id), route_id)
    # a route the feed declares but routes does not: keep it selectable
    for r in route_ids:
        out.setdefault(r, r)
    return {r: out[r] for r in route_ids}


def get_route_list(schedule: Schedule, data: Mapping[str, Any], with_trips_only: bool = False,
                   gtfs_dir: str | None = None) -> list[str]:
    """List the routes of a datasource.

    with_trips_only skips the routes that carry no trip. A datasource holds
    every route of the network, and routes stays complete even when the trips
    of a route are not (or no longer) loaded, so offering those would send the
    user to a stop list that comes back empty.

    Routes that a prune emptied are the exception: they are kept, because the
    user is entitled to add a line the prune removed, and hiding it would leave
    no way back. They come back flagged so the caller can offer to reload the
    datasource rather than walking into an empty stop list.

    Which lines those are is read from the source zip, not from the database:
    a prune empties whatever links routes to stops, so the database can only
    report the lines it still carries. gtfs_dir enables that lookup; without it
    the pruned lines are simply not offered, as before.
    """
    _LOGGER.debug("Getting routes with data: %s", data)
    trips_where = ""
    pruned: set[str] = set()
    if with_trips_only:
        with_trips = "and exists (select 1 from trips t where t.route_id = r.route_id)"
        if gtfs_dir:
            in_zip = get_routes_in_zip(gtfs_dir, data["file"])
            if in_zip:
                with schedule.engine.connect() as conn:
                    loaded = {r[0] for r in conn.execute(
                        text("select distinct route_id from trips"))}
                # declared by the feed but carrying no trip here: a prune took
                # them out, and the zip can put them back
                pruned = in_zip - loaded
        trips_where = with_trips
        if pruned:
            trips_where = ("and (exists (select 1 from trips t where t.route_id = r.route_id) "
                           "or r.route_id in (select value from json_each(:pruned)))")
    picked_where, params = _routes_where(data)
    sql_routes = f"""
    SELECT r.route_type, r.route_id, r.route_short_name, r.route_long_name, a.agency_name
    from routes r
    left join agency a on a.agency_id = r.agency_id
    where 1=1
    {picked_where}
    {trips_where}
    order by agency_name
    """  # noqa: S608
    routes_list: list[list[Any]] = []
    routes: list[str] = []
    with schedule.engine.connect() as conn:
        params["pruned"] = json.dumps(sorted(pruned))
        rows = conn.execute(text(sql_routes), params).fetchall()
    for row_cursor in rows:
        routes_list.append(list(row_cursor))
    # the lines whose long name says nothing get the two ends of the route
    # instead, read in one go rather than one query per line
    endpoints = route_ends(
        schedule, gtfs_dir, data["file"], [str(x[1]) for x in routes_list if not _adds_to(x[2], x[3])])
    for x in routes_list:
        # the value keeps route_type and route_id, which the flow parses back;
        # what follows the second ## is only ever shown to the user, so it
        # leads with the line number and where it goes, not the raw id
        route_type, route_id, short, long, agency = (str(v) for v in x)
        # route_long_name names the two ends of the line, in no particular
        # order: the direction is picked on the same screen, so no arrow here
        shown = _route_label(short, long, endpoints.get(route_id), route_id)
        if route_id in pruned:
            # a fourth field the flow reads to know the timetable is missing;
            # the label itself stays clean, the flow explains it in words
            val = f"{route_type}##{route_id}##{shown}##pruned"
        else:
            val = f"{route_type}##{route_id}##{shown}"
        routes.append(val)
    routes = set_lines_apart(routes, [x[4] for x in routes_list], schedule, gtfs_dir,
                             data["file"], [str(x[1]) for x in routes_list])
    _LOGGER.debug(f"routes: {routes}")
    return routes


def get_route_count(schedule: Schedule, data: Mapping[str, Any]) -> int:
    """How many routes get_route_list lists without with_trips_only.

    The route screen only shows that number. Building the whole list to
    count it read the ends of every line with no long name from
    stop_times: IDFM with every operator, 1837 lines, 25 to 47 s once many
    lines are imported, and the screen waited for it.
    """
    picked_where, params = _routes_where(data)
    sql = f"select count(*) from routes r where 1=1 {picked_where}"  # noqa: S608
    with schedule.engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _routes_where(data: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """(SQL, params) of the routes, aliased r, of the entry's agency and
    mode: "0" stands for every agency, "99" for every mode. The list and
    its count ask the same, so the count is the list's length."""
    # bound, not written into the query: an agency_id holding a quote
    # broke the list, and what the flow hands in is the user's pick
    agency_id = data["agency"].split(': ', 1)[0]
    where = "and r.agency_id = :agency_id" if agency_id != "0" else ""
    if data["route_type"] != "99":
        where += " and r.route_type = :route_type"
    return where, {"agency_id": agency_id, "route_type": data["route_type"]}


def get_agency_list(schedule: Schedule, data: Mapping[str, Any]) -> list[str]:
    _LOGGER.debug("Getting agencies with data: %s", data)
    sql_agencies = """
    SELECT a.agency_id, a.agency_name 
    from agency a
    order by a.agency_name
    """
    agencies_list: list[list[Any]] = []
    agencies: list[str] = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_agencies), {"q": "q"}).fetchall()
    for row_cursor in rows:
        agencies_list.append(list(row_cursor))
    for x in agencies_list:
        val = str(x[0]) + ": " + str(x[1])
        agencies.append(val)
    _LOGGER.debug(f"agencies: {agencies}")
    return agencies
