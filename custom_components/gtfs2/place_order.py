"""The order a line's places are ridden in, which the lists of places.py
follow: which records make one place (_places_of), one order of places for
the whole line, both ways round, branches and loops included (_chain_of),
and the end it starts from (_heading_of).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .stop_rules import PLACE_LAT, PLACE_LON


# the sampled trips of a line, each with its calls in riding order:
# {trip_id: [(stop_id, stop_sequence)]}; a place is the stop_id of the
# record that names it (_places_of)
type _Trips = dict[str, list[tuple[str, int]]]


def _same_place(a: Sequence[Any], b: Sequence[Any]) -> bool:
    """The rule of _place_group, on (name, parent, lat, lon) tuples."""
    if a[1] or b[1]:
        return bool(a[1]) and a[1] == b[1]
    try:
        return (a[0] == b[0]
                and abs(float(a[2]) - float(b[2])) <= PLACE_LAT
                and abs(float(a[3]) - float(b[3])) <= PLACE_LON)
    except (TypeError, ValueError):
        return False


def _trips_of(rows: Iterable[Sequence[Any]]) -> tuple[_Trips, dict[str, tuple[Any, ...]]]:
    """{trip_id: [(stop_id, stop_sequence)]} and {stop_id: (name, parent,
    lat, lon, station_name)} out of _STOP_ROWS shaped rows."""
    trips: _Trips = {}
    info: dict[str, tuple[Any, ...]] = {}
    for trip_id, stop_id, stop_name, stop_sequence, parent_station, station_name, lat, lon in rows:
        trips.setdefault(trip_id, []).append((stop_id, stop_sequence))
        info[stop_id] = (stop_name, parent_station or "", lat, lon, station_name)
    return trips, info


def _box_distance(a: Sequence[Any], b: Sequence[Any]) -> float:
    """How far apart two (name, parent, lat, lon) records are, in boxes:
    below 1 within PLACE_LAT / PLACE_LON."""
    try:
        return max(abs(float(a[2]) - float(b[2])) / PLACE_LAT,
                   abs(float(a[3]) - float(b[3])) / PLACE_LON)
    except (TypeError, ValueError):
        return 0


def _places_of(trips: _Trips, info: Mapping[str, Sequence[Any]]) -> dict[str, str]:
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
    seeds: list[str] = []
    calls: dict[str, bool] = {}
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


def _segments_of(places: Iterable[str]) -> list[list[str]]:
    """A trip read as places, cut where it comes back to a place it already
    passed: the next piece starts from the last place, so pieces stay tied.
    A racket (Palm Bus 21 out and back through Gare SNCF) or a loop (TAO 22,
    Zenith to Zenith) gives two pieces, each passing a place once."""
    pieces: list[list[str]]
    current: list[str]
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


def _runs_forward(shared: Sequence[int]) -> bool:
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


def _lay_piece(order: list[str], piece: Sequence[str]) -> bool:
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


def _chain_of(trips: _Trips, place: Mapping[str, str]) -> list[str]:
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
    pieces: dict[tuple[str, ...], int] = {}
    piece: Sequence[str]
    for _trip_id, trip_stops in trips.items():
        for piece in _segments_of([place[s] for s, _seq in trip_stops]):
            pieces.setdefault(tuple(piece), 0)
            pieces[tuple(piece)] += 1
    pending = sorted(pieces, key=lambda p: (-len(p), -pieces[p], p))
    order: list[str] = []
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


def _ridden_next_to(order: Sequence[str], pieces: Mapping[tuple[str, ...], int],
                    ) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """({place: {place ridden just before it: weight}}, the same for just
    after), each piece read the way it runs the chain (_runs_forward) and
    weighed by how many patterns ride it."""
    position = {p: i for i, p in enumerate(order)}
    before: dict[str, dict[str, int]] = {p: {} for p in order}
    after: dict[str, dict[str, int]] = {p: {} for p in order}
    for piece, count in pieces.items():
        if not _runs_forward([position[p] for p in piece]):
            piece = piece[::-1]
        for a, b in zip(piece, piece[1:]):
            before[b][a] = before[b].get(a, 0) + count
            after[a][b] = after[a].get(b, 0) + count
    return before, after


def _slot_costs(rest: Iterable[str], before: Mapping[str, int], after: Mapping[str, int]) -> list[int]:
    """For each slot of a place among the others (rest), how much the
    steps ridden through it go against the chain: the places ridden
    before it set after the slot, those ridden after it set before."""
    cost = sum(before.values())
    costs = [cost]
    for q in rest:
        cost += after.get(q, 0) - before.get(q, 0)
        costs.append(cost)
    return costs


def _settle(order: list[str], pieces: Mapping[tuple[str, ...], int]) -> list[str]:
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


def _heading_of(order: Sequence[str], place: Mapping[str, str], heading: Iterable[Sequence[Any]]) -> bool:
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


def _ride_of(rows: Iterable[Sequence[Any]], heading: Iterable[Sequence[Any]] = (),
             ) -> tuple[list[list[Any]], dict[str, str | None], dict[str, str], _Trips]:
    """One entry per place, in riding order, out of _STOP_ROWS shaped rows.

    Returns the kept [stop_id, name, sequence], stop_id being the record that
    names the place, the station names by stop_id, which the labels read,
    the {stop_id: place} the entries were drawn from, and the trips read
    out of the rows. heading, the
    _HEADING_ROWS of the line, says which end comes first.
    """
    trips, info = _trips_of(rows)
    place = _places_of(trips, info)
    order = _chain_of(trips, place)
    if _heading_of(order, place, heading):
        order.reverse()
    first_seq: dict[str, int] = {}
    for _trip_id, trip_stops in sorted(trips.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        for stop_id, seq in trip_stops:
            first_seq.setdefault(stop_id, seq)
    kept = [[p, info[p][0], first_seq[p]] for p in order]
    station_names = {stop_id: values[4] for stop_id, values in info.items()}
    return kept, station_names, place, trips
