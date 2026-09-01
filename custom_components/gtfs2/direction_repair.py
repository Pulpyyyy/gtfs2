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
"""

import logging
from collections import Counter, defaultdict

from sqlalchemy.sql import text

_LOGGER = logging.getLogger(__name__)

# a trip is judged only on stops it shares with a canonical chain: at least
# 4 of them (or the whole trip when shorter), at least 30 percent of the
# trip, riding the chain in order for at least 90 percent of its steps
MIN_SHARED = 4
MIN_SHARED_RATIO = 0.3
MIN_MONOTONY = 0.9
# a route whose two canonical chains mostly share stops in the same order is
# circular: both rotations serve the same platforms and order proves nothing
CIRCULAR_SHARED_RATIO = 0.5
CIRCULAR_MONOTONY = 0.8


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


def plan_repairs(patterns_by_dir):
    """Trips to move to the opposite direction, for one route.

    patterns_by_dir: {direction: {stop_id_tuple: [trip_id, ...]}} with
    exactly two directions. Returns {trip_id: new_direction}, empty when the
    route is healthy or circular.
    """
    if len(patterns_by_dir) != 2:
        return {}
    (dir_a, pat_a), (dir_b, pat_b) = sorted(
        patterns_by_dir.items(), key=lambda kv: str(kv[0])
    )
    chain_a = _canonical(pat_a)
    chain_b = _canonical(pat_b)
    pos_a = {sid: i for i, sid in enumerate(chain_a)}
    pos_b = {sid: i for i, sid in enumerate(chain_b)}

    shared = [s for s in chain_a if s in pos_b]
    if len(shared) > CIRCULAR_SHARED_RATIO * len(chain_a):
        _, monotony = _fit(shared, pos_b)
        if monotony > CIRCULAR_MONOTONY:
            return {}

    flips = {}
    for own_dir, opp_dir, own_pos, opp_pos, patterns in (
        (dir_a, dir_b, pos_a, pos_b, pat_a),
        (dir_b, dir_a, pos_b, pos_a, pat_b),
    ):
        for pattern, trip_ids in patterns.items():
            if _fits(pattern, opp_pos) and not _fits(pattern, own_pos):
                for trip_id in trip_ids:
                    flips[trip_id] = opp_dir
    return flips


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
        pattern = tuple(sid for _, sid in current_stops)
        patterns[route_id][direction][pattern].append(current_trip)

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
        route_flips = plan_repairs(patterns[route_id])
        if route_flips:
            total = sum(
                len(t) for d in patterns[route_id].values() for t in d.values()
            )
            _LOGGER.info(
                "Direction repair: route %s: %s of %s trips ride the opposite"
                " direction's stop order, rewriting their direction_id",
                route_labels.get(route_id) or route_id,
                len(route_flips),
                total,
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
