"""The direction of a line an entry rides, once the flow has its two
places: the one it must keep for the pair (get_pair_direction), what each
direction reads as (get_direction_labels), and whether a trip rides the
pair at all (has_trip_between), which the flow asks of the mirror journey.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
import logging
import statistics
from typing import TYPE_CHECKING, Any

from sqlalchemy.sql import text

from .clocks import gtfs_seconds
from .stop_rules import _alights, _boards, _no_call_between, _place_group
from .places import _RIDES, _line_of, _line_rows, _loop_termini, _origin_boarding, _ways_of

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule
    from sqlalchemy.engine import Connection

_LOGGER = logging.getLogger(__name__)


def _closed_calls(conn: Connection, route_id: str) -> dict[tuple[str, int], tuple[bool, bool]]:
    """{(trip_id, stop_sequence): (boards, alights)} of the calls of the
    trips _STOP_ROWS samples where no trip of the pattern takes riders on,
    or none sets them down; every other call is open both ways."""
    return {(row[0], row[1]): (bool(row[2]), bool(row[3])) for row in _line_rows(conn, f"""
        with {_RIDES}, sample as (
            select trip_id, min(trip_id) over (partition by stops) as sample_id from ride
        )
        select sample.sample_id, st.stop_sequence,
               max({_boards("st")}) as boards, max({_alights("st")}) as alights
        from sample
        inner join stop_times st on st.trip_id = sample.trip_id
        group by sample.sample_id, st.stop_sequence
        having boards = 0 or alights = 0""", {"route_id": route_id, "direction": None})}  # noqa: S608


def _shortest_ride(calls: Iterable[tuple[str, bool, bool]], origin: str,
                   destination: str) -> tuple[int, int] | None:
    """(where the ride boards, where it alights) of the trip's shortest ride
    from origin to destination, calls being its (place, boards, alights) in
    call order; None when it rides none. Only a call the rider can use is
    an end, or in the way, as in the departure query: Kennington on a loop,
    the terminus passed again with no way on or off, made the ride look
    one stop long."""
    best = None
    last_origin = None
    for i, (p, boards, alights) in enumerate(calls):
        if p == origin and boards:
            last_origin = i
        elif p == destination and alights and last_origin is not None:
            if best is None or i - last_origin < best[1] - best[0]:
                best = (last_origin, i)
            last_origin = None
    return best


def get_pair_direction(schedule: Schedule, route_id: str, origin_stop_id: str,
                       destination_stop_id: str, towards: str | None = None) -> str | None:
    """The direction an entry must keep for this pair, or None.

    towards, the way the rider answered get_towards with, picks the rotation
    when the trips riding the pair that way agree on one label; otherwise,
    and when nothing was asked, the rules below.

    The pair and the order of the stops on one trip say which way the
    rider goes, whatever the labels. Only a loop leaves it open: TAO 22 runs
    Zenith to Zenith both ways round, and a trip leaving Zenith reaches any
    stop of the loop, the short way on one rotation and the long way on the
    other; on that line the 29 pairs with Zenith at one end are the only
    ones where this happens, out of 870: a trip calls at the terminus at
    both ends, so it rides a pair with the terminus at one end whichever
    way round it goes. Then the rotation with the fewest stops is kept,
    when its trips agree on one direction.

    Stops rather than minutes: on TAO 22 both pick the same rotation for 54
    of the 58 pairs, the 2 that differ are 42 seconds apart, and Zou 989
    gives every stop of a trip the same time, so minutes decide nothing
    there. They settle a tie in stops (a stop halfway round), by the median
    ride time of each rotation; a tie on both keeps no direction.
    """
    with schedule.engine.connect() as conn:
        _kept, _station_names, place, trips = _line_of(conn, route_id)
        labels = dict(conn.execute(text(
            "select trip_id, direction_id from trips where route_id = :route_id"),
            {"route_id": route_id}).fetchall())
        boarding = (_origin_boarding(conn, route_id, origin_stop_id)
                    if towards is not None else None)
        closed = _closed_calls(conn, route_id)
    origin = place.get(origin_stop_id, origin_stop_id)
    destination = place.get(destination_stop_id, destination_stop_id)
    termini = _loop_termini(trips, place)
    if origin not in termini and destination not in termini:
        return None
    if towards is not None:
        way = _ways_of(trips, place, origin, boarding).get(towards, [])
        told = {str(labels[trip_id]) for ride, trip_id in way
                if destination in ride and labels.get(trip_id) is not None}
        if len(told) == 1:
            direction: str | None = told.pop()
            _LOGGER.debug("Pair %s -> %s on %s ridden %s, keeping direction %s",
                          origin_stop_id, destination_stop_id, route_id, towards, direction)
            return direction
    rides: list[tuple[int, Any]] = []
    for trip_id, trip_stops in trips.items():
        best = _shortest_ride([(place[s], *closed.get((trip_id, seq), (True, True)))
                               for s, seq in trip_stops], origin, destination)
        if best:
            rides.append((best[1] - best[0], labels.get(trip_id)))
    if len({label for _length, label in rides}) < 2:
        return None
    fewest = min(length for length, _label in rides)
    agreed = {str(label) for length, label in rides if length == fewest and label is not None}
    if len(agreed) > 1:
        agreed = _quickest_rotations(schedule, route_id, origin_stop_id,
                                     destination_stop_id, agreed)
    direction = agreed.pop() if len(agreed) == 1 else None
    _LOGGER.debug("Pair %s -> %s on %s is served both ways round, keeping direction %s",
                  origin_stop_id, destination_stop_id, route_id, direction)
    return direction


def _quickest_rotations(schedule: Schedule, route_id: str, origin_stop_id: str,
                        destination_stop_id: str, candidates: Collection[str]) -> set[str]:
    """Of the direction labels in candidates, the one whose shortest rides of
    the pair take the least time, by the median over its trips; all of them
    when that does not tell them apart. The rides are the departure query's:
    only a call the rider can use is an end, or in the way."""
    origin_group = _place_group("origin")
    destination_group = _place_group("destination")
    sql = f"""
    select t.direction_id, o.departure_time, d.arrival_time
    from trips t
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stop_times d on d.trip_id = t.trip_id
    where t.route_id = :route_id
      and o.stop_id in {origin_group}
      and d.stop_id in {destination_group}
      and o.stop_sequence < d.stop_sequence
      and {_boards("o")} and {_alights("d")}
      and {_no_call_between("t", "o", "d", origin_group, destination_group)}
    """  # noqa: S608
    minutes: dict[str, list[float]] = {}
    try:
        with schedule.engine.connect() as conn:
            for label, departs, arrives in conn.execute(text(sql), {
                    "route_id": route_id, "origin": origin_stop_id,
                    "destination": destination_stop_id}):
                leaves, reaches = gtfs_seconds(departs), gtfs_seconds(arrives)
                if str(label) in candidates and leaves is not None and reaches is not None:
                    minutes.setdefault(str(label), []).append((reaches - leaves) / 60)
    except (TypeError, ValueError) as ex:
        _LOGGER.debug("Could not time the rotations of %s -> %s: %s",
                      origin_stop_id, destination_stop_id, ex)
        return set(candidates)
    medians = {label: statistics.median(values) for label, values in minutes.items() if values}
    if len(medians) < 2 or len(set(medians.values())) < len(medians):
        return set(candidates)
    return {min(medians, key=lambda label: medians[label])}


def get_direction_labels(schedule: Schedule, route_id: str) -> dict[str, str]:
    """First and last stop of each direction, to label 0 and 1.

    direction_id says nothing on its own, and trip_headsign is often empty,
    so read where the vehicle actually starts and ends. A circular line ends
    where it starts, so both directions would read the same: a rotation is
    told by where it heads first out of the terminus
    ("Zénith → Zénith via Plissay, Horloge Fleurie"). Returns {"0": "A → B"}
    with only the directions that have trips.

    The label trip is the longest one of its direction: an arbitrary trip
    would as easily be a short turn, naming the line after a partial run
    (GVB tram 1 read "Surinameplein → Azartplein" for a Matterhorn line).

    A trip with no direction_id counts as direction 0, in the query itself:
    grouped apart there and merged here, the longest trip of each went into
    one list, and the label read the start of one and the end of the other.
    """
    _LOGGER.debug("Getting direction labels for route: %s", route_id)
    sql = """
    with runs as (
        select st2.trip_id as trip_id, coalesce(t2.direction_id, 0) as d,
               count(*) as n
        from trips t2
        inner join stop_times st2 on st2.trip_id = t2.trip_id
        where t2.route_id = :route_id
        group by st2.trip_id
    ),
    picked as (
        select trip_id, d from (
            select trip_id, d,
                   row_number() over (partition by d order by n desc, trip_id) as r
            from runs
        )
        where r = 1
    )
    SELECT p.d, s.stop_name, st.stop_sequence
    from picked p
    inner join stop_times st on st.trip_id = p.trip_id
    inner join stops s on s.stop_id = st.stop_id
    order by p.d, st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"route_id": route_id}).fetchall()
    stops: dict[str, list[str]] = {}
    for direction, name, _seq in rows:
        stops.setdefault(str(direction), []).append(name)
    labels: dict[str, str] = {}
    for key, names in stops.items():
        if not names or not names[0] or not names[-1]:
            continue
        label = f"{names[0]} → {names[-1]}"
        if names[0] == names[-1]:
            # circular: the two rotations serve the same stops (opposite
            # platforms share a name), so comparing stop sets says nothing;
            # what tells them apart is where each heads first
            via = [n for n in names[1:-1] if n != names[0]][:2]
            if via:
                label += " via " + ", ".join(via)
        labels[key] = label
    _LOGGER.debug("Direction labels: %s", labels)
    return labels


def has_trip_between(schedule: Schedule, route_id: str, origin_id: str, destination_id: str,
                     direction: str | int | None = None) -> bool:
    """Whether any trip of a route calls at both stops, in this order.

    This asks whether the journey exists at all, not whether a bus is due:
    a sensor set up in the evening, or on a day the line does not run, is
    still a valid sensor. Times are the coordinator's business. The stop
    pair usually implies the direction, except on a circular line where
    both rotations run it in the same order: pass direction to tell them
    apart, trips without a direction_id still matching.
    """
    direction_where = ""
    params: dict[str, Any] = {
        "route_id": route_id,
        "origin_id": origin_id,
        "destination_id": destination_id,
    }
    if direction is not None:
        direction_where = "and (t.direction_id = :direction or t.direction_id is null)"
        params["direction"] = int(direction)
    sql = f"""
    SELECT 1
    from trips t
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stop_times d on d.trip_id = t.trip_id
    where t.route_id = :route_id
      and o.stop_id in {_place_group("origin_id")}
      and d.stop_id in {_place_group("destination_id")}
      and o.stop_sequence < d.stop_sequence
      and {_boards("o")} and {_alights("d")}
      {direction_where}
    limit 1
    """
    with schedule.engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()
    _LOGGER.debug("Trip between %s and %s on %s (direction %s): %s",
                  origin_id, destination_id, route_id, direction, bool(row))
    return bool(row)
