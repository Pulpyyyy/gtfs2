"""The order get_destination_stop_list offers the places reached from an
origin in: the rides after the origin (_rides_after), what they say of each
place (_riding_order, _tails_of), and the places in the order the list
offers them (_placed_in_order).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _groups_of(before: Mapping[str, Iterable[str]]) -> dict[str, str]:
    """{place: group}, the places that come before one another, directly or
    round a cycle, in one group (a strongly connected component of the
    "comes after" relation, Tarjan's walk without recursion)."""
    group: dict[str, str]
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


def _onward_of(before: Mapping[str, Iterable[str]]) -> dict[str, set[str]]:
    """{place: the place and every place some ride reaches after it}."""
    after: dict[str, set[str]] = {p: set() for p in before}
    for p, earlier in before.items():
        for q in earlier:
            after.setdefault(q, set()).add(p)
    onward: dict[str, set[str]] = {}
    for start in before:
        seen, todo = {start}, [start]
        while todo:
            for q in after.get(todo.pop(), ()):
                if q not in seen:
                    seen.add(q)
                    todo.append(q)
        onward[start] = seen
    return onward


def _close_group(root: str, stack: list[str], on_stack: set[str], group: dict[str, str]) -> None:
    """Take a finished group off _groups_of's stack, named after its root."""
    while True:
        member = stack.pop()
        on_stack.discard(member)
        group[member] = root
        if member == root:
            return


def _rides_after(calls: Sequence[Sequence[Any]],
                 ) -> tuple[dict[str, tuple[tuple[int, str], ...]], dict[str, int], set[tuple[str, str]]]:
    """The rides after the origin, out of every call of every trip through it
    (trip_id, stop_sequence, stop_id, whether riders get off there), in trip
    then sequence order.

    Trips calling at the same stops in the same order are one ride, which
    the smallest trip_id stands for, as _STOP_ROWS samples. Returns the
    sampled trips with their calls, {sampled trip: how many trips its ride
    stands for}, and every (sampled trip, stop) some trip of the ride sets
    riders down at.
    """
    by_trip: dict[str, list[tuple[int, str]]] = {}
    for trip_id, sequence, stop_id, _alights_there in calls:
        by_trip.setdefault(trip_id, []).append((sequence, stop_id))
    ride_of = {trip_id: tuple(ride) for trip_id, ride in by_trip.items()}
    sample_of: dict[tuple[tuple[int, str], ...], str]
    count: dict[tuple[tuple[int, str], ...], int]
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


def _riding_order(calls: Iterable[tuple[Sequence[str], str]], trip_count: Mapping[str, int],
                  ) -> tuple[dict[str, int], dict[str, set[str]], dict[str, int]]:
    """What the rides from the origin say of each place: {place: how soon a
    ride reaches it}, {place: the places a ride calls at just before it},
    {place: how many trips ride through it}."""
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
    reach: dict[str, int]
    before: dict[str, set[str]]
    weight: dict[str, int]
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
    return reach, before, weight


def _tails_of(calls: Iterable[tuple[Sequence[str], str]]) -> dict[str, list[frozenset[str]]]:
    """{place: each way on a ride goes from it, as the place and the places
    the ride reaches after it}. A ride ending short on another's way is not
    a way of its own (gtfs-nl 152922 turns trips at Rotterdam Centraal)."""
    tails: dict[str, set[frozenset[str]]] = {}
    for ride, _trip_id in calls:
        for n, p in enumerate(ride):
            if p not in ride[:n]:
                tails.setdefault(p, set()).add(frozenset(ride[n:]))
    return {p: [tail for tail in ways if not any(tail < other for other in ways)]
            for p, ways in tails.items()}


def _meets_a_split_side(place: str, pool: Iterable[str], onward: Mapping[str, set[str]],
                        tails: Mapping[str, Iterable[frozenset[str]]]) -> bool:
    """Whether what a free place leads to is reached from another free
    place too, neither lying on the other's way, which a ride also leaves
    for somewhere else."""
    return any(other != place and other not in onward[place] and place not in onward[other]
               and onward[place] & onward[other]
               and any(not tail & onward[place] for tail in tails[other])
               for other in pool)


def _placed_in_order(reach: Mapping[str, int], before: Mapping[str, set[str]],
                     weight: Mapping[str, int], position: Mapping[str, int],
                     tails: Mapping[str, Sequence[frozenset[str]]]) -> list[str]:
    """The places the rides reach, in the order the list offers them."""
    # places that come before one another, two variants riding them in
    # opposite orders, form one group: it is free once what comes before it
    # from outside is placed. Waiting for each other, they came last, after
    # the terminus (TEC B0026 listed Noduwez after Jodoigne; the 48-feed
    # sweep). Without such a cycle every place is a group of its own
    group = _groups_of(before)
    onward = _onward_of(before)
    order: list[str]
    rank: dict[str, int]
    joined: dict[str, int]
    waiting: dict[str, int]
    order, placed, rank, joined, waiting = [], set(), {}, {}, {}
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
        # two (GtfsDe 22884, Zagreb 14). Between two places hanging off it
        # alike, the one going on to a place a ride listed further back
        # waits at, then a side of its own before one that meets another
        # side later, when a ride of that other side goes elsewhere too:
        # left last, the ride it carries on runs into the side it meets.
        # Zou school 9200 from Pourtoules listed the Conil side, which meets
        # the Caristie side at Louis Pasteur, Caristie also sending a run to
        # Lycée de l'Arc, before the Les Sables side, and from Caristie went
        # to Lycée de l'Arc before Louis Pasteur, which the Pont de la Gare
        # run waited at. A side that meets one wholly heading there comes
        # first, the busiest, the one joining it after (GtfsDe 22884)
        p = min(pool, key=lambda q: (-max((joined.get(x, -1) for x in onward[q]), default=-1),
                                     -max((rank[x] for x in before[q] if x in rank), default=-1),
                                     -max((waiting.get(x, -1) for x in onward[q] if x != q and group[x] in blocked),
                                          default=-1),
                                     _meets_a_split_side(q, pool, onward, tails),
                                     -weight[q], reach[q], position.get(q, 0)))
        rank[p] = len(order)
        for x in onward[p]:
            joined[x] = rank[p]
        for x in reach:
            if p in before[x]:
                waiting[x] = rank[p]
        order.append(p)
        placed.add(p)
    return order
