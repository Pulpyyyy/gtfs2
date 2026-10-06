"""The places of a line, as the config flow offers them: the stops a rider
can start from (get_stop_list), which way they go from there (get_towards),
and where they can get to (get_destination_stop_list); the direction an
entry keeps for its pair is pair_direction.py's.

A place is a stop, or the stops of one station taken together; the list
follows the order a trip calls at them, branches and loops included
(place_order.py).
"""
from __future__ import annotations

from collections.abc import Callable, Container, Mapping, Sequence
import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.sql import text

from .destination_order import _placed_in_order, _rides_after, _riding_order, _tails_of
from .gtfs_db import file_edition
from .stop_rules import _alights, _boards, _place_group
from .place_order import _Trips, _ride_of, _trips_of

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule
    from sqlalchemy.engine import Connection

_LOGGER = logging.getLogger(__name__)


# each trip of the line this way with the stops it calls at, in order: its
# pattern. _STOP_ROWS and _origin_boarding sample the same trip per pattern
# from it
_RIDES = """ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id = :route_id
        and (:direction is null or t.direction_id = :direction or t.direction_id is null)
        group by t.trip_id
    )"""


# The trips of one direction ride a handful of distinct stop patterns, a
# few thousand times each over the feed's calendar (TAO tram A: 4214 trips,
# 27 stops). The walk only needs each pattern once, so one trip stands for
# every trip that rides the same stops in the same order: the lowest
# trip_id of the pattern, which is also the trip _ride_of would have walked
# first among them, so the result is the one reading every trip gives.
# The signature is concatenated in scan order on purpose: sorting it
# first costs more than reading every trip did (TAO A: 4.2 s against 2.8
# for the six lines, 1.2 s this way). Should the order ever vary between
# two trips of one pattern, that pattern is read twice, never lost.
_STOP_ROWS = f"""
    with {_RIDES}, sample as (
        select min(trip_id) as trip_id from ride group by stops
    )
    SELECT st.trip_id, s.stop_id, s.stop_name, st.stop_sequence, s.parent_station, station.stop_name,
           s.stop_lat, s.stop_lon
    from sample
    inner join stop_times st on st.trip_id = sample.trip_id
    inner join stops s on s.stop_id = st.stop_id
    left join stops station on station.stop_id = s.parent_station
    order by st.trip_id, st.stop_sequence
"""


_STOP_GROUP = _place_group("origin")


def _calls_where(can: Callable[[str], str]) -> str:
    """SQL: the records of a line where some trip of it this way lets
    riders do what can(alias) says, _boards or _alights."""
    return f"""
    select distinct st.stop_id
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    where t.route_id = :route_id
    and (:direction is null or t.direction_id = :direction or t.direction_id is null)
    and {can("st")}
"""


# the records of a line where some trip takes riders on (BOARDING) or sets
# them down (ALIGHTING): the lists offer a place when one of its records is
_BOARDING_ROWS = _calls_where(_boards)
_ALIGHTING_ROWS = _calls_where(_alights)


def _line_ways(conn: Connection, route_id: str, direction: str | int | None = None,
               ) -> tuple[Callable[[str], bool], Callable[[str], bool]]:
    """Whether the line, over every trip of it this way, ever takes riders
    on, or sets them down, at a record: (boards, alights), each answering
    a stop_id.

    The drawn trip's own pickup_type says how THAT trip calls; a night
    train's 1 at a station the next TER boards at says nothing of the
    line. Read per place, not per record: a station's other platform is
    the same place to the rider (see _place_group), so a trip boarding
    there makes the whole place a way on. A record the sampled trips of
    _line_of do not place is judged on its own rows. In doubt the answer
    is yes: a no shuts a stop out of a card's lists, and only the feed's
    own word, on every trip, may do that.
    """
    params = {"route_id": route_id, "direction": _direction_param(direction)}
    _kept, _station_names, place, _trips = _line_of(conn, route_id, direction)

    def ways(sql: str) -> Callable[[str], bool]:
        records = {row[0] for row in conn.execute(text(sql), params)}
        places = {place[s] for s in records if s in place}
        return lambda stop_id: stop_id in records or place.get(stop_id) in places

    return ways(_BOARDING_ROWS), ways(_ALIGHTING_ROWS)


# Which end the list starts from. One order serves both ways, so half the
# riders read it backwards whichever end comes first; it follows the way most
# trips labelled direction 0 ride it, the way a timetable of the line is
# usually printed first. Only the reading order comes from the label: nothing
# is built from it, and a wrong one (GVB 1 files 455 Matterhorn > Azartplein
# trips and 324 Azartplein > Surinameplein trips as 0) turns the list round
# and hides nothing.
_HEADING_ROWS = """
    with ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id = :route_id and t.direction_id = 0
        group by t.trip_id
    )
    select stops, count(*) from ride group by stops
"""


def _labels_of(kept: Sequence[Sequence[Any]],
               station_names: Mapping[str, str | None]) -> dict[str, str]:
    """{stop_id: readable name} for the stops whose name the line meets
    more than once; a stop met once keeps its plain name.

    Records of one place are already one entry, so a repeat left here is two
    places of the same name. The feed sometimes knows what tells them apart:
    on line 1 in Amsterdam one "Surinameplein" belongs to the Surinameplein
    station and the other to Hoofdweg, two hundred metres away. Often it
    does not: Zou 926 calls at five villages' "Centre". So a repeat carries
    its station when the station adds something, and falls back on its rank
    in the order the line calls at them when it does not. The value keeps
    the id untouched, only the readable part changes.
    """
    by_name: dict[str, list[Sequence[Any]]] = {}
    for x in kept:
        by_name.setdefault(x[1], []).append(x)
    label: dict[str, str] = {}
    for name, group in by_name.items():
        if len(group) == 1:
            continue
        for x in group:
            station_name = station_names.get(x[0])
            label[x[0]] = (f"{name} ({station_name})"
                           if station_name and station_name not in name
                           else name)
        # the station settles it only if it settles it for everyone: where
        # two of them still read the same, those keep their rank instead,
        # and a stop the station already told apart keeps its plain reading
        still_shared = [x for x in group
                        if [y for y in group if label[y[0]] == label[x[0]]][1:]]
        for n, x in enumerate(still_shared, 1):
            label[x[0]] = f"{label[x[0]]} #{n}"
    return label


def _entries_of(kept: Sequence[Sequence[Any]], label: Mapping[str, str]) -> list[str]:
    """The picker's entries, "stop_id: Name (sequence)": get_next_departure
    cuts the id back out of the value, only the name is the user's to read."""
    return [f"{x[0]}: {label.get(x[0], x[1])} ({x[2]})" for x in kept]


def _direction_param(direction: str | int | None) -> int | None:
    """None for no direction (the whole line), else 0 or 1."""
    if direction is None or str(direction) not in ("0", "1"):
        return None
    return int(direction)


# the rows the flow's screens read of a line, per edition of the database:
# the origin, towards, destination and pair screens each read the same
# ones again, every trip of the line grouped each time (TAO tram A, 1.5 s
# a read). The last few only: a flow reads one line at a time
_LINE_ROWS: dict[tuple[Any, ...], Sequence[Any]] = {}
_LINE_ROWS_KEPT = 8


def _line_rows(conn: Connection, sql: str, params: Mapping[str, Any]) -> Sequence[Any]:
    """conn.execute(text(sql), params).fetchall(), kept while the database
    file stays the same one, unchanged; read afresh when it cannot say."""
    try:
        path = conn.engine.url.database
    except AttributeError:
        path = None
    edition = file_edition(path)
    if edition is None:
        return conn.execute(text(sql), params).fetchall()
    key = (path, edition, sql, tuple(sorted(params.items())))
    rows = _LINE_ROWS.get(key)
    if rows is None:
        rows = conn.execute(text(sql), params).fetchall()
        while len(_LINE_ROWS) >= _LINE_ROWS_KEPT:
            _LINE_ROWS.pop(next(iter(_LINE_ROWS)), None)
        _LINE_ROWS[key] = rows
    return rows


def _line_of(conn: Connection, route_id: str, direction: str | int | None = None,
             ) -> tuple[list[list[Any]], dict[str, str | None], dict[str, str], _Trips]:
    """_ride_of for a route, its sampled trips kept beside: (kept,
    station_names, place, trips)."""
    rows = _line_rows(conn, _STOP_ROWS, {
        "route_id": route_id, "direction": _direction_param(direction)})
    heading = _line_rows(conn, _HEADING_ROWS, {"route_id": route_id})
    return _ride_of(rows, heading)


def _loop_termini(trips: _Trips, place: Mapping[str, str]) -> set[str | None]:
    """The places some trip of the line starts and ends at: a loop's terminus
    (TAO 22 runs Zénith to Zénith both ways round)."""
    return {place.get(stops[0][0]) for stops in trips.values()
            if stops and place.get(stops[0][0]) == place.get(stops[-1][0])}


def _origin_boarding(conn: Connection, route_id: str, origin_stop_id: str,
                     direction: str | int | None = None) -> set[tuple[str, int]]:
    """The calls of the route at the origin's place a rider can get on at,
    as {(trip_id, stop_sequence)}: what _calls_out starts a ride from.

    Keyed on the trip _STOP_ROWS samples for each pattern of stops, as the
    rides are read from that trip alone: a call counts when any trip of the
    pattern boards there. Read from the sampled trip's own flag, a pattern
    whose sample only sets down at the origin lost its way out (Amtrak's
    Stockton, where one Thruway bus of three does not pick up)."""
    return {(row[0], row[1]) for row in _line_rows(conn, f"""
        with {_RIDES}, sample as (
            select trip_id, min(trip_id) over (partition by stops) as sample_id from ride
        )
        select distinct sample.sample_id, st.stop_sequence
        from sample
        inner join stop_times st on st.trip_id = sample.trip_id
        where st.stop_id in {_STOP_GROUP}
        and {_boards("st")}""", {"route_id": route_id, "origin": origin_stop_id,  # noqa: S608
                                 "direction": _direction_param(direction)})}


def _calls_out(trips: Mapping[str, Sequence[tuple[str, int | None]]], place: Mapping[str, str],
               origin_place: str,
               boarding: Container[tuple[str, int | None]] | None = None) -> list[tuple[list[str], str]]:
    """(ride, trip_id) for each ride out of the origin place, the ride as
    places: from a call at it to the trip's next call at it, or its end. A
    trip passing the origin twice (Palm Bus 21 out and back through Gare
    Maritime) gives a ride from each.

    boarding, when given, holds the (trip_id, stop_sequence) calls at the
    origin a rider can get on at: a ride from any other call is nobody's
    way out (Zou 620 only sets down at Pont des Gabres on its way into
    Cannes) and is left out, the calls still cutting the rides as before."""
    rides: list[tuple[list[str], str]] = []
    for trip_id, trip_stops in trips.items():
        ride: list[str] | None
        ride, way_on = None, True
        for stop_id, seq in trip_stops:
            p = place.get(stop_id, stop_id)
            if p == origin_place:
                if ride and way_on:
                    rides.append((ride, trip_id))
                ride = []
                way_on = boarding is None or (trip_id, seq) in boarding
            elif ride is not None:
                ride.append(p)
        if ride and way_on:
            rides.append((ride, trip_id))
    return rides


def _ways_of(trips: _Trips, place: Mapping[str, str], origin_place: str,
             boarding: Container[tuple[str, int | None]] | None = None) -> dict[str, list[tuple[list[str], str]]]:
    """The ways out of an origin, {way: [(ride, trip_id)]}: where the bus
    goes, as the bus itself shows it.

    A way is the terminus of the trip, the place it ends at, read from the
    trips and never from direction_id. Only when the terminus tells nothing
    does the next stop come with it: a loop's terminus, which both rotations
    end at (TAO 22 reaches Zénith by Vieux Poirier or the long way round by
    Bois Girault), or the origin itself (from Zénith, by Plissay or by Jean
    Moulin). A trip ending short of a terminus (GVB 1 turns trams at
    Surinameplein) goes the way of the trips that leave for the same next
    stop and pass its end, or every stop of its ride but the end. The way is
    the stop_id of the terminus, the next stop's appended after "|" when it
    is part of it. boarding keeps the rides a rider can start (_calls_out).
    """
    loop_termini = _loop_termini(trips, place)
    rides: dict[tuple[str | None, str | None], list[tuple[list[str], str]]] = {}
    for ride, trip_id in _calls_out(trips, place, origin_place, boarding):
        end = place.get(trips[trip_id][-1][0])
        told_by_next = end in loop_termini or end == origin_place
        rides.setdefault((end, ride[0] if told_by_next else None), []).append((ride, trip_id))
    folded: dict[tuple[str | None, str | None], tuple[str | None, str | None]] = {}
    for key, calls in rides.items():
        if key[1] is not None:
            continue
        # on the way to another terminus: the trips there pass its end, or
        # every stop of its rides but the end when that end is a pole of its
        # own (GVB 1 turns at Surinameplein (Hoofdweg), the line goes on by
        # Surinameplein; TAO 40 ends at quai C, the line goes on by quai D)
        # a ride's body is the ride without its end, which a trip may enter
        # on two records in a row (TAO 3 closes on two Belneuf poles)
        for other, other_calls in rides.items():
            if other != key and other[1] is None and any(
                    ride[0] == mine[0]
                    and (key[0] in [p for p in ride if p != ride[-1]]
                         or {p for p in mine if p != mine[-1]} <= set(ride))
                    for ride, _trip_id in other_calls for mine, _mine_trip in calls):
                folded[key] = other
                break
    ways: dict[str, list[tuple[list[str], str]]] = {}
    for key, calls in rides.items():
        # two poles of one terminus fold into each other: one key for both
        chain: list[tuple[str | None, str | None]] = []
        while key in folded and key not in chain:
            chain.append(key)
            key = folded[key]
        if key in chain:
            key = min(chain[chain.index(key):])
        ways.setdefault("|".join(p for p in key if p), []).extend(calls)
    return ways


def get_towards(schedule: Schedule, route_id: str, origin_stop_id: str) -> list[tuple[str, str]]:
    """The ways a rider can leave the origin, or nothing to ask.

    Asked only when it settles something: when buses from that place go
    different ways. At the end of a line every bus goes the same way, and
    nothing is asked. From a loop's terminus the two rotations are asked
    (TAO 22 sends buses round both ways at once from Zénith), and the answer
    is the rotation the entry keeps; mid-way round, the short or the long way
    to Zénith. Each answer keeps its own destinations. A line with three
    termini offers three ways: that is what its buses show.

    Returns [(way, label)], in the order of the list. A way reads as its
    terminus; with the next stop before it when the terminus alone tells
    nothing ("Vieux Poirier … Zénith"), and as the next stop alone when the
    terminus is the origin, which is nowhere to go ("Plissay").
    """
    with schedule.engine.connect() as conn:
        kept, station_names, place, trips = _line_of(conn, route_id)
        boarding = _origin_boarding(conn, route_id, origin_stop_id)
    origin_place = place.get(origin_stop_id, origin_stop_id)
    ways = _ways_of(trips, place, origin_place, boarding)
    if len(ways) < 2:
        return []
    label = _labels_of(kept, station_names)
    names = {x[0]: label.get(x[0], x[1]) for x in kept}
    position = {x[0]: i for i, x in enumerate(kept)}
    shown: list[tuple[int, int, str, str]] = []
    for way in ways:
        end, _sep, following = way.partition("|")
        if not following or following == end:
            text = names.get(end, end)
        elif end == origin_place:
            text = names.get(following, following)
        else:
            text = f"{names.get(following, following)} … {names.get(end, end)}"
        shown.append((position.get(end, 0), position.get(following, 0), way, text))
    # from a loop's terminus, the trips round the loop and the trips ending
    # at the next stop both read as that stop (Zou 989 at Gare Routière):
    # the ones coming back say so
    texts = [text for _end, _following, _way, text in shown]
    shown = [(end, following, way,
              f"{text} … {names.get(origin_place, origin_place)}"
              if texts.count(text) > 1 and way.startswith(origin_place + "|") else text)
             for end, following, way, text in shown]
    shown.sort()
    _LOGGER.debug("Ways out of %s on %s: %s", origin_stop_id, route_id, shown)
    return [(way, text) for _end, _following, way, text in shown]


def get_stop_list(schedule: Schedule, route_id: str, direction: str | int | None = None) -> list[str]:
    """Every place a route rides, one entry each, in riding order.

    Without a direction, the whole line both ways round, which is what the
    flow offers: the rider picks where they are, not a label of the feed.
    A direction still narrows it to that direction's trips.

    A place no trip of the line takes riders on at is left out: the rider
    picks where they get on, and a call the feed flags as set-down only
    (pickup_type 1 on every trip, see _boards) is nowhere to get on. The
    order is still read from every call, so a place kept sits where the
    line rides it.
    """
    _LOGGER.debug("Getting stops list for route: %s direction: %s", route_id, direction)
    with schedule.engine.connect() as conn:
        kept, station_names, place, _trips = _line_of(conn, route_id, direction)
        boarding = {row[0] for row in conn.execute(text(_BOARDING_ROWS), {
            "route_id": route_id, "direction": _direction_param(direction)})}
    boardable = {place[s] for s in boarding if s in place}
    kept = [x for x in kept if x[0] in boardable]
    stops = _entries_of(kept, _labels_of(kept, station_names))
    _LOGGER.debug(f"Route stops: {stops}")
    return stops


def _sample_rows(conn: Connection, samples: Mapping[str, Sequence[tuple[int, str]]]) -> list[tuple[Any, ...]]:
    """The sampled rides' calls at the stops the feed describes, shaped as
    _STOP_ROWS rows, in trip then sequence order."""
    stop_ids = sorted({stop_id for ride in samples.values() for _sequence, stop_id in ride})
    records = {row[0]: row[1:] for row in conn.execute(text("""
        select s.stop_id, s.stop_name, s.parent_station, station.stop_name, s.stop_lat, s.stop_lon
        from stops s
        left join stops station on station.stop_id = s.parent_station
        where s.stop_id in (select value from json_each(:stop_ids))
        """), {"stop_ids": json.dumps(stop_ids)}).fetchall()}
    return [(trip_id, stop_id, name, sequence, parent, station, lat, lon)
            for trip_id in sorted(samples)
            for sequence, stop_id in samples[trip_id] if stop_id in records
            for name, parent, station, lat, lon in [records[stop_id]]]


def get_destination_stop_list(schedule: Schedule, route_id: str, direction: str | int | None,
                              origin_stop_id: str, towards: str | None = None) -> list[str]:
    """The places a trip really reaches from the departure place.

    towards, a way get_towards offered, keeps the rides leaving that way
    only: the places on the rider's side, in riding order.

    Only the trips that call at the origin are read, and of each only the
    part after it, so every entry offered can be paired with the origin on
    at least one trip and nothing has to be rejected afterwards. Whether
    that trip runs today is the coordinator's business. The origin is
    matched as a whole place, every record of it, the way the departure
    query matches it; a loop that calls at it twice is read from the first
    call, which keeps the way back on offer. Without a direction both ways
    round are read, each in riding order from the origin. The entries are
    the line's, records and labels, so a stop reads the same on both
    screens; the origin's own place is not offered.

    Only the calls the rider can make count: a trip is through the origin
    from its first call there that takes riders on, and a place is offered
    when some such trip sets riders down there afterwards (see _boards and
    _alights). A call with no way off still orders the places around it.
    """
    _LOGGER.debug("Getting destinations for route: %s direction: %s from: %s",
                  route_id, direction, origin_stop_id)
    # every call after the origin of every trip of the route through it,
    # read once: the rides, how many trips each stands for and where riders
    # get off all come out of it (_rides_after)
    calls_sql = f"""
    with through as (
        select trip_id, min(stop_sequence) as origin_sequence
        from stop_times where stop_id in {_STOP_GROUP} and {_boards("stop_times")}
        group by trip_id
    )
    SELECT t.trip_id, st.stop_sequence, st.stop_id, {_alights("st")}
    from trips t
    inner join through o on o.trip_id = t.trip_id
    inner join stop_times st on st.trip_id = t.trip_id
        and st.stop_sequence > o.origin_sequence
    where t.route_id = :route_id
    and (:direction is null or t.direction_id = :direction or t.direction_id is null)
    order by t.trip_id, st.stop_sequence
    """  # noqa: S608
    scope = {"route_id": route_id, "direction": _direction_param(direction)}
    with schedule.engine.connect() as conn:
        line, station_names, place, _line_trips = _line_of(conn, route_id, direction)
        samples, trip_count, alighting = _rides_after(
            conn.execute(text(calls_sql), {**scope, "origin": origin_stop_id}).fetchall())
        rows = _sample_rows(conn, samples)
        boarding = (_origin_boarding(conn, route_id, origin_stop_id, direction)
                    if towards is not None else None)
    position = {x[0]: i for i, x in enumerate(line)}
    by_place = {x[0]: x for x in line}
    trips, _info = _trips_of(rows)
    origin_place = place.get(origin_stop_id, origin_stop_id)
    # the rows start right after each trip's first call at the origin
    calls = _calls_out({t: [(origin_stop_id, None), *s] for t, s in trips.items()},
                       place, origin_place)
    if towards is not None:
        # the rides of the way get_towards offered, read from the same trips
        way = _ways_of(_line_trips, place, origin_place, boarding).get(towards, [])
        chosen = {tuple(ride) for ride, _trip_id in way}
        calls = [(ride, trip_id) for ride, trip_id in calls if tuple(ride) in chosen]
    # a place is offered where the rides kept set riders down: with a way
    # asked, the other way's set-downs are not the rider's (Zou's school
    # runs towards Gare Routière call at two stops with no way off, which
    # the other way's buses set down at: they were offered that way too)
    riders = {trip_id for _ride, trip_id in calls}
    alightable = {place[s] for sample, s in alighting
                  if s in place and (towards is None or sample in riders)}
    reach, before, weight = _riding_order(calls, trip_count)
    order = _placed_in_order(reach, before, weight, position, _tails_of(calls))
    kept = [by_place[p] for p in order if p in by_place and p in alightable]
    stops = _entries_of(kept, _labels_of(line, station_names))
    _LOGGER.debug(f"Destinations from {origin_stop_id}: {stops}")
    return stops


def get_stops_between(schedule: Schedule, route_id: str, origin_stop_id: str,
                      destination_stop_id: str) -> list[str]:
    """The stops to get on or off at as well of a journey: the places
    strictly between its two, on the trips of the line that ride from one
    to the other, in riding order, one entry a place ("stop_id: Name
    (sequence)", as the other lists of the flow).

    A week of works, a stop closed for a market, a second stop nearer the
    other end of the street, a connection: a bus journey takes several,
    as a train one takes Orleans and Les Aubrais. A place where no such
    trip takes riders on nor sets them down is not offered; the
    departures hold to what each call allows (see _boards and _alights).
    """
    sql = f"""
    SELECT mid.stop_id, s.stop_name, min(mid.stop_sequence) AS sequence,
           min(mid.stop_sequence - o.stop_sequence) AS reached,
           max({_boards("mid")}) AS boards, max({_alights("mid")}) AS alights
    FROM trips t
    JOIN stop_times o ON o.trip_id = t.trip_id AND o.stop_id IN {_place_group("origin")} AND {_boards("o")}
    JOIN stop_times d ON d.trip_id = t.trip_id AND d.stop_id IN {_place_group("destination")}
        AND {_alights("d")} AND d.stop_sequence > o.stop_sequence
    JOIN stop_times mid ON mid.trip_id = t.trip_id
        AND mid.stop_sequence > o.stop_sequence AND mid.stop_sequence < d.stop_sequence
    JOIN stops s ON s.stop_id = mid.stop_id
    WHERE t.route_id = :route_id
    GROUP BY mid.stop_id
    ORDER BY reached, mid.stop_id
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"route_id": route_id, "origin": origin_stop_id,
                                        "destination": destination_stop_id}).fetchall()
    stops: list[str] = []
    named: set[str] = set()
    for stop_id, name, sequence, _reached, boards, alights in rows:
        # one entry a place: the other side of the road reads the same
        if (name or stop_id) in named or not (boards or alights):
            continue
        named.add(name or stop_id)
        stops.append(f"{stop_id}: {name or stop_id} ({sequence})")
    return stops
