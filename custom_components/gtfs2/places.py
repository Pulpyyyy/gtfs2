"""The places of a line, as the config flow offers them: the stops a rider
can start from (get_stop_list), which way they go from there (get_towards),
where they can get to (get_destination_stop_list), and the direction an
entry keeps for its pair (get_pair_direction, get_direction_labels).

A place is a stop, or the stops of one station taken together; the list
follows the order a trip calls at them, branches and loops included.
"""
from __future__ import annotations

import json
import logging
import os
import statistics

from sqlalchemy.sql import text

from .gtfs_helper import PLACE_LAT, PLACE_LON, _alights, _boards, _place_group, gtfs_seconds

_LOGGER = logging.getLogger(__name__)


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
_STOP_ROWS = """
    with ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join stop_times st on st.trip_id = t.trip_id
        where t.route_id = :route_id
        and (:direction is null or t.direction_id = :direction or t.direction_id is null)
        group by t.trip_id
    ), sample as (
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


def _call_type(value):
    """A pickup_type / drop_off_type as the feed meant it: 0 when blank."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# the records of a line where some trip takes riders on (BOARDING) or sets
# them down (ALIGHTING): the lists offer a place when one of its records is
_BOARDING_ROWS = f"""
    select distinct st.stop_id
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    where t.route_id = :route_id
    and (:direction is null or t.direction_id = :direction or t.direction_id is null)
    and {_boards("st")}
"""

_ALIGHTING_ROWS = f"""
    select distinct st.stop_id
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    where t.route_id = :route_id
    and (:direction is null or t.direction_id = :direction or t.direction_id is null)
    and {_alights("st")}
"""


def _line_ways(conn, route_id, direction=None):
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

    def ways(sql):
        records = {row[0] for row in conn.execute(text(sql), params)}
        places = {place[s] for s in records if s in place}
        return lambda stop_id: stop_id in records or place.get(stop_id) in places

    return ways(_BOARDING_ROWS), ways(_ALIGHTING_ROWS)


def _same_place(a, b):
    """The rule of _place_group, on (name, parent, lat, lon) tuples."""
    if a[1] or b[1]:
        return bool(a[1]) and a[1] == b[1]
    try:
        return (a[0] == b[0]
                and abs(float(a[2]) - float(b[2])) <= PLACE_LAT
                and abs(float(a[3]) - float(b[3])) <= PLACE_LON)
    except (TypeError, ValueError):
        return False


def _trips_of(rows):
    """{trip_id: [(stop_id, stop_sequence)]} and {stop_id: (name, parent,
    lat, lon, station_name)} out of _STOP_ROWS shaped rows."""
    trips = {}
    info = {}
    for trip_id, stop_id, stop_name, stop_sequence, parent_station, station_name, lat, lon in rows:
        trips.setdefault(trip_id, []).append((stop_id, stop_sequence))
        info[stop_id] = (stop_name, parent_station or "", lat, lon, station_name)
    return trips, info


def _box_distance(a, b):
    """How far apart two (name, parent, lat, lon) records are, in boxes:
    below 1 within PLACE_LAT / PLACE_LON."""
    try:
        return max(abs(float(a[2]) - float(b[2])) / PLACE_LAT,
                   abs(float(a[3]) - float(b[3])) / PLACE_LON)
    except (TypeError, ValueError):
        return 0


def _places_of(trips, info):
    """{stop_id: place}, a place being named by the first of its records the
    line calls at, fullest trip first: that record is what the entry keeps,
    and the one the queries widen to the whole place again.

    A box is measured from its seed only, as the SQL one is from the entry's
    record, so a chain of near records never drifts a place further. Two
    seeds' boxes can still overlap: TAO N has two Liberation-Interives 150 m
    apart, and a third pole within reach of both. Such a record joins the
    nearer seed, whichever the line met first, so the list does not depend
    on the order it reads the trips in.
    """
    seeds = []
    calls = {}
    for _trip_id, trip_stops in sorted(trips.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for stop_id, _seq in trip_stops:
            if stop_id in calls:
                continue
            calls[stop_id] = True
            if not any(_same_place(info[s], info[stop_id]) for s in seeds):
                seeds.append(stop_id)
    place = {}
    for stop_id in calls:
        # near is in seed order, and min keeps the first of equals: on a
        # tie the seed the line met first
        near = [s for s in seeds if _same_place(info[s], info[stop_id])]
        place[stop_id] = min(near, key=lambda s: _box_distance(info[s], info[stop_id]))
    return place


def _segments_of(places):
    """A trip read as places, cut where it comes back to a place it already
    passed: the next piece starts from the last place, so pieces stay tied.
    A racket (Palm Bus 21 out and back through Gare SNCF) or a loop (TAO 22,
    Zenith to Zenith) gives two pieces, each passing a place once."""
    pieces, current = [], []
    for p in places:
        if current and p == current[-1]:
            continue
        if p in current:
            pieces.append(current)
            current = [current[-1], p]
        else:
            current.append(p)
    if len(current) > 1 or not pieces:
        pieces.append(current)
    return pieces


def _runs_forward(shared):
    """Whether a piece runs the chain forward, from the chain positions of
    the places it shares with it, in riding order: most pairs of them in
    the chain's order. Counted over pairs, not steps: a way back riding a
    one-way loop in the outbound sense (Autolinee Toscane 93 round
    Albereto) makes many short steps up the chain after a few long ones
    down it."""
    up = down = 0
    for i, a in enumerate(shared):
        for b in shared[i + 1:]:
            up += b > a
            down += b < a
    return up >= down


def _lay_piece(order, piece):
    """Slot the places of a piece into the chain, each after the place
    preceding it, the piece read forward or backward as the places it
    shares with the chain agree (_runs_forward). False, the chain left
    alone, when a chain already started shares fewer than two places with
    it."""
    position = {p: i for i, p in enumerate(order)}
    shared = [position[p] for p in piece if p in position]
    if not order:
        forward = True
    elif len(shared) < 2:
        return False
    else:
        forward = _runs_forward(shared)
    prev = -1
    for p in (piece if forward else reversed(piece)):
        if p in position:
            prev = position[p]
            continue
        prev += 1
        order.insert(prev, p)
        position = {q: i for i, q in enumerate(order)}
    return True


def _chain_of(trips, place):
    """One order of places for the whole line, both ways round.

    direction_id cannot be trusted to split a line: on GVB tram 1 a third of
    the trips carry the other way's label, and the spec itself keeps it for
    publishing timetables, not for routing. The order of the stops can: the
    fullest piece is laid first, and every other piece is read forward or
    backward, whichever way the places it shares with the chain already
    agree with, then its places are slotted in after the place preceding
    them. A piece sharing nothing yet waits for the chain to grow. Once
    every piece is laid, the places settle (_settle).
    """
    pieces = {}
    for _trip_id, trip_stops in trips.items():
        for piece in _segments_of([place[s] for s, _seq in trip_stops]):
            pieces.setdefault(tuple(piece), 0)
            pieces[tuple(piece)] += 1
    pending = sorted(pieces, key=lambda p: (-len(p), -pieces[p], p))
    order = []
    while pending:
        waiting = [piece for piece in pending if not _lay_piece(order, piece)]
        if len(waiting) == len(pending):
            # nothing left shares two places with the chain: keep them in
            # riding order at the end rather than lose them
            for piece in waiting:
                order.extend(p for p in piece if p not in order)
            break
        pending = waiting
    return _settle(order, pieces)


def _ridden_next_to(order, pieces):
    """({place: {place ridden just before it: weight}}, the same for just
    after), each piece read the way it runs the chain (_runs_forward) and
    weighed by how many patterns ride it."""
    position = {p: i for i, p in enumerate(order)}
    before = {p: {} for p in order}
    after = {p: {} for p in order}
    for piece, count in pieces.items():
        if not _runs_forward([position[p] for p in piece]):
            piece = piece[::-1]
        for a, b in zip(piece, piece[1:]):
            before[b][a] = before[b].get(a, 0) + count
            after[a][b] = after[a].get(b, 0) + count
    return before, after


def _slot_costs(rest, before, after):
    """For each slot of a place among the others (rest), how much the
    steps ridden through it go against the chain: the places ridden
    before it set after the slot, those ridden after it set before."""
    cost = sum(before.values())
    costs = [cost]
    for q in rest:
        cost += after.get(q, 0) - before.get(q, 0)
        costs.append(cost)
    return costs


def _settle(order, pieces):
    """The chain with each place moved, one at a time, to the slot the
    rides through it contradict least, until none moves.

    A piece slots a place it brings after the place preceding it, which is
    a guess when the piece knows nothing of the places the chain already
    holds between its neighbours: on Rome 8 the long outbound ride brings a
    pole of Gianicolense/Colli Portuensi without the two stops the way back
    passes there, and it landed after them, where a shorter ride laid later
    calls at it before them. A place moves only to a slot strictly better,
    the nearest of equals.
    """
    for _round in range(len(order)):
        before, after = _ridden_next_to(order, pieces)
        moved = False
        for p in list(order):
            here = order.index(p)
            rest = order[:here] + order[here + 1:]
            costs = _slot_costs(rest, before[p], after[p])
            slot = min(range(len(costs)), key=lambda i: (costs[i], abs(i - here)))
            if costs[slot] < costs[here]:
                order[:] = rest[:slot] + [p] + rest[slot:]
                moved = True
        if not moved:
            break
    return order


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


def _heading_of(order, place, heading):
    """True when the trips of direction 0, weighed by how many run each
    pattern, ride order backwards more than forwards."""
    position = {p: i for i, p in enumerate(order)}
    up = down = 0
    for stops, count in heading:
        calls = sorted((int(seq), stop_id) for seq, stop_id in
                       (call.split(":", 1) for call in (stops or "").split(",") if ":" in call))
        known = [position[place[s]] for _seq, s in calls if s in place]
        up += count * sum(1 for a, b in zip(known, known[1:]) if b > a)
        down += count * sum(1 for a, b in zip(known, known[1:]) if b < a)
    return down > up


def _ride_of(rows, heading=()):
    """One entry per place, in riding order, out of _STOP_ROWS shaped rows.

    Returns the kept [stop_id, name, sequence], stop_id being the record that
    names the place, the station names by stop_id, which the labels read,
    and the {stop_id: place} the entries were drawn from. heading, the
    _HEADING_ROWS of the line, says which end comes first.
    """
    trips, info = _trips_of(rows)
    place = _places_of(trips, info)
    order = _chain_of(trips, place)
    if _heading_of(order, place, heading):
        order.reverse()
    first_seq = {}
    for _trip_id, trip_stops in sorted(trips.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for stop_id, seq in trip_stops:
            first_seq.setdefault(stop_id, seq)
    kept = [[p, info[p][0], first_seq[p]] for p in order]
    station_names = {stop_id: values[4] for stop_id, values in info.items()}
    return kept, station_names, place


def _labels_of(kept, station_names):
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
    by_name = {}
    for x in kept:
        by_name.setdefault(x[1], []).append(x)
    label = {}
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


def _entries_of(kept, label):
    """The picker's entries, "stop_id: Name (sequence)": get_next_departure
    cuts the id back out of the value, only the name is the user's to read."""
    return [f"{x[0]}: {label.get(x[0], x[1])} ({x[2]})" for x in kept]


def _direction_param(direction):
    """None for no direction (the whole line), else 0 or 1."""
    if direction is None or str(direction) not in ("0", "1"):
        return None
    return int(direction)


# the rows the flow's screens read of a line, per edition of the database:
# the origin, towards, destination and pair screens each read the same
# ones again, every trip of the line grouped each time (TAO tram A, 1.5 s
# a read). The last few only: a flow reads one line at a time
_LINE_ROWS = {}
_LINE_ROWS_KEPT = 8


def _line_rows(conn, sql, params):
    """conn.execute(text(sql), params).fetchall(), kept while the database
    file stays the same one, unchanged; read afresh when it cannot say."""
    try:
        path = conn.engine.url.database
        stat = os.stat(path)
    except (AttributeError, OSError, TypeError, ValueError):
        return conn.execute(text(sql), params).fetchall()
    key = (path, stat.st_ino, stat.st_mtime_ns, stat.st_size, sql, tuple(sorted(params.items())))
    rows = _LINE_ROWS.get(key)
    if rows is None:
        rows = conn.execute(text(sql), params).fetchall()
        while len(_LINE_ROWS) >= _LINE_ROWS_KEPT:
            _LINE_ROWS.pop(next(iter(_LINE_ROWS)), None)
        _LINE_ROWS[key] = rows
    return rows


def _line_of(conn, route_id, direction=None):
    """_ride_of for a route, its sampled trips kept beside: (kept,
    station_names, place, trips)."""
    rows = _line_rows(conn, _STOP_ROWS, {
        "route_id": route_id, "direction": _direction_param(direction)})
    heading = _line_rows(conn, _HEADING_ROWS, {"route_id": route_id})
    kept, station_names, place = _ride_of(rows, heading)
    trips, _info = _trips_of(rows)
    return kept, station_names, place, trips


def _loop_termini(trips, place):
    """The places some trip of the line starts and ends at: a loop's terminus
    (TAO 22 runs Zénith to Zénith both ways round)."""
    return {place.get(stops[0][0]) for stops in trips.values()
            if stops and place.get(stops[0][0]) == place.get(stops[-1][0])}


def _origin_boarding(conn, route_id, origin_stop_id, direction=None):
    """The calls of the route at the origin's place a rider can get on at,
    as {(trip_id, stop_sequence)}: what _calls_out starts a ride from.

    Keyed on the trip _STOP_ROWS samples for each pattern of stops, as the
    rides are read from that trip alone: a call counts when any trip of the
    pattern boards there. Read from the sampled trip's own flag, a pattern
    whose sample only sets down at the origin lost its way out (Amtrak's
    Stockton, where one Thruway bus of three does not pick up)."""
    return {(row[0], row[1]) for row in _line_rows(conn, f"""
        with ride as (
            select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
            from trips t
            inner join stop_times st on st.trip_id = t.trip_id
            where t.route_id = :route_id
            and (:direction is null or t.direction_id = :direction or t.direction_id is null)
            group by t.trip_id
        ), sample as (
            select trip_id, min(trip_id) over (partition by stops) as sample_id from ride
        )
        select distinct sample.sample_id, st.stop_sequence
        from sample
        inner join stop_times st on st.trip_id = sample.trip_id
        where st.stop_id in {_STOP_GROUP}
        and {_boards("st")}""", {"route_id": route_id, "origin": origin_stop_id,  # noqa: S608
                                 "direction": _direction_param(direction)})}


def _calls_out(trips, place, origin_place, boarding=None):
    """(ride, trip_id) for each ride out of the origin place, the ride as
    places: from a call at it to the trip's next call at it, or its end. A
    trip passing the origin twice (Palm Bus 21 out and back through Gare
    Maritime) gives a ride from each.

    boarding, when given, holds the (trip_id, stop_sequence) calls at the
    origin a rider can get on at: a ride from any other call is nobody's
    way out (Zou 620 only sets down at Pont des Gabres on its way into
    Cannes) and is left out, the calls still cutting the rides as before."""
    rides = []
    for trip_id, trip_stops in trips.items():
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


def _ways_of(trips, place, origin_place, boarding=None):
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
    rides = {}
    for ride, trip_id in _calls_out(trips, place, origin_place, boarding):
        end = place.get(trips[trip_id][-1][0])
        told_by_next = end in loop_termini or end == origin_place
        rides.setdefault((end, ride[0] if told_by_next else None), []).append((ride, trip_id))
    folded = {}
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
    ways = {}
    for key, calls in rides.items():
        # two poles of one terminus fold into each other: one key for both
        chain = []
        while key in folded and key not in chain:
            chain.append(key)
            key = folded[key]
        if key in chain:
            key = min(chain[chain.index(key):])
        ways.setdefault("|".join(p for p in key if p), []).extend(calls)
    return ways


def get_towards(schedule, route_id, origin_stop_id):
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
    shown = []
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


def get_stop_list(schedule, route_id, direction=None):
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


def _groups_of(before):
    """{place: group}, the places that come before one another, directly or
    round a cycle, in one group (a strongly connected component of the
    "comes after" relation, Tarjan's walk without recursion)."""
    index, low, group, stack, on_stack = {}, {}, {}, [], set()
    counter = 0
    for root in before:
        if root in index:
            continue
        work = [(root, iter(before[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, following = work[-1]
            nxt = next((q for q in following if q in before), None)
            if nxt is not None:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(before[nxt])))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                _close_group(node, stack, on_stack, group)
    return group


def _onward_of(before):
    """{place: the place and every place some ride reaches after it}."""
    after = {p: set() for p in before}
    for p, earlier in before.items():
        for q in earlier:
            after.setdefault(q, set()).add(p)
    onward = {}
    for start in before:
        seen, todo = {start}, [start]
        while todo:
            for q in after.get(todo.pop(), ()):
                if q not in seen:
                    seen.add(q)
                    todo.append(q)
        onward[start] = seen
    return onward


def _close_group(root, stack, on_stack, group):
    """Take a finished group off _groups_of's stack, named after its root."""
    while True:
        member = stack.pop()
        on_stack.discard(member)
        group[member] = root
        if member == root:
            return


def _rides_after(calls):
    """The rides after the origin, out of every call of every trip through it
    (trip_id, stop_sequence, stop_id, whether riders get off there), in trip
    then sequence order.

    Trips calling at the same stops in the same order are one ride, which
    the smallest trip_id stands for, as _STOP_ROWS samples. Returns the
    sampled trips with their calls, {sampled trip: how many trips its ride
    stands for}, and every (sampled trip, stop) some trip of the ride sets
    riders down at.
    """
    by_trip = {}
    for trip_id, sequence, stop_id, _alights_there in calls:
        by_trip.setdefault(trip_id, []).append((sequence, stop_id))
    ride_of = {trip_id: tuple(ride) for trip_id, ride in by_trip.items()}
    sample_of, count = {}, {}
    for trip_id, ride in ride_of.items():
        if ride not in sample_of or trip_id < sample_of[ride]:
            sample_of[ride] = trip_id
        count[ride] = count.get(ride, 0) + 1
    samples = {sample_of[ride]: ride for ride in sample_of}
    trip_count = {sample_of[ride]: n for ride, n in count.items()}
    alighting = {(sample_of[ride_of[trip_id]], stop_id)
                 for trip_id, _sequence, stop_id, alights_there in calls if alights_there}
    return samples, trip_count, alighting


def _sample_rows(conn, samples):
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


def get_destination_stop_list(schedule, route_id, direction, origin_stop_id, towards=None):
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
    calls = _calls_out({t: [(origin_stop_id, None)] + s for t, s in trips.items()},
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
    # Riding order first: a place comes after every place some trip calls at
    # just before it on its way from the origin, so two branches that meet
    # again (GVB 1 reaches Leidseplein by Overtoom or by Jan Pieter
    # Heijestraat) keep each ride's order. A later call at the origin starts
    # the ride again (Palm Bus 21 passes Gare SNCF out and back). A place met
    # again on the same ride orders nothing, and what the ride meets next
    # comes after the last place it met for the first time: a spur ridden
    # out and back (Krakow 141 turns off at Rzepakowa for Ruszcza and comes
    # back through it) sits where the ride serves it, not after the line.
    # Where the rides leave the order open, the branch in progress is
    # finished before another starts, so the stops of one street stay
    # together: interleaving them by distance read as no bus runs (Zou 653
    # put RD du 24 Août inside the Plascassier village loop, which is the
    # other variant). The busiest branch comes first, by the trips it
    # carries, then the nearest.
    reach, before, weight = {}, {}, {}
    for ride, trip_id in calls:
        count, newest, met = 0, None, set()
        for p in ride:
            count += 1
            reach[p] = min(reach.get(p, count), count)
            before.setdefault(p, set())
            if p in met:
                continue
            if newest is not None:
                before[p].add(newest)
            met.add(p)
            newest = p
        for p in set(ride):
            weight[p] = weight.get(p, 0) + trip_count.get(trip_id, 1)

    # places that come before one another, two variants riding them in
    # opposite orders, form one group: it is free once what comes before it
    # from outside is placed. Waiting for each other, they came last, after
    # the terminus (TEC B0026 listed Noduwez after Jodoigne; the 48-feed
    # sweep). Without such a cycle every place is a group of its own
    group = _groups_of(before)
    onward = _onward_of(before)
    order, placed, rank, joined = [], set(), {}, {}
    while len(order) < len(reach):
        blocked = {group[p] for p in reach if p not in placed
                   for q in before[p] if q not in placed and group[q] != group[p]}
        ready = [p for p in reach if p not in placed and group[p] not in blocked]
        # nothing free: a loop's rotations order each other round
        pool = ready or [p for p in reach if p not in placed]
        # what goes on to where the latest place listed is headed comes
        # first: the branch in progress, a branch that joins it, then what
        # branches off it further back, before another side of the line
        # starts. Taking the busiest free place instead left a side's last
        # pole, the other quay of a terminus, after the whole other way (TAO
        # 40 listed Chèques Postaux quai C after the Gare d'Orléans end; the
        # 48-feed sweep); and a branch waiting for another to join it was
        # left for a third (Rome 404 from Fabriano listed Fabriano/Pergola,
        # then the Urbania branch, then Corridonia, which joins Pergola's at
        # Casale S. Basilio). Next, what hangs off the latest place listed:
        # ahead of the branch that joins, it cut the side in progress in
        # two (GtfsDe 22884, Zagreb 14)
        p = min(pool, key=lambda q: (-max((joined.get(x, -1) for x in onward[q]), default=-1),
                                     -max((rank[x] for x in before[q] if x in rank), default=-1),
                                     -weight[q], reach[q], position.get(q, 0)))
        rank[p] = len(order)
        for x in onward[p]:
            joined[x] = rank[p]
        order.append(p)
        placed.add(p)
    kept = [by_place[p] for p in order if p in by_place and p in alightable]
    stops = _entries_of(kept, _labels_of(line, station_names))
    _LOGGER.debug(f"Destinations from {origin_stop_id}: {stops}")
    return stops


def _shortest_ride(seq, origin, destination):
    """(where the ride boards, where it alights) of the trip's shortest ride
    from origin to destination, seq being its places in call order; None
    when it rides none."""
    best = None
    last_origin = None
    for i, p in enumerate(seq):
        if p == origin:
            last_origin = i
        elif p == destination and last_origin is not None:
            if best is None or i - last_origin < best[1] - best[0]:
                best = (last_origin, i)
            last_origin = None
    return best


def get_pair_direction(schedule, route_id, origin_stop_id, destination_stop_id, towards=None):
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
            direction = told.pop()
            _LOGGER.debug("Pair %s -> %s on %s ridden %s, keeping direction %s",
                          origin_stop_id, destination_stop_id, route_id, towards, direction)
            return direction
    rides = []
    for trip_id, trip_stops in trips.items():
        best = _shortest_ride([place[s] for s, _ in trip_stops], origin, destination)
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


def _quickest_rotations(schedule, route_id, origin_stop_id, destination_stop_id, candidates):
    """Of the direction labels in candidates, the one whose shortest rides of
    the pair take the least time, by the median over its trips; all of them
    when that does not tell them apart."""
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
      and not exists (
          select 1 from stop_times between_stop
          where between_stop.trip_id = t.trip_id
            and between_stop.stop_sequence > o.stop_sequence
            and between_stop.stop_sequence < d.stop_sequence
            and (between_stop.stop_id in {origin_group}
                 or between_stop.stop_id in {destination_group}))
    """  # noqa: S608
    minutes = {}
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
    return {min(medians, key=medians.get)}


def get_direction_labels(schedule, route_id):
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
    stops = {}
    for direction, name, _seq in rows:
        stops.setdefault(str(direction), []).append(name)
    labels = {}
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


def has_trip_between(schedule, route_id, origin_id, destination_id, direction=None):
    """Whether any trip of a route calls at both stops, in this order.

    This asks whether the journey exists at all, not whether a bus is due:
    a sensor set up in the evening, or on a day the line does not run, is
    still a valid sensor. Times are the coordinator's business. The stop
    pair usually implies the direction, except on a circular line where
    both rotations run it in the same order: pass direction to tell them
    apart, trips without a direction_id still matching.
    """
    direction_where = ""
    params = {
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
