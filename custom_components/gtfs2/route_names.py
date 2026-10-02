"""What the user reads for a line, and which lines a feed declares.

A route's label is its number then where it goes, built from routes in the
database (get_route_labels) or, before any database exists, from routes.txt
in the source zip (get_route_options_from_zip and the other *_zip readers).
When a feed leaves the long name empty the destinations its trips show stand
in for it (headsign_ends), else the two ends of its longest trip
(_route_endpoints); labels sort the way a line number is read (_natural).
The config flow lists a database's lines and agencies from here too
(get_route_list, get_route_count, get_agency_list).
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
import json
import logging
import os
from collections import Counter, defaultdict
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from .gtfs_db import feed_zip
from .gtfs_filter import read_zip_agencies, read_zip_routes
from .line_ends import _Spans, headsign_ends, look_alike_ends, route_ends, route_spans
from .line_labels import _adds_to, _natural, _route_label, _says_something, line_mode

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


def set_lines_apart(options: list[str], agencies: list[str | None], schedule: Schedule | None,
                    gtfs_dir: str | None, filename: str | None, route_ids: list[str]) -> list[str]:
    """The line options, told apart where they read the same, in the order a
    line number is read; agencies holds each option's agency name, schedule
    is None when there is no database yet.

    Lines of two operators under one number get the agency's name; lines one
    operator publishes under one name get their two ends; what still reads
    the same is the same line published once per period of validity: the
    dead ones go, the rest say which period. The periods are asked only when
    some line is still ambiguous, so a feed that names its lines properly
    never pays for the dates of any of them. Sorted on what the user reads:
    a cast on route_id is 0 for every id that is not a number, which is most
    of them outside a small network.
    """
    options = _set_apart(options, agencies)
    options = _set_apart_by_ends(
        options, look_alike_ends(schedule, gtfs_dir, filename, _look_alikes(options)))
    if _look_alikes(options):
        spans = route_spans(gtfs_dir, filename, route_ids)
        options = _leave_out_expired(options, spans)
        options = _set_apart_by_span(options, spans)
    return sorted(options, key=lambda value: _natural(value.split("##")[2]))


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


def _set_apart(options: list[str], agencies: list[str | None]) -> list[str]:
    """Name the agency where two lines of the list read the same.

    options are "route_type##route_id##label[##pruned]" values, agencies the
    agency name of each, in the same order. A feed covering a region lists
    lines of several operators under one number: the metro 1 of the RATP and
    the bus 1 of Terres d'Envol at IDFM, the tram 4 of GVB and of HTM in the
    Netherlands. Those get " · <agency>" after their label, and only those:
    a label nobody else wears keeps its words. Nor is it added where the
    agencies of the look-alikes are the same too (SNCF's "INCONNU" lines),
    since it would lengthen every one of them without telling them apart.
    """
    labels = [option.split("##")[2] for option in options]
    groups: dict[str, set[str]] = {}
    for label, agency in zip(labels, agencies):
        groups.setdefault(label.casefold(), set()).add(str(agency or "").strip().casefold())
    out = []
    for option, label, agency in zip(options, labels, agencies):
        if len(groups[label.casefold()]) > 1 and _says_something(agency):
            parts = option.split("##")
            parts[2] = f"{label} · {str(agency).strip()}"
            option = "##".join(parts)
        out.append(option)
    return out


def _look_alikes(options: list[str]) -> list[str]:
    """The route_ids of the lines whose label another line of the same mode
    wears too, after _set_apart: one operator publishing one name for
    several routes (IDFM's three "TER : TER Centre - Val de Loire", to
    Chartres, to Montargis and to Châteaudun). Look-alikes of different
    modes are left out, the flow already names their mode (with_modes):
    Zou's P18 train and P18 coach."""
    labels = Counter(_shown_as(option) for option in options)
    return [option.split("##")[1] for option in options if labels[_shown_as(option)] > 1]


def _shown_as(option: str) -> tuple[str, str | None]:
    """What tells two options apart before the flow adds the mode."""
    return (option.split("##")[2].casefold(), line_mode(option.split("##")[0]))


def _leave_out_expired(options: list[str], spans: _Spans, today: str | None = None) -> list[str]:
    """Drop a line whose days are over when a live line wears its label.

    Publishers who cut their feed by period of validity list one line per
    period: the Dutch national feed had 46 lines twice, once for the day
    the old timetable ended and once for the months that follow, reading
    exactly the same. Picking the wrong one gives a sensor that will never
    have a departure.

    Only the ones a live twin stands for are dropped. A whole feed can be
    out of date - two of the eighteen sources surveyed were, one by
    seventeen months - and there the list has to keep showing the lines it
    has, expired or not, rather than going empty.
    """
    today = today or date.today().strftime("%Y%m%d")

    def over(option: str) -> bool:
        span = spans.get(option.split("##")[1])
        return span is not None and span[1] < today

    alive: defaultdict[tuple[str, str | None], bool] = defaultdict(bool)
    for option in options:
        alive[_shown_as(option)] |= not over(option)
    return [option for option in options
            if not (over(option) and alive[_shown_as(option)])]


def _read_date(stamp: str) -> str | None:
    """A GTFS date as the user reads it, or None when it is not one."""
    try:
        return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])).isoformat()
    except (TypeError, ValueError):
        return None


def _set_apart_by_span(options: list[str], spans: _Spans) -> list[str]:
    """Give the look-alikes that remain the days they run.

    What is left after the agency, the ends and the expired ones: the same
    line published once per period of validity, which is how Brisbane lists
    eighteen entries for its airport line, some of them lasting a single
    day. Their dates are the only thing that differs, so their dates are
    what the list shows.

    Only where they differ: look-alikes running the very same days are told
    apart by nothing here, and the dates would lengthen every one of them
    for no reader's benefit - the rail replacement runs Leipzig publishes
    under one name, all of them dated the same twelvemonth.
    """
    periods: defaultdict[tuple[str, str | None], set[tuple[str, str] | None]] = defaultdict(set)
    for option in options:
        periods[_shown_as(option)].add(spans.get(option.split("##")[1]))
    out = []
    for option in options:
        parts = option.split("##")
        span = spans.get(parts[1])
        if len(periods[_shown_as(option)]) > 1 and span:
            first, last = _read_date(span[0]), _read_date(span[1])
            if first and last:
                parts[2] = f"{parts[2]} · {first}" + (f" → {last}" if last != first else "")
                option = "##".join(parts)
        out.append(option)
    return out


def _set_apart_by_ends(options: list[str], ends: Mapping[str, str]) -> list[str]:
    """Give the look-alikes their two ends: " · Chartres ↔ Gare Montparnasse".

    ends is {route_id: ends} as route_ends or headsign_ends read them. A
    label that already shows its ends (a line without a long name) is left
    as it is, the words would only be said twice.
    """
    out = []
    for option in options:
        parts = option.split("##")
        found = ends.get(parts[1])
        if found and found.casefold() not in parts[2].casefold():
            parts[2] = f"{parts[2]} · {found}"
            option = "##".join(parts)
        out.append(option)
    return out


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
