"""What the user reads for a line, and which lines a feed declares.

A route's label is its number then where it goes, built from routes in the
database (get_route_labels) or, before any database exists, from routes.txt
in the source zip (get_route_options_from_zip and the other *_zip readers).
When a feed leaves the long name empty the destinations its trips show stand
in for it (headsign_ends), else the two ends of its longest trip
(_route_endpoints); labels sort the way a line number is read (_natural).
Called from the config flow and from gtfs_helper.get_route_list.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import date

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
    # the one reader of routes.txt, which the flow uses too
    return {row["route_id"] for row in read_zip_routes(path)}


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
    zip_path = os.path.join(gtfs_dir, filename + ".zip")
    rows = read_zip_routes(zip_path)
    if agency and agency != "0":
        rows = [row for row in rows if (row.get("agency_id") or "0") == agency]
    agencies = read_zip_agencies(zip_path)
    # agency_id may be left out when the feed has a single agency
    names = {str(a.get("agency_id") or ""): a["agency_name"] for a in agencies}
    only = agencies[0]["agency_name"] if len(agencies) == 1 else ""
    ends = headsign_ends(gtfs_dir, filename, [
        row["route_id"] for row in rows
        if not _adds_to(row.get("route_short_name"), row.get("route_long_name"))])
    options = []
    for row in rows:
        label = _route_label(row.get("route_short_name"),
                             row.get("route_long_name"),
                             ends.get(row["route_id"]), row["route_id"])
        options.append(
            f"{row.get('route_type') or '99'}##{row['route_id']}##{label}##pruned")
    options = _set_apart(options, [names.get(str(row.get("agency_id") or ""), only) for row in rows])
    options = _set_apart_by_ends(options, look_alike_ends(None, gtfs_dir, filename, _look_alikes(options)))
    # and what still reads the same is the same line published once per
    # period of validity: the dead ones go, the rest say which period.
    # Asked only when some line is still ambiguous, so a feed that names
    # its lines properly never pays for the dates of any of them
    if _look_alikes(options):
        spans = route_spans(gtfs_dir, filename, [row["route_id"] for row in rows])
        options = _leave_out_expired(options, spans)
        options = _set_apart_by_span(options, spans)
    return sorted(options, key=lambda value: _natural(value.split("##")[2]))


def get_route_labels_from_zip(gtfs_dir, filename, route_ids):
    """get_route_labels when there is no database to ask: names from the zip."""
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    wanted = set(route_ids)
    ends = headsign_ends(gtfs_dir, filename, [
        row["route_id"] for row in rows if row["route_id"] in wanted
        and not _adds_to(row.get("route_short_name"), row.get("route_long_name"))])
    known = {row["route_id"]: _route_label(row.get("route_short_name"),
                                           row.get("route_long_name"),
                                           ends.get(row["route_id"]), row["route_id"])
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


def _adds_to(short, long_name):
    """Whether the long name tells the reader more than the number does.

    IDFM writes the number again as the long name on 1837 of its 2024 lines
    ("1" / "1", "4244" / "4244"), which read "1 : 1" in the list. A long name
    that only repeats the number is treated as no long name at all, so the
    two ends of the route take its place as they do for an empty one.
    """
    return (_says_something(long_name)
            and str(long_name).strip().casefold() != str(short or "").strip().casefold())


def _set_apart(options, agencies):
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
    groups = {}
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


def _look_alikes(options):
    """The route_ids of the lines whose label another line of the same mode
    wears too, after _set_apart: one operator publishing one name for
    several routes (IDFM's three "TER : TER Centre - Val de Loire", to
    Chartres, to Montargis and to Châteaudun). Look-alikes of different
    modes are left out, the flow already names their mode (with_modes):
    Zou's P18 train and P18 coach."""
    labels = Counter(_shown_as(option) for option in options)
    return [option.split("##")[1] for option in options if labels[_shown_as(option)] > 1]


def _shown_as(option):
    """What tells two options apart before the flow adds the mode."""
    return (option.split("##")[2].casefold(), line_mode(option.split("##")[0]))


def _leave_out_expired(options, spans, today=None):
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

    def over(option):
        span = spans.get(option.split("##")[1])
        return bool(span) and span[1] < today

    alive = defaultdict(bool)
    for option in options:
        alive[_shown_as(option)] |= not over(option)
    return [option for option in options
            if not (over(option) and alive[_shown_as(option)])]


def _read_date(stamp):
    """A GTFS date as the user reads it, or None when it is not one."""
    try:
        return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])).isoformat()
    except (TypeError, ValueError):
        return None


def _set_apart_by_span(options, spans):
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
    periods = defaultdict(set)
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


def _set_apart_by_ends(options, ends):
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


# the ends read from trips.txt, per zip edition: {(path, size, mtime): ends}
_HEADSIGN_ENDS = {}


def _names_a_place(headsign, places=frozenset()):
    """Whether a trip_headsign reads as a destination rather than a code.

    SNCF writes the train number there ("44930"), IDFM the RER mission code
    ("UZAR", "NATO"): neither tells a rider where the line goes. A short
    word in capitals with no space is taken for such a code, unless the
    feed has a place of that name: places holds its stop names and their
    first words, casefolded, so NICE or PAU, a town written in capitals,
    is still the destination it is.
    """
    headsign = str(headsign or "").strip()
    if not any(character.isalpha() for character in headsign):
        return False
    if headsign.isupper() and " " not in headsign and len(headsign) <= 5:
        return headsign.casefold() in places
    return True


def _read_place_words(zin):
    """The stop names of an open feed and their first words, casefolded."""
    member = next((n for n in zin.namelist()
                   if n.rsplit("/", 1)[-1] == "stops.txt"), None)
    if member is None:
        return frozenset()
    words = set()
    with zin.open(member) as fh:
        for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig", newline="")):
            name = (row.get("stop_name") or "").strip().casefold()
            if name:
                words.add(name)
                words.add(re.split(r"[\s\-/]", name, 1)[0])
    return frozenset(words)


def _read_service_spans(zin):
    """{service_id: (first date, last date)} of an open feed.

    Both calendars count: a feed may give a service a window in
    calendar.txt, a list of dates in calendar_dates.txt, or a window that
    exceptions extend. Only the dates a service runs on are read, never
    the ones it is removed from, so a window never grows on a cancellation.
    """
    spans = {}
    def seen(service, first, last):
        if not service or not first or not last:
            return
        was = spans.get(service)
        spans[service] = ((min(was[0], first), max(was[1], last)) if was
                          else (first, last))
    member = next((n for n in zin.namelist()
                   if n.rsplit("/", 1)[-1] == "calendar.txt"), None)
    if member is not None:
        with zin.open(member) as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig", newline="")):
                seen(row.get("service_id"), row.get("start_date"), row.get("end_date"))
    member = next((n for n in zin.namelist()
                   if n.rsplit("/", 1)[-1] == "calendar_dates.txt"), None)
    if member is not None:
        with zin.open(member) as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig", newline="")):
                if (row.get("exception_type") or "1") == "1":
                    seen(row.get("service_id"), row.get("date"), row.get("date"))
    return spans


def _read_trips(zip_path):
    """What trips.txt says about every line, in one pass over it.

    Returns ({route_id: "A ↔ B"}, {route_id: (first date, last date)}): the
    destination each direction shows most often, and the days the line
    runs. A direction whose trips mostly show a code, or nothing, is left
    out of the first; a line with none left is not in it. Both answers come
    from the same reading because that file is the expensive one: 196 MB on
    the British national feed, and reading it twice showed."""
    shown = defaultdict(lambda: defaultdict(Counter))
    serves = defaultdict(set)
    place_words = frozenset()
    spans = {}
    try:
        with zipfile.ZipFile(zip_path) as zin:
            member = next((n for n in zin.namelist()
                           if n.rsplit("/", 1)[-1] == "trips.txt"), None)
            if member is None:
                return {}, {}
            calendar = _read_service_spans(zin)
            with zin.open(member) as fh:
                reader = csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig", newline=""))
                names = reader.fieldnames or []
                headsigns = "trip_headsign" in names
                for row in reader:
                    if headsigns:
                        shown[row.get("route_id")][row.get("direction_id") or ""][
                            (row.get("trip_headsign") or "").strip()] += 1
                    service = calendar.get(row.get("service_id"))
                    if service:
                        serves[str(row.get("route_id"))].add(service)
            if headsigns:
                place_words = _read_place_words(zin)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read the trips of %s: %s", zip_path, ex)
        return {}, {}
    for route_id, windows in serves.items():
        spans[route_id] = (min(w[0] for w in windows), max(w[1] for w in windows))
    ends = {}
    for route_id, directions in shown.items():
        places = []
        for direction in sorted(directions):
            counts = directions[direction]
            # a feed without direction_id puts both ways under one key
            wanted = 2 if direction == "" else 1
            named = [(h, n) for h, n in counts.most_common() if _names_a_place(h, place_words)]
            if sum(n for _, n in named) * 2 < sum(counts.values()):
                continue
            places += [h for h, _ in named[:wanted]]
        places = list(dict.fromkeys(places))[:2]
        if places:
            ends[str(route_id)] = " ↔ ".join(places)
    return ends, spans


def headsign_ends(gtfs_dir, filename, route_ids):
    """Where these lines go, as the trips of the source zip say it:
    {route_id: "Château de Vincennes ↔ La Défense"}, for the ones it can.

    Works before any database exists and for a line whose timetable was
    never imported, which the stops cannot do. trips.txt is read once per
    edition of the zip and kept in memory (IDFM: 47 MB, 6 s), and only when
    some line needs it.
    """
    route_ids = {str(r) for r in route_ids}
    if not route_ids:
        return {}
    ends, _ = _from_trips(gtfs_dir, filename)
    return {r: ends[r] for r in route_ids if r in ends}


def _from_trips(gtfs_dir, filename):
    """The pair _read_trips builds, read once per edition of the zip."""
    zip_path = os.path.join(gtfs_dir, filename + ".zip") if gtfs_dir else None
    if not zip_path or not os.path.exists(zip_path):
        return {}, {}
    stat = os.stat(zip_path)
    key = (zip_path, stat.st_size, stat.st_mtime_ns)
    if key not in _HEADSIGN_ENDS:
        for old in [k for k in _HEADSIGN_ENDS if k[0] == zip_path]:
            del _HEADSIGN_ENDS[old]
        _HEADSIGN_ENDS[key] = _read_trips(zip_path)
    return _HEADSIGN_ENDS[key]


def route_spans(gtfs_dir, filename, route_ids):
    """{route_id: (first date, last date)}: the days these lines run.

    Read from the same pass over trips.txt as the destinations, so a list
    that already named its lines pays nothing more for their dates.
    """
    route_ids = {str(r) for r in route_ids}
    if not route_ids:
        return {}
    _, spans = _from_trips(gtfs_dir, filename)
    return {r: spans[r] for r in route_ids if r in spans}


def _read_stop_ends(zip_path, route_ids):
    """{route_id: "A > B"} for these lines: the first and last stop of the
    trip that calls at the most stops, read from the zip's stop_times.txt,
    as _route_endpoints reads it from the database."""
    trips, calls, first, last = {}, Counter(), {}, {}
    try:
        with zipfile.ZipFile(zip_path) as zin:
            files = {n.rsplit("/", 1)[-1]: n for n in zin.namelist()}

            def rows(name):
                return csv.DictReader(io.TextIOWrapper(zin.open(files[name]), "utf-8-sig", newline=""))

            for row in rows("trips.txt"):
                if row.get("route_id") in route_ids:
                    trips[row["trip_id"]] = row["route_id"]
            for row in rows("stop_times.txt"):
                trip = row.get("trip_id")
                if trip not in trips:
                    continue
                sequence = int(row["stop_sequence"])
                calls[trip] += 1
                if trip not in first or sequence < first[trip][0]:
                    first[trip] = (sequence, row["stop_id"])
                if trip not in last or sequence > last[trip][0]:
                    last[trip] = (sequence, row["stop_id"])
            names = {row["stop_id"]: row.get("stop_name") for row in rows("stops.txt")}
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read the stops of %s: %s", zip_path, ex)
        return {}
    most = {}
    for trip, route_id in trips.items():
        most[route_id] = max(most.get(route_id, 0), calls[trip])
    ends = {}
    for trip, route_id in trips.items():
        if not calls[trip] or calls[trip] < most[route_id]:
            continue
        a, b = names.get(first[trip][1]), names.get(last[trip][1])
        # a loop ends where it starts, and a stop with no name says nothing:
        # "A > A" and "None > B" told two look-alikes apart by nothing
        if not a or not b or a == b:
            continue
        # among the longest, the one whose ends come first by name, in the
        # order it rides them, as _route_endpoints chooses
        if route_id not in ends or (a, b) < ends[route_id]:
            ends[route_id] = (a, b)
    return {route_id: f"{a} > {b}" for route_id, (a, b) in ends.items()}


# the ends read from stop_times.txt, per zip edition: {(path, size, mtime): ends}
_STOP_ENDS = {}

# the largest stop_times.txt worth walking for a handful of look-alikes.
# What the read buys does not grow with the feed, what it costs does: Renfe
# levels 648 look-alikes in 0.5 s (21 MB), SNCF 58 in 1.5 s (54 MB), the
# German national feed 24 in 95 s (2.2 GB), the British one 11 in 188 s
# (5.1 GB). Measured on 48 feeds, the rate falls off between 76 MB and
# 401 MB and never recovers, so the cap sits between the two.
_STOP_TIMES_CAP = 150 * 1024 * 1024


def _stop_times_size(zip_path):
    """How big stop_times.txt is unpacked, read from the zip's directory.

    The central directory carries every member's size, so this answers in
    milliseconds without decompressing a byte. Returns None when the member
    or the zip cannot be read, which leaves the decision to the caller.
    """
    try:
        with zipfile.ZipFile(zip_path) as zin:
            for info in zin.infolist():
                if info.filename.rsplit("/", 1)[-1] == "stop_times.txt":
                    return info.file_size
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Could not size the stops of %s: %s", zip_path, ex)
    return None


def look_alike_ends(schedule, gtfs_dir, filename, route_ids):
    """The ends of look-alike lines (see _look_alikes), wherever they are
    written: the trips' destinations, the imported trips (when schedule is
    given), and for what is left the zip's stop_times.txt.

    That last read is the costly one, so it is kept for the look-alikes the
    rest leaves without ends: SNCF's 54 "INCONNU" lines, whose trips show a
    train number and which a filtered import does not carry (54 MB, 1.2 s),
    Renfe's lines named after the product alone (22 MB, 0.4 s). IDFM, NL,
    TAO never reach it. Kept per edition of the zip, like the destinations.

    Past _STOP_TIMES_CAP it is not read at all: nobody waits minutes on the
    route screen for a dozen better names. Those lines keep the label they
    have, which is what they wore before this read existed.
    """
    route_ids = [str(r) for r in route_ids]
    if schedule is not None:
        ends = route_ends(schedule, gtfs_dir, filename, route_ids)
    else:
        ends = headsign_ends(gtfs_dir, filename, route_ids)
    missing = {r for r in route_ids if r not in ends}
    zip_path = os.path.join(gtfs_dir, filename + ".zip") if gtfs_dir else None
    if not missing or not zip_path or not os.path.exists(zip_path):
        return ends
    size = _stop_times_size(zip_path)
    if size is not None and size > _STOP_TIMES_CAP:
        _LOGGER.debug(
            "Not reading the %s MB of stops of %s to name %s look-alike routes",
            size // (1024 * 1024), filename, len(missing))
        return ends
    stat = os.stat(zip_path)
    key = (zip_path, stat.st_size, stat.st_mtime_ns)
    if key not in _STOP_ENDS:
        for old in [k for k in _STOP_ENDS if k[0] == zip_path]:
            del _STOP_ENDS[old]
        _STOP_ENDS[key] = {}
    known = _STOP_ENDS[key]
    unread = missing - set(known)
    if unread:
        found = _read_stop_ends(zip_path, unread)
        # a line with no trip in the zip is remembered too, not read again
        known.update({r: found.get(r) for r in unread})
    ends.update({r: known[r] for r in missing if known[r]})
    return ends


def route_ends(schedule, gtfs_dir, filename, route_ids):
    """The ends of these lines: the trips' destinations where the zip names
    them, the first and last stop of the longest imported trip otherwise."""
    ends = headsign_ends(gtfs_dir, filename, route_ids)
    ends.update(_route_endpoints(schedule, [r for r in route_ids if str(r) not in ends]))
    return ends


def _route_endpoints(schedule, route_ids):
    """Where each of these lines starts and ends, as "A > B".

    Read from the trip of the line that calls at the most stops: the first
    trip by id is often a short turn (IDFM metro 4: Montparnasse, not
    Bagneux). It is only asked for the lines whose name is unusable, so the
    query stays small even on a national feed.
    """
    if not route_ids:
        return {}
    route_ids = sorted(route_ids)
    placeholders = ", ".join(f":e{i}" for i in range(len(route_ids)))
    # the longest trips of each line, direction 0 first: every one of them,
    # for the choice between them is made on their stops below. Left to
    # max(n), SQLite picked whichever tied trip it met first, and the label
    # turned round, A > B, then B > A, after a rebuild
    sql = f"""
    with calls as (
        select t.route_id, t.trip_id, coalesce(t.direction_id, 0) as d, count(*) as n
        from trips t inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id in ({placeholders}) group by t.trip_id
    ), picked as (
        select route_id, trip_id from (
            select route_id, trip_id,
                   rank() over (partition by route_id order by n desc, d) as r
            from calls
        ) where r = 1
    )
    select p.route_id, p.trip_id, st.stop_sequence, s.stop_name
    from picked p
    inner join stop_times st on st.trip_id = p.trip_id
    inner join stops s on s.stop_id = st.stop_id
    """  # noqa: S608
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {f"e{i}": r for i, r in enumerate(route_ids)}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # without this the label falls back to the route_id, which is what it
        # did before: ugly, but never empty
        _LOGGER.warning("Could not read the ends of %s routes: %s", len(route_ids), ex)
        return {}
    trips = {}
    for route_id, trip_id, sequence, name in rows:
        if not name:
            continue
        first, last = trips.get((route_id, trip_id), (None, None))
        if first is None or sequence < first[0]:
            first = (sequence, name)
        if last is None or sequence > last[0]:
            last = (sequence, name)
        trips[(route_id, trip_id)] = (first, last)
    # among the tied trips, the one whose ends come first by name: chosen by
    # the timetable, not by the trip ids, which some feeds change at every
    # export (the SNCF dates them). The ends stay in the order the trip
    # rides them: a feed publishing each way as a line of its own (Renfe's
    # Alvia) tells the two apart by that order alone
    ends = {}
    for (route_id, _trip), (first, last) in trips.items():
        if not first or not last or first[1] == last[1]:
            continue
        pair = (first[1], last[1])
        if route_id not in ends or pair < ends[route_id]:
            ends[route_id] = pair
    return {route_id: f"{a} > {b}" for route_id, (a, b) in ends.items()}


def _route_label(short, long_name, endpoints=None, route_id=None):
    """What the user reads for one line: its number, then where it goes.

    The two parts are kept only if they say something, so a line named
    "INCONNU" against a long name of " -" no longer reads "INCONNU :  -". When
    the long name is the one missing, the two ends of the route take its place,
    which is the thing the user was looking for in the first place.
    """
    parts = [str(p) for p in (short, long_name)
             if p and str(p) != "None" and _says_something(p)]
    if len(parts) == 2 and not _adds_to(short, long_name):
        # the long name repeats the number: "1 : 1" says it twice
        parts = parts[:1]
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

# the modes a line can be named by, as translated in common.line_mode_*
LINE_MODES = ("tram", "metro", "train", "bus", "coach", "ferry", "cable_tram",
              "aerial_lift", "funicular", "trolleybus", "monorail")


def line_mode(route_type):
    """The mode of a GTFS route_type, basic or extended, or None."""
    try:
        n = int(str(route_type))
    except ValueError:
        return None
    if n == 0 or 900 <= n < 1000:
        return "tram"
    if n == 1 or 400 <= n < 500:
        return "metro"
    if n == 2 or 100 <= n < 200:
        return "train"
    if n == 3 or 700 <= n < 800:
        return "bus"
    if 200 <= n < 300:
        return "coach"
    if n == 4 or 1000 <= n < 1100 or n == 1200:
        return "ferry"
    if n == 5:
        return "cable_tram"
    if n == 6 or 1300 <= n < 1400:
        return "aerial_lift"
    if n in (7, 1400):
        return "funicular"
    if n in (11, 800):
        return "trolleybus"
    if n == 12:
        return "monorail"
    return None


def with_modes(options, words):
    """The labels to show for route options, the mode in brackets at the end
    where lines of one number run different modes.

    IDFM lists three lines 6 once the operator is not narrowed: the metro,
    the bus that replaces it during works, and a bus of Vallée Sud Grand
    Paris. Their ends tell them apart, their mode does it at a glance:
    "6 : Nation ↔ Charles de Gaulle - Étoile (métro)". A number no other
    line wears, or worn by lines of one mode, keeps its label. words maps a
    mode to the word shown, in the user's language.
    """
    labels = [option.split("##")[2] for option in options]
    modes = [line_mode(option.split("##")[0]) for option in options]

    def number(label):
        return label.split(" : ")[0].split(" · ")[0].strip().casefold()

    seen = {}
    for label, mode in zip(labels, modes):
        seen.setdefault(number(label), set()).add(mode)
    return [f"{label} ({words.get(mode, mode)})"
            if mode and len(seen[number(label)]) > 1 else label
            for label, mode in zip(labels, modes)]


def get_route_labels(schedule, route_ids, gtfs_dir=None, filename=None):
    """Readable names for route_ids, as {route_id: "41 : GARE - ESAT RODIN"}.

    routes survives a prune even when its trips do not, so these names are
    available for lines the datasource no longer carries any timetable for -
    which is exactly when they need to be offered back. gtfs_dir and filename
    let the source zip's trips name where a line goes (see route_ends).
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
    needs_ends = [r[0] for r in rows if not _adds_to(r[1], r[2])]
    endpoints = route_ends(schedule, gtfs_dir, filename, needs_ends)
    for route_id, short, long in rows:
        out[route_id] = _route_label(short, long, endpoints.get(route_id), route_id)
    # a route the feed declares but routes does not: keep it selectable
    for r in route_ids:
        out.setdefault(r, r)
    return {r: out[r] for r in route_ids}
