"""Where a line goes, for its label: the destinations its trips show, read
from the source zip (headsign_ends), else the two ends of its longest trip,
from the database or from the zip (route_ends, look_alike_ends), and the
days its services run (route_spans).
"""
from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
import json
import logging
import os
import re
import zipfile
from collections import Counter, defaultdict
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from ..data.validity import runs_some_day
from ..feed.files import feed_zip, file_edition
from ..data.zip_filter import _member, table_reader, table_rows

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


# the days lines or services run: {id: (first date, last date)}, the dates
# as GTFS writes them, YYYYMMDD
type _Spans = dict[str, tuple[str, str]]


# the ends read from trips.txt, per zip edition: {(path, file_edition): (ends, spans)}
_HEADSIGN_ENDS: dict[tuple[str, tuple[int, int, int] | None], tuple[dict[str, str], _Spans]] = {}


def _names_a_place(headsign: str | None, places: frozenset[str] = frozenset()) -> bool:
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


def _read_place_words(zin: zipfile.ZipFile) -> frozenset[str]:
    """The stop names of an open feed and their first words, casefolded."""
    words: set[str] = set()
    for row in table_rows(zin, "stops.txt"):
        name = (row.get("stop_name") or "").strip().casefold()
        if name:
            words.add(name)
            words.add(re.split(r"[\s\-/]", name, 1)[0])
    return frozenset(words)


def _read_service_spans(zin: zipfile.ZipFile) -> _Spans:
    """{service_id: (first date, last date)} of an open feed.

    Both calendars count: a feed may give a service a window in
    calendar.txt, a list of dates in calendar_dates.txt, or a window that
    exceptions extend. Only the dates a service runs on are read, never
    the ones it is removed from, so a window never grows on a cancellation.
    """
    spans: _Spans = {}
    def seen(service: str | None, first: str | None, last: str | None) -> None:
        # the last column of a padded table carries the padding (Renfe)
        first, last = (first or "").strip(), (last or "").strip()
        if not service or not first or not last:
            return
        was = spans.get(service)
        spans[service] = ((min(was[0], first), max(was[1], last)) if was
                          else (first, last))
    for row in table_rows(zin, "calendar.txt"):
        # every weekday off, the window is no day the line runs:
        # Bizkaibus dated its lines from 2017, Zagreb to 2030
        if runs_some_day(row):
            seen(row.get("service_id"), row.get("start_date"), row.get("end_date"))
    for row in table_rows(zin, "calendar_dates.txt"):
        if (row.get("exception_type") or "1").strip() == "1":
            seen(row.get("service_id"), row.get("date"), row.get("date"))
    return spans


def _headsign_ends(shown: Mapping[str | None, Mapping[str, Counter[str]]],
                   place_words: frozenset[str]) -> dict[str, str]:
    """{route_id: "A ↔ B"} out of {route_id: {direction: Counter of the
    headsigns its trips show}}: the place each direction shows most often.
    A direction whose trips mostly show a code, or nothing, is left out; a
    line with none left is not in it."""
    ends: dict[str, str] = {}
    for route_id, directions in shown.items():
        places: list[str] = []
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
    return ends


def _read_trips(zip_path: str) -> tuple[dict[str, str], _Spans]:
    """What trips.txt says about every line, in one pass over it.

    Returns ({route_id: "A ↔ B"}, {route_id: (first date, last date)}): the
    destination each direction shows most often (_headsign_ends), and the
    days the line runs. Both answers come from the same reading because
    that file is the expensive one: 196 MB on the British national feed,
    and reading it twice showed."""
    shown: defaultdict[str | None, defaultdict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    serves: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
    place_words: frozenset[str] = frozenset()
    try:
        with zipfile.ZipFile(zip_path) as zin:
            member = _member(zin, "trips.txt")
            if member is None:
                return {}, {}
            calendar = _read_service_spans(zin)
            with zin.open(member) as fh:
                reader = table_reader(fh)
                headsigns = "trip_headsign" in (reader.fieldnames or [])
                for row in reader:
                    if headsigns:
                        shown[row.get("route_id")][row.get("direction_id") or ""][
                            (row.get("trip_headsign") or "").strip()] += 1
                    service = calendar.get(row.get("service_id") or "")
                    if service:
                        serves[str(row.get("route_id"))].add(service)
            if headsigns:
                place_words = _read_place_words(zin)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read the trips of %s: %s", zip_path, ex)
        return {}, {}
    spans = {route_id: (min(w[0] for w in windows), max(w[1] for w in windows))
             for route_id, windows in serves.items()}
    return _headsign_ends(shown, place_words), spans


def headsign_ends(gtfs_dir: str | None, filename: str | None, route_ids: Iterable[str]) -> dict[str, str]:
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


def _from_trips(gtfs_dir: str | None, filename: str | None) -> tuple[dict[str, str], _Spans]:
    """The pair _read_trips builds, read once per edition of the zip."""
    zip_path = feed_zip(gtfs_dir, filename) if gtfs_dir and filename else None
    if not zip_path or not os.path.exists(zip_path):
        return {}, {}
    key = (zip_path, file_edition(zip_path))
    if key not in _HEADSIGN_ENDS:
        for old in [k for k in _HEADSIGN_ENDS if k[0] == zip_path]:
            del _HEADSIGN_ENDS[old]
        _HEADSIGN_ENDS[key] = _read_trips(zip_path)
    return _HEADSIGN_ENDS[key]


def route_spans(gtfs_dir: str | None, filename: str | None, route_ids: Iterable[str]) -> _Spans:
    """{route_id: (first date, last date)}: the days these lines run.

    Read from the same pass over trips.txt as the destinations, so a list
    that already named its lines pays nothing more for their dates.
    """
    route_ids = {str(r) for r in route_ids}
    if not route_ids:
        return {}
    _, spans = _from_trips(gtfs_dir, filename)
    return {r: spans[r] for r in route_ids if r in spans}


def _read_trip_calls(zin: zipfile.ZipFile, route_ids: Container[str]) -> tuple[
        dict[str, tuple[str, str]], Counter[str], dict[str, tuple[int, str]],
        dict[str, tuple[int, str]]]:
    """({trip_id: (route_id, direction_id)} of these lines, Counter of each
    trip's calls, {trip_id: (sequence, stop_name)} of its first and of its
    last call at a stop with a name), read from the zip's tables."""
    trips: dict[str, tuple[str, str]]
    calls: Counter[str]
    first: dict[str, tuple[int, str]]
    last: dict[str, tuple[int, str]]
    trips, calls, first, last = {}, Counter(), {}, {}
    names = {str(row["stop_id"]): row.get("stop_name") for row in table_rows(zin, "stops.txt")}
    for row in table_rows(zin, "trips.txt"):
        route_id, trip = row.get("route_id"), row.get("trip_id")
        if route_id and trip and route_id in route_ids:
            # no direction is direction 0, as the database reads it
            trips[trip] = (route_id, row.get("direction_id") or "0")
    for row in table_rows(zin, "stop_times.txt"):
        trip, stop_id = row.get("trip_id"), row.get("stop_id")
        if not trip or trip not in trips or not stop_id:
            continue
        sequence = int(str(row.get("stop_sequence")))
        calls[trip] += 1
        # a stop with no name says nothing: the ends are the named stops
        name = names.get(stop_id)
        if not name:
            continue
        if trip not in first or sequence < first[trip][0]:
            first[trip] = (sequence, name)
        if trip not in last or sequence > last[trip][0]:
            last[trip] = (sequence, name)
    return trips, calls, first, last


def _read_stop_ends(zip_path: str, route_ids: Container[str]) -> dict[str, str]:
    """{route_id: "A > B"} for these lines: the first and last named stop of
    the trip that calls at the most stops, direction 0 first, read from the
    zip's stop_times.txt, as _route_endpoints reads it from the database."""
    try:
        with zipfile.ZipFile(zip_path) as zin:
            trips, calls, first, last = _read_trip_calls(zin, route_ids)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read the stops of %s: %s", zip_path, ex)
        return {}
    # the longest trips of each line, direction 0 first among them
    most: dict[str, tuple[int, str]] = {}
    for trip, (route_id, direction) in trips.items():
        if calls[trip] and (route_id not in most or (-calls[trip], direction) < most[route_id]):
            most[route_id] = (-calls[trip], direction)
    ends: dict[str, tuple[str, str]] = {}
    for trip, (route_id, direction) in trips.items():
        if route_id not in most or (-calls[trip], direction) != most[route_id] or trip not in first:
            continue
        a, b = first[trip][1], last[trip][1]
        # a loop ends where it starts: "A > A" told two look-alikes apart
        # by nothing
        if a == b:
            continue
        # among the longest, the one whose ends come first by name, in the
        # order it rides them, as _route_endpoints chooses
        if route_id not in ends or (a, b) < ends[route_id]:
            ends[route_id] = (a, b)
    return {route_id: f"{a} > {b}" for route_id, (a, b) in ends.items()}


# the ends read from stop_times.txt, per zip edition: {(path, file_edition): ends}
_STOP_ENDS: dict[tuple[str, tuple[int, int, int] | None], dict[str, str | None]] = {}


# the largest stop_times.txt worth walking for a handful of look-alikes.
# What the read buys does not grow with the feed, what it costs does: Renfe
# levels 648 look-alikes in 0.5 s (21 MB), SNCF 58 in 1.5 s (54 MB), the
# German national feed 24 in 95 s (2.2 GB), the British one 11 in 188 s
# (5.1 GB). Measured on 48 feeds, the rate falls off between 76 MB and
# 401 MB and never recovers, so the cap sits between the two.
_STOP_TIMES_CAP = 150 * 1024 * 1024


def _stop_times_size(zip_path: str) -> int | None:
    """How big stop_times.txt is unpacked, read from the zip's directory.

    The central directory carries every member's size, so this answers in
    milliseconds without decompressing a byte. Returns None when the member
    or the zip cannot be read, which leaves the decision to the caller.
    """
    try:
        with zipfile.ZipFile(zip_path) as zin:
            member = _member(zin, "stop_times.txt")
            if member is not None:
                return zin.getinfo(member).file_size
    except (OSError, zipfile.BadZipFile) as ex:
        _LOGGER.debug("Could not size the stops of %s: %s", zip_path, ex)
    return None


def look_alike_ends(schedule: Schedule | None, gtfs_dir: str | None, filename: str | None,
                    route_ids: Iterable[str]) -> dict[str, str]:
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
    zip_path = feed_zip(gtfs_dir, filename) if gtfs_dir and filename else None
    if not missing or not zip_path or not os.path.exists(zip_path):
        return ends
    size = _stop_times_size(zip_path)
    if size is not None and size > _STOP_TIMES_CAP:
        _LOGGER.debug(
            "Not reading the %s MB of stops of %s to name %s look-alike routes",
            size // (1024 * 1024), filename, len(missing))
        return ends
    key = (zip_path, file_edition(zip_path))
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
    ends.update({r: name for r in missing if (name := known[r])})
    return ends


def route_ends(schedule: Schedule, gtfs_dir: str | None, filename: str | None,
               route_ids: list[str]) -> dict[str, str]:
    """The ends of these lines: the trips' destinations where the zip names
    them, the first and last stop of the longest imported trip otherwise."""
    ends = headsign_ends(gtfs_dir, filename, route_ids)
    ends.update(_route_endpoints(schedule, [r for r in route_ids if str(r) not in ends]))
    return ends


def _route_endpoints(schedule: Schedule, route_ids: list[str]) -> dict[str, str]:
    """Where each of these lines starts and ends, as "A > B".

    Read from the trip of the line that calls at the most stops: the first
    trip by id is often a short turn (IDFM metro 4: Montparnasse, not
    Bagneux). It is only asked for the lines whose name is unusable, so the
    query stays small even on a national feed.
    """
    if not route_ids:
        return {}
    route_ids = sorted(route_ids)
    # the longest trips of each line, direction 0 first: every one of them,
    # for the choice between them is made on their stops below. Left to
    # max(n), SQLite picked whichever tied trip it met first, and the label
    # turned round, A > B, then B > A, after a rebuild
    sql = """
    with calls as (
        select t.route_id, t.trip_id, coalesce(t.direction_id, 0) as d, count(*) as n
        from trips t inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id in (select value from json_each(:routes)) group by t.trip_id
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
    """
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(text(sql), {"routes": json.dumps(route_ids)}).fetchall()
    except SQLAlchemyError as ex:
        # without this the label falls back to the route_id, which is what it
        # did before: ugly, but never empty
        _LOGGER.warning("Could not read the ends of %s routes: %s", len(route_ids), ex)
        return {}
    trips: dict[tuple[str, str], tuple[tuple[int, str], tuple[int, str]]] = {}
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
    ends: dict[str, tuple[str, str]] = {}
    for (route_id, _trip), (first, last) in trips.items():
        if not first or not last or first[1] == last[1]:
            continue
        pair = (first[1], last[1])
        if route_id not in ends or pair < ends[route_id]:
            ends[route_id] = pair
    return {route_id: f"{a} > {b}" for route_id, (a, b) in ends.items()}
