"""Repair trip direction_id after import.

Some feeds label trips of both geographic senses with the same direction_id
(GVB trams 1, 7 and 17: 30 to 40 percent of trips), or scatter a few
counter-sense trips into a direction (SNCF). Every consumer downstream
filters on direction_id, so a mislabeled trip is invisible to the sensor of
its real direction and pollutes the stop list of the wrong one.

The imported database is derived and rebuilt from the zip on every update,
so it may be corrected in place: each trip is tested against the canonical
stop order of its direction and of the opposite one, and rewritten when it
contradicts its own order but rides the opposite one.

Classification is by the ORDER of the stops a trip shares with a canonical
chain, not by how many it shares: short runs end on turnback stops that the
full-length pattern never serves, so a coverage test leaves them
unclassified (GVB tram 1: 650 of 1634 trips).

Stops compare by station, not by platform: the parent_station when the feed
publishes one, the stop itself otherwise. SNCF gives one station a stop_id
per product, so two trains of one line shared no stop at all and 5623 of
its 37500 trips fit neither chain; compared by station, 3085 do not.

A route whose two directions follow the same stop order is either a loop,
whose chain returns to its first station, or a line whose direction_id
carries no sense (GVB tram 14: half of its 1404 trips ride against their
label, under both labels). Neither is repaired: the loop needs nothing, the
other holds no majority to recover a sense from, and is logged.
"""

import logging
from collections import defaultdict

from sqlalchemy.sql import text

_LOGGER = logging.getLogger(__name__)

# a trip is judged only on stops it shares with a canonical chain: at least
# 4 of them (or the whole trip when shorter), at least 30 percent of the
# trip, riding the chain in order for at least 90 percent of its steps
MIN_SHARED = 4
MIN_SHARED_RATIO = 0.3
MIN_MONOTONY = 0.9
# two canonical chains that mostly share stops in the same order follow one
# sense: order proves nothing between them
CIRCULAR_SHARED_RATIO = 0.5
CIRCULAR_MONOTONY = 0.8
# moving a pattern across changes which pattern is the modal longest of each
# direction, so a pass can uncover trips the one before left in place
MAX_PASSES = 4


def _canonical(patterns):
    """The modal longest stop pattern: the route as most riders ride it."""
    maxlen = max(len(p) for p in patterns)
    return max(
        (p for p in patterns if len(p) == maxlen),
        key=lambda p: len(patterns[p]),
    )


def _fit(seq, pos):
    """(shared stops, monotonicity) of a trip against a chain's positions."""
    hits = [pos[s] for s in seq if s in pos]
    if len(hits) < 2:
        return len(hits), 0.0
    inc = sum(1 for a, b in zip(hits, hits[1:]) if b > a)
    return len(hits), inc / (len(hits) - 1)


def _fits(seq, pos):
    hits, monotony = _fit(seq, pos)
    return (
        hits >= min(MIN_SHARED, len(seq))
        and hits >= MIN_SHARED_RATIO * len(seq)
        and monotony >= MIN_MONOTONY
    )


def _same_order(chain_a, chain_b):
    """Whether two canonical chains mostly share their stops, in one order."""
    pos_b = {sid: i for i, sid in enumerate(chain_b)}
    shared = [s for s in chain_a if s in pos_b]
    if len(shared) <= CIRCULAR_SHARED_RATIO * len(chain_a):
        return False
    _, monotony = _fit(shared, pos_b)
    return monotony > CIRCULAR_MONOTONY


def canonical_pair(patterns_by_dir):
    """((direction, chain), (direction, chain)), directions in stable order."""
    (dir_a, pat_a), (dir_b, pat_b) = sorted(
        patterns_by_dir.items(), key=lambda kv: str(kv[0])
    )
    return (dir_a, _canonical(pat_a)), (dir_b, _canonical(pat_b))


def plan_repairs(patterns_by_dir):
    """Trips to move to the opposite direction, for one route.

    patterns_by_dir: {direction: {stop_tuple: [trip_id, ...]}} with exactly
    two directions. Returns {trip_id: new_direction}, empty when the route
    is healthy or when both directions follow one stop order (a loop, or a
    direction_id without sense: same_order_report tells which).
    """
    if len(patterns_by_dir) != 2:
        return {}
    (dir_a, chain_a), (dir_b, chain_b) = canonical_pair(patterns_by_dir)
    if _same_order(chain_a, chain_b):
        return {}
    pos_a = {sid: i for i, sid in enumerate(chain_a)}
    pos_b = {sid: i for i, sid in enumerate(chain_b)}

    flips = {}
    for own_dir, opp_dir, own_pos, opp_pos in (
        (dir_a, dir_b, pos_a, pos_b),
        (dir_b, dir_a, pos_b, pos_a),
    ):
        for pattern, trip_ids in patterns_by_dir[own_dir].items():
            if _fits(pattern, opp_pos) and not _fits(pattern, own_pos):
                for trip_id in trip_ids:
                    flips[trip_id] = opp_dir
    return flips


def plan_until_stable(patterns_by_dir):
    """plan_repairs applied to the in-memory patterns until it finds nothing.

    SNCF: a second pass over the repaired patterns moved 3 to 5 more trips
    after the 80 to 90 of the first. The passes rewrite patterns_by_dir; the
    database is written once, with the sum. A trip moved back to where it
    started is not a repair.
    """
    origin = {
        trip_id: direction
        for direction, patterns in patterns_by_dir.items()
        for trip_ids in patterns.values()
        for trip_id in trip_ids
    }
    flips = {}
    for _ in range(MAX_PASSES):
        if any(not patterns for patterns in patterns_by_dir.values()):
            break
        step = plan_repairs(patterns_by_dir)
        if not step:
            break
        for own_dir in list(patterns_by_dir):
            for pattern in list(patterns_by_dir[own_dir]):
                trip_ids = patterns_by_dir[own_dir][pattern]
                new_dir = step.get(trip_ids[0])
                if new_dir is None:
                    continue
                del patterns_by_dir[own_dir][pattern]
                patterns_by_dir[new_dir].setdefault(pattern, []).extend(trip_ids)
        flips.update(step)
    return {t: d for t, d in flips.items() if d != origin[t]}


def same_order_report(patterns_by_dir, station_name):
    """Why a two-direction route whose directions follow one stop order was
    left alone: ("loop",) or ("no_sense", against, total, first, last).

    None when the two directions differ, which is the normal case. A loop
    is a chain that returns to its first station; on any other route, the
    trips that ride the shared chain the other way are counted.
    """
    (_, chain_a), (_, chain_b) = canonical_pair(patterns_by_dir)
    if not _same_order(chain_a, chain_b):
        return None
    first = station_name.get(chain_a[0], chain_a[0])
    last = station_name.get(chain_a[-1], chain_a[-1])
    if chain_a[0] == chain_a[-1] or first == last:
        return ("loop",)
    pos_a = {sid: i for i, sid in enumerate(chain_a)}
    against = total = 0
    for patterns in patterns_by_dir.values():
        for pattern, trip_ids in patterns.items():
            total += len(trip_ids)
            hits, monotony = _fit(pattern, pos_a)
            if hits >= 2 and monotony < 0.5:
                against += len(trip_ids)
    return ("no_sense", against, total, first, last)


def repair_trip_directions(schedule):
    """Rewrite mislabeled direction_id values in the imported database.

    Returns the number of repaired trips. Never raises: a failed repair must
    not fail the import that a working database already survived.
    """
    try:
        return _repair(schedule)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Direction repair failed, database left as imported: %s", ex)
        return 0


def _stations(schedule):
    """{stop_id: station}, {station: name}: the parent station when the feed
    publishes one, the stop itself otherwise."""
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT stop_id, parent_station, stop_name FROM stops")
            ).fetchall()
    except Exception:  # pylint: disable=broad-except
        # a stops table without the column: every stop is its own station
        with schedule.engine.connect() as conn:
            rows = [
                (stop_id, None, name)
                for stop_id, name in conn.execute(
                    text("SELECT stop_id, stop_name FROM stops")
                )
            ]
    station_of, station_name = {}, {}
    for stop_id, parent, name in rows:
        station = parent or stop_id
        station_of[stop_id] = station
        # the parent's own row names the station; a child only when the
        # parent has no row of its own
        if stop_id == station or station not in station_name:
            station_name[station] = name
    return station_of, station_name


def _repair(schedule):
    trip_meta = {}
    dirs_per_route = defaultdict(set)
    with schedule.engine.connect() as conn:
        for trip_id, route_id, direction in conn.execute(
            text("SELECT trip_id, route_id, direction_id FROM trips")
        ):
            if direction is None:
                continue
            trip_meta[trip_id] = (route_id, direction)
            dirs_per_route[route_id].add(direction)
        route_labels = dict(
            conn.execute(
                text("SELECT route_id, route_short_name FROM routes")
            ).fetchall()
        )

    eligible = {r for r, ds in dirs_per_route.items() if len(ds) == 2}
    if not eligible:
        _LOGGER.debug("Direction repair: no route with two directions, nothing to do")
        return 0

    station_of, station_name = _stations(schedule)

    # one streaming pass; ordering by trip_id alone rides the
    # gtfs2_stop_times_trip_id index, the few stops of each trip are sorted here
    patterns = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    current_trip = None
    current_stops = []

    def _close_trip():
        if current_trip is None:
            return
        route_id, direction = trip_meta[current_trip]
        current_stops.sort()
        pattern = []
        for _, stop_id in current_stops:
            station = station_of.get(stop_id, stop_id)
            # two platforms of one station in a row are one stop of the chain
            if not pattern or pattern[-1] != station:
                pattern.append(station)
        patterns[route_id][direction][tuple(pattern)].append(current_trip)

    with schedule.engine.connect() as conn:
        for trip_id, stop_id, stop_sequence in conn.execute(
            text(
                "SELECT trip_id, stop_id, stop_sequence FROM stop_times"
                " ORDER BY trip_id"
            )
        ):
            if trip_id != current_trip:
                _close_trip()
                current_trip = trip_id if trip_id in trip_meta else None
                current_stops = []
            if current_trip is not None:
                current_stops.append((stop_sequence, stop_id))
        _close_trip()

    flips = {}
    for route_id in eligible:
        if route_id not in patterns:
            continue
        label = route_labels.get(route_id) or route_id
        by_dir = patterns[route_id]
        total = sum(len(t) for d in by_dir.values() for t in d.values())
        route_flips = plan_until_stable(by_dir)
        if route_flips:
            _LOGGER.info(
                "Direction repair: route %s: %s of %s trips ride the opposite"
                " direction's stop order, rewriting their direction_id",
                label,
                len(route_flips),
                total,
            )
        else:
            report = same_order_report(by_dir, station_name)
            if report and report[0] == "no_sense":
                _, against, total, first, last = report
                # a route published one way under both labels has nothing
                # to repair either, but nothing is hidden from a sensor
                log = _LOGGER.warning if against else _LOGGER.info
                log(
                    "Direction repair: route %s: both directions follow the"
                    " same stop order (%s to %s) and %s of %s trips ride the"
                    " other way; direction_id carries no sense on this route,"
                    " left as published",
                    label, first, last, against, total,
                )
        flips.update(route_flips)

    if not flips:
        _LOGGER.debug("Direction repair: all trips match their direction")
        return 0

    with schedule.engine.begin() as conn:
        # pygtfs indexes trips by its own surrogate key only
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS gtfs2_trips_trip_id"
                " ON trips(trip_id)"
            )
        )
        conn.execute(
            text("UPDATE trips SET direction_id = :d WHERE trip_id = :t"),
            [{"d": d, "t": t} for t, d in flips.items()],
        )
    _LOGGER.info("Direction repair: %s trips repaired", len(flips))
    return len(flips)
