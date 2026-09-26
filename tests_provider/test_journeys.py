"""Every departure/arrival pair a fixture's routes offer must hold up.

For each route named in a fixture's manifest, the trips of each direction
it runs are sampled, and the promises are checked against the real code, on
a db built from the fixture zip, with the clock pinned to a day the trips
actually run. The list and the queries are asked the way the flow asks them:
one list per line, both ways round, a place per entry, and no direction but
the rotation get_pair_direction keeps at a loop's terminus.

    stop_list   each place once (the records of a parent station, or of one
                name close together), every stop a trip calls at stood for,
                and every trip riding the list one way or the other
    destinations  from an origin, get_destination_stop_list offers every
                place a trip rides to after it, once, in the order every
                ride makes, one branch at a time (busiest first) where the
                rides leave it open, and
                nothing no trip through that origin reaches
    next_service  get_next_service_date, asked on a day a pair runs, answers
                that day; asked the day after, the next day the feed holds
                within its horizon, or nothing
    towards     from an origin, a way is asked exactly when its trips go
                to different termini (the next stop telling them apart at a
                loop's terminus, or when the terminus is the origin);
                each way's destinations hold every place its rides reach and
                nothing else, in their order, and the two ways together are
                the whole destination list
    pairs       origin before destination on some trip: get_next_departure
                answers it, on the right places, in riding order, arriving no
                earlier than it departs, on the shortest ride of its trip;
                and the departure it shows, and the first it lists, is the
                next ride the feed has, asked at 00:05 and at 12:00, read
                call by call from stop_times and the calendar
    swapped     destination before origin: nothing, or a ride the line really
                makes, either way round

Trains (route_type 2) ride their own path in get_next_departure, matched by
stop name with no direction: for them the pairs are checked by name, the
answer must stay on the asked line, and a swapped pair is legitimate, it is
the return journey, so only `pairs` and `next_service` are checked, by name.
Their arrival screen offers stations, not stops:

    destinations  from a station, get_train_destination_list offers every
                station a trip of the line calls at after it, with the modes
                it is reached by (a coach stop is a coach), and nothing else

A train entry created while the station screen took several stations at one
end (the station and the coach station its replacement coaches leave from)
still reads them all:

    stations    two stations ticked answer every departure each one gives
                alone, and each departure's route type is a replacement bus
                (714) exactly when it leaves from a coach stop

One test per fixture, route, direction and promise; its message lists every
pair that broke the promise. The promises are about what the sensors say,
not how the code says it, so they survive a rewrite of the query.

    pytest tests_provider/
    pytest tests_provider/ -k "palmbus and 21"

This tree is separate from tests/ on purpose: it needs pygtfs, sqlalchemy and
the protobuf bindings (tests_provider/requirements.txt), and its cases come
from real feeds, cut down under tests_provider/fixtures. tests/ha_stub.py is
shared, nothing else is.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import types
import zipfile
import zoneinfo
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import bindparam
from sqlalchemy.sql import text

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

import fixture_db  # noqa: E402

# Loaded on its own rather than through the package, whose __init__ pulls in
# the platforms and with them the rest of Home Assistant.
gtfs_helper = ha_stub.load("gtfs_helper")
# the station queries have a module of their own here; a checkout run with
# --component that keeps them in gtfs_helper is read there, and a function
# it lacks fails the cases that need it
stations = (ha_stub.load("stations") if (ha_stub.COMPONENT / "stations.py").is_file()
            else gtfs_helper)


def _function(module, name):
    """The checkout's function, or, for a checkout run with --component that
    lacks it, one that fails the cases calling it and only those."""
    found = getattr(module, name, None)
    if found is not None:
        return found

    def missing(*_args, **_kwargs):
        raise AssertionError(f"this checkout has no {name}")
    return missing


get_next_departure = _function(gtfs_helper, "get_next_departure")
get_stop_list = _function(gtfs_helper, "get_stop_list")
get_destination_stop_list = _function(gtfs_helper, "get_destination_stop_list")
get_next_service_date = _function(gtfs_helper, "get_next_service_date")

FIXTURES = Path(__file__).parent / "fixtures"
KINDS = ("stop_list", "destinations", "towards", "next_service", "pairs",
         "swapped", "midnight")
TRAIN_KINDS = ("pairs", "next_service", "destinations", "stations")


class Fixture:
    """One fixture directory: its manifest, its db, and what the checks read
    from that db over and over."""

    def __init__(self, path: Path) -> None:
        self.name = path.name
        self.path = path
        self.manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.schedule = fixture_db.build(str(path))
        self.repaired = _repair_directions(self.schedule)
        with self.schedule.engine.connect() as conn:
            agency_tz = conn.execute(text(
                "SELECT agency_timezone FROM agency "
                "WHERE agency_timezone IS NOT NULL")).fetchone()
            self.route_types = dict(conn.execute(
                text("SELECT route_id, route_type FROM routes")).fetchall())
            self.route_short_names = dict(conn.execute(
                text("SELECT route_id, route_short_name FROM routes")).fetchall())
            self.stop_names = dict(conn.execute(
                text("SELECT stop_id, stop_name FROM stops")).fetchall())
            self.stations = dict(conn.execute(
                text("SELECT stop_id, parent_station FROM stops")).fetchall())
            # the calls the rider can make: (trip, stop) with a way on, and
            # with a way off. stop_times flags each call, pickup_type and
            # drop_off_type, 1 being none; a trip calling twice at a stop
            # counts the call that lets the rider through
            self.ways_on, self.ways_off = set(), set()
            # and the calls the feed times: GTFS lets a call off the
            # timepoints go untimed (Clemson leaves three in four so), and
            # a departure is only ever listed between timed calls
            self.timed = set()
            for trip_id, stop_id, pickup, drop_off, arrival, departure in conn.execute(text(
                    "SELECT trip_id, stop_id, pickup_type, drop_off_type, "
                    "arrival_time, departure_time FROM stop_times")):
                if _flag(pickup) != 1:
                    self.ways_on.add((trip_id, stop_id))
                if _flag(drop_off) != 1:
                    self.ways_off.add((trip_id, stop_id))
                if arrival is not None and departure is not None:
                    self.timed.add((trip_id, stop_id))
        # get_next_departure compares a departure against "now" in the
        # agency's zone (it overrides the Home Assistant one as soon as the
        # row carries it), so the clock is pinned in that zone: 00:05 UTC is
        # 02:05 in Paris, past a night line's 00:30 departure
        self.agency_tz = agency_tz[0] if agency_tz else "UTC"
        self._places = {}

    def hass(self):
        """What get_next_departure reads off hass, and nothing more."""
        return types.SimpleNamespace(config=types.SimpleNamespace(
            path=lambda *parts: str(self.path.joinpath(*parts)),
            time_zone=self.agency_tz))

    def instant_on(self, date_iso: str,
                   at: datetime.time = datetime.time(0, 5)) -> datetime.datetime:
        """A wall time of the agency's zone on that day, 00:05 by default."""
        return datetime.datetime.combine(
            datetime.date.fromisoformat(date_iso), at,
            zoneinfo.ZoneInfo(self.agency_tz))

    def station_of(self, stop_id):
        """The parent station the feed declares for a stop, if any."""
        return self.stations.get(stop_id) or None

    def boards(self, trip_ids, stop_id):
        """Some of these trips takes riders on at the stop."""
        return any((t, stop_id) in self.ways_on for t in trip_ids)

    def alights(self, trip_ids, stop_id):
        """Some of these trips sets riders down at the stop."""
        return any((t, stop_id) in self.ways_off for t in trip_ids)

    def times(self, trip_ids, stop_id):
        """Some of these trips gives the stop a time."""
        return any((t, stop_id) in self.timed for t in trip_ids)

    def rider_ends(self, pattern, trip_ids):
        """(o, d): the first call a rider can get on at and the last one
        they can get off at, both timed, or None. A turnback or a relief
        point closing the pattern (Brisbane's G:link: no way on, no way off)
        is no journey anyone asks."""
        ons = [i for i, s in enumerate(pattern)
               if self.boards(trip_ids, s) and self.times(trip_ids, s)]
        offs = [i for i, s in enumerate(pattern)
                if self.alights(trip_ids, s) and self.times(trip_ids, s)]
        if not ons or not offs or ons[0] >= offs[-1]:
            return None
        return ons[0], offs[-1]

    def next_ride(self, route_id, direction, origins, destinations, now):
        """(instant, trip_id) of the first ride of the line leaving one of
        the origin's records at or after now for one of the destination's,
        or None. Read call by call from stop_times and the calendar tables,
        the component's query set aside: a trip counts from its last timed
        call with a way on at the origin before a timed call with a way off
        at the destination (the shortest ride on it), leaving after now: a
        ride leaving this very minute is gone. The sensor lists the next
        departures whatever day they fall on, so the service days are read
        from yesterday's (a time past 24:00) on, until a day starts after
        the best ride found or the calendar ends. `direction` None takes
        either way round, as the pair alone decides then."""
        if not hasattr(self, "_calls"):
            self._calls, self._trips = {}, {}
            with self.schedule.engine.connect() as conn:
                for trip_id, route, way, service in conn.execute(text(
                        "SELECT trip_id, route_id, direction_id, service_id FROM trips")):
                    self._trips[trip_id] = (route, way, service)
                for trip_id, seq, stop_id, arrival, departure in conn.execute(text(
                        "SELECT trip_id, stop_sequence, stop_id, arrival_time, "
                        "departure_time FROM stop_times ORDER BY trip_id, stop_sequence")):
                    self._calls.setdefault(trip_id, []).append(
                        (stop_id, arrival, departure))
            self._running = {}
            with self.schedule.engine.connect() as conn:
                ends = [str(row[0])[:10] for row in conn.execute(text(
                    "SELECT max(end_date) FROM calendar UNION ALL "
                    "SELECT max(date) FROM calendar_dates")) if row[0]]
            self._last_day = datetime.date.fromisoformat(max(ends)) if ends else None
        zone = zoneinfo.ZoneInfo(self.agency_tz)
        rides = []  # (service_id, seconds from the service day's midnight, trip)
        for trip_id, (route, way, service) in self._trips.items():
            if route != route_id or (direction is not None and str(way) != str(direction)):
                continue
            leave = None
            for stop_id, arrival, departure in self._calls.get(trip_id, ()):
                if (stop_id in destinations and leave is not None and arrival is not None
                        and (trip_id, stop_id) in self.ways_off):
                    rides.append((service, gtfs_seconds(leave), trip_id))
                    break
                if (stop_id in origins and departure is not None
                        and (trip_id, stop_id) in self.ways_on):
                    leave = departure
        best = None
        day = now.astimezone(zone).date() - datetime.timedelta(days=1)
        while rides and self._last_day and day <= self._last_day:
            midnight = datetime.datetime.combine(day, datetime.time()).replace(tzinfo=zone)
            # compared as instants: two datetimes of one zone compare by
            # their wall clocks, which an hour lived twice makes lie
            if best is not None and midnight.timestamp() > best[0].timestamp():
                break
            iso = day.isoformat()
            if iso not in self._running:
                self._running[iso] = services_on(self.schedule, iso)
            for service, seconds, trip_id in rides:
                if service not in self._running[iso]:
                    continue
                at = (datetime.datetime.combine(day, datetime.time())
                      + datetime.timedelta(seconds=seconds)).replace(tzinfo=zone)
                if at.timestamp() > now.timestamp() and (
                        best is None or at.timestamp() < best[0].timestamp()):
                    best = (at, trip_id)
            day += datetime.timedelta(days=1)
        return best

    def siblings_of(self, stop_id):
        """Every record of the place this one belongs to.

        A place is what a rider waits at: the records a feed groups under a
        parent station, and without a parent, the records of one name close
        together. The journey's ends are matched on whole places, so
        get_next_departure may answer on any of their records: a pair is
        right when it lands on one of these. The checkout's own rule is read
        (_place_group), a parent-only reading when it has none.
        """
        if stop_id in self._places:
            return self._places[stop_id]
        group = getattr(gtfs_helper, "_place_group", None)
        if group:
            with self.schedule.engine.connect() as conn:
                found = {stop_id} | {row[0] for row in conn.execute(
                    text("SELECT stop_id FROM stops WHERE stop_id IN " + group("s")),
                    {"s": stop_id})}
        else:
            parent = self.station_of(stop_id)
            found = {stop_id} | ({s for s, p in self.stations.items() if p == parent}
                                 if parent else set())
        self._places[stop_id] = found
        return found

    def box_distance(self, a, b):
        """How far apart two records are, in the checkout's place boxes (0
        when it has none): of two places claiming a record, the nearer one
        has it."""
        lat_box = getattr(gtfs_helper, "PLACE_LAT", None)
        lon_box = getattr(gtfs_helper, "PLACE_LON", None)
        if not lat_box or not lon_box:
            return 0
        if not hasattr(self, "_where"):
            with self.schedule.engine.connect() as conn:
                self._where = {row[0]: (row[1], row[2]) for row in conn.execute(
                    text("SELECT stop_id, stop_lat, stop_lon FROM stops"))}
        try:
            (a_lat, a_lon), (b_lat, b_lon) = self._where[a], self._where[b]
            return max(abs(float(a_lat) - float(b_lat)) / lat_box,
                       abs(float(a_lon) - float(b_lon)) / lon_box)
        except (KeyError, TypeError, ValueError):
            return 0


def _repair_directions(schedule):
    """Run the checkout's direction repair on the db, when it has one.

    A checkout that repairs direction_ids at import time serves its sensors
    the repaired trips, so the promises are checked on those; a checkout
    without one is checked on what the feed published. Returns how many trips
    moved, or None when there is nothing to run.
    """
    if not (ha_stub.COMPONENT / "direction_repair.py").is_file():
        return None
    return ha_stub.load("direction_repair").repair_trip_directions(schedule)


def _flag(value):
    """A pickup_type / drop_off_type as the feed meant it: 0 when blank."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# the rule the queries hold to, for the expected side: a call with a way
# on at the origin, a call with a way off at the destination
_WAY_ON = "coalesce(cast(o.pickup_type as integer), 0) <> 1"
_WAY_OFF = "coalesce(cast(x.drop_off_type as integer), 0) <> 1"


def line_entries(schedule, route_id):
    """Every place of the line, one entry each, in riding order: what the
    destination screen draws its entries from. The origin list is the same
    less the places nobody gets on at, so the two are told apart here."""
    line_of = getattr(gtfs_helper, "_line_of", None)
    if not line_of:
        return get_stop_list(schedule, route_id, None)
    with schedule.engine.connect() as conn:
        kept, station_names, _place, _trips = line_of(conn, route_id, None)
    return gtfs_helper._entries_of(kept, gtfs_helper._labels_of(kept, station_names))


def line_places(fx, route_id):
    """(entries, ids, entry_of) of a line, as the flow offers them.

    entries is every place of the line, the universe the answers are read
    against (the origin list is these less the places with no way on), ids
    their stop ids, and entry_of the position of the entry that stands for
    each record: the one of its place, the nearer one when two places reach
    it (TAO N's Liberation-Interives), whichever end the list starts from.
    """
    entries = line_entries(fx.schedule, route_id)
    ids = [entry.split(": ", 1)[0] for entry in entries]
    claims = {}
    for n, stop_id in enumerate(ids):
        for member in fx.siblings_of(stop_id):
            claims.setdefault(member, []).append(n)
    entry_of = {member: min(claimants, key=lambda n: (fx.box_distance(ids[n], member), n))
                for member, claimants in claims.items()}
    return entries, ids, entry_of


_LOADED: dict[str, Fixture] = {}


def fixture_of(name: str) -> Fixture:
    if name not in _LOADED:
        _LOADED[name] = Fixture(FIXTURES / name)
    return _LOADED[name]


def directions_of(schedule, route_id):
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT DISTINCT direction_id FROM trips WHERE route_id = :r"),
            {"r": route_id}).fetchall()
    directions = sorted({row[0] for row in rows if row[0] is not None})
    return directions or [None]


def patterns_of(schedule, route_id, direction):
    """{stop pattern: [trip_id]} for one route and direction."""
    where = "AND (t.direction_id = :d OR t.direction_id IS NULL)"
    if direction is None:
        where = "AND t.direction_id IS NULL"
    sql = f"""
    SELECT st.trip_id, st.stop_id, st.stop_sequence
    FROM trips t INNER JOIN stop_times st ON st.trip_id = t.trip_id
    WHERE t.route_id = :r {where}
    ORDER BY st.trip_id, st.stop_sequence
    """  # noqa: S608
    trips = {}
    with schedule.engine.connect() as conn:
        for trip_id, stop_id, _seq in conn.execute(
                text(sql), {"r": route_id, "d": direction}):
            trips.setdefault(trip_id, []).append(stop_id)
    grouped = {}
    for trip_id, stops in trips.items():
        grouped.setdefault(tuple(stops), []).append(trip_id)
    return grouped


def service_date(schedule, trip_ids):
    """The first day one of these trips runs, as an ISO date, or None: the
    earliest calendar_dates addition, or the first weekday of a calendar
    window its removals leave, over the trips' services."""
    with schedule.engine.connect() as conn:
        services = {row[0] for row in conn.execute(text(
            "SELECT DISTINCT service_id FROM trips WHERE trip_id IN :trips"
        ).bindparams(bindparam("trips", expanding=True)),
            {"trips": list(trip_ids)})}
        exceptions = conn.execute(text(
            "SELECT service_id, date, exception_type FROM calendar_dates")).fetchall()
        days = {str(d)[:10] for s, d, k in exceptions if k == 1 and s in services}
        removed = {(s, str(d)[:10]) for s, d, k in exceptions if k == 2}
        for row in conn.execute(text(
                "SELECT service_id, monday, tuesday, wednesday, thursday, "
                "friday, saturday, sunday, start_date, end_date FROM calendar")):
            if row[0] not in services or not row[8] or not row[9]:
                continue
            day = datetime.date.fromisoformat(str(row[8])[:10])
            end = datetime.date.fromisoformat(str(row[9])[:10])
            while day <= end:
                if row[1 + day.weekday()] and (row[0], day.isoformat()) not in removed:
                    days.add(day.isoformat())
                    break
                day += datetime.timedelta(days=1)
    return min(days) if days else None


def pair_service_days(schedule, origin, destination, route_type,
                      route_id=None, direction=None):
    """Every day the feed says some trip rides origin before destination,
    as sorted ISO dates: calendar_dates additions, and calendar windows
    expanded by weekday minus their removals. The stops are matched the way
    the sensor matches them, by place, or by name prefix for a train. Any trip
    counts unless a route, and a direction on it, is named."""
    if route_type == "2":
        o_where = "o.stop_id IN (SELECT stop_id FROM stops WHERE stop_name LIKE :o)"
        x_where = "x.stop_id IN (SELECT stop_id FROM stops WHERE stop_name LIKE :x)"
        params = {"o": origin + "%", "x": destination + "%"}
    else:
        group = getattr(gtfs_helper, "_place_group", None)
        if group:
            o_where, x_where = "o.stop_id IN " + group("o"), "x.stop_id IN " + group("x")
        else:
            o_where, x_where = "o.stop_id = :o", "x.stop_id = :x"
        params = {"o": origin, "x": destination}
    on_route = ""
    if route_id is not None:
        on_route = " AND t.route_id = :route"
        params["route"] = route_id
        if direction is not None:
            on_route += " AND t.direction_id = :direction"
            params["direction"] = direction
    serving = f"""
    SELECT DISTINCT t.service_id FROM trips t
    INNER JOIN stop_times o ON o.trip_id = t.trip_id
    INNER JOIN stop_times x ON x.trip_id = t.trip_id
    WHERE {o_where} AND {x_where} AND o.stop_sequence < x.stop_sequence{on_route}
      AND {_WAY_ON} AND {_WAY_OFF}
    """  # noqa: S608
    days = set()
    with schedule.engine.connect() as conn:
        services = {row[0] for row in conn.execute(text(serving), params)}
        if not services:
            return []
        exceptions = conn.execute(text(
            "SELECT service_id, date, exception_type FROM calendar_dates")).fetchall()
        for service_id, day, kind in exceptions:
            if service_id in services and kind == 1:
                days.add(str(day)[:10])
        removed = {(s, str(d)[:10]) for s, d, k in exceptions if k == 2}
        for row in conn.execute(text(
                "SELECT service_id, monday, tuesday, wednesday, thursday, "
                "friday, saturday, sunday, start_date, end_date FROM calendar")):
            if row[0] not in services or not row[8] or not row[9]:
                continue
            day = datetime.date.fromisoformat(str(row[8])[:10])
            end = datetime.date.fromisoformat(str(row[9])[:10])
            while day <= end:
                if row[1 + day.weekday()] and (row[0], day.isoformat()) not in removed:
                    days.add(day.isoformat())
                day += datetime.timedelta(days=1)
    return sorted(days)


def services_on(schedule, day_iso):
    """The service_ids the feed runs on that day: calendar windows by
    weekday minus their removals, plus calendar_dates additions."""
    day = datetime.date.fromisoformat(day_iso)
    running = set()
    with schedule.engine.connect() as conn:
        exceptions = conn.execute(text(
            "SELECT service_id, date, exception_type FROM calendar_dates")).fetchall()
        removed = {s for s, d, k in exceptions if k == 2 and str(d)[:10] == day_iso}
        running |= {s for s, d, k in exceptions if k == 1 and str(d)[:10] == day_iso}
        for row in conn.execute(text(
                "SELECT service_id, monday, tuesday, wednesday, thursday, "
                "friday, saturday, sunday, start_date, end_date FROM calendar")):
            if not row[8] or not row[9]:
                continue
            start = datetime.date.fromisoformat(str(row[8])[:10])
            end = datetime.date.fromisoformat(str(row[9])[:10])
            if start <= day <= end and row[1 + day.weekday()]:
                running.add(row[0])
    return running - removed


def gtfs_seconds(value):
    """Seconds since the service day's midnight. GTFS hours pass 24; the db
    stores such a time on 1970-01-02, so a date in front counts as days."""
    value = str(value)
    days = 0
    if " " in value:
        date, value = value.split(" ", 1)
        days = (datetime.date.fromisoformat(date) - datetime.date(1970, 1, 1)).days
    h, m, s = (int(float(part)) for part in value.split(":"))
    return days * 86400 + h * 3600 + m * 60 + s


def late_departures(schedule, route_id, direction, origins, destinations,
                    day_iso, since="23:50:00"):
    """Departure times at the origin, on that day's service, of the trips
    of this route and direction that ride one of the origin's records before
    one of the destination's and leave at or after `since`. Hours past 24
    are that day's trips running into the next one."""
    running = services_on(schedule, day_iso)
    where = ("t.route_id = :route AND o.stop_id IN :origins "
             "AND x.stop_id IN :destinations AND o.stop_sequence < x.stop_sequence "
             f"AND {_WAY_ON} AND {_WAY_OFF}")
    params = {"route": route_id, "origins": list(origins),
              "destinations": list(destinations)}
    if direction is not None:
        where += " AND t.direction_id = :direction"
        params["direction"] = direction
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT t.service_id, o.departure_time FROM trips t "
            "INNER JOIN stop_times o ON o.trip_id = t.trip_id "
            "INNER JOIN stop_times x ON x.trip_id = t.trip_id "
            f"WHERE {where}"  # noqa: S608
        ).bindparams(bindparam("origins", expanding=True),
                     bindparam("destinations", expanding=True)),
            params).fetchall()
    # an untimed call (off the timepoints) leaves at no time anyone lists
    return sorted((str(dep) for service, dep in rows
                   if service in running and dep is not None
                   and gtfs_seconds(dep) >= gtfs_seconds(since)),
                  key=gtfs_seconds)


def next_of(days, from_iso, horizon=90):
    """What get_next_service_date is expected to answer from that day."""
    start = datetime.date.fromisoformat(from_iso)
    end = start + datetime.timedelta(days=horizon)
    for day in days:
        if start <= datetime.date.fromisoformat(day) <= end:
            return day
    return None


def shifted(iso, days):
    return (datetime.date.fromisoformat(iso) + datetime.timedelta(days=days)).isoformat()


def served_between(patterns, origins, destinations):
    """Some trip of the line really rides from one set to the other."""
    for pattern in patterns:
        before = [i for i, stop in enumerate(pattern) if stop in origins]
        after = [i for i, stop in enumerate(pattern) if stop in destinations]
        if before and after and min(before) < max(after):
            return True
    return False


def pieces_of(places):
    """A ride read as list positions, cut where it comes back to a position
    it already passed (a racket, a loop), repeats in a row dropped."""
    pieces, current = [], []
    for p in places:
        if current and p == current[-1]:
            continue
        if p in current:
            pieces.append(current)
            current = [current[-1], p]
        else:
            current.append(p)
    if len(current) > 1:
        pieces.append(current)
    return pieces


def rides_in_order(piece, size, ends=(), ways=(True, False)):
    """One way along the list, forward or backward.

    A loop has no straight order: its seam, a step across more than half the
    list, is allowed once. Nor has a terminus the two ways share: TAO 40 ends
    Montesquieu, Cheques Postaux quai D, then quai C, and GVB 14 starts in the
    turning loop at Flevopark, so one step against the way is allowed when it
    touches the trip's own first or last stop (ends). A step against the way
    in the middle of a ride still fails.
    """
    for forward in ways:
        wraps = shuffles = 0
        for a, b in zip(piece, piece[1:]):
            step = b - a
            if abs(step) > size / 2:
                wraps += 1
            elif (step > 0) != forward:
                if a in ends or b in ends:
                    shuffles += 1
                else:
                    break
        else:
            if wraps <= 1 and shuffles <= 1:
                return True
    return False


def variants_disagree(met, others):
    """The steps of a ride against the list's order, [(a, b)] as list
    positions, when another ride runs each of them the list's way; None as
    soon as one step no other ride makes that way.

    Two variants of one way may run the same two places in opposite orders
    (TEC B0026 round Noduwez both ways, a gtfs-nl variant through Het Kant
    before De Hoeve where the others pass it after Station Houten): no list
    can follow both, so the one the component picks is the answer, and the
    pair is recorded rather than failed."""
    excused = []
    for a, b in zip(met, met[1:]):
        if b >= a:
            continue
        if not any(a in other and b in other and other.index(b) < other.index(a)
                   for other in others):
            return None
        excused.append((a, b))
    return excused


def stretches_from(pattern, home, entry_of, ids, at):
    """Each stretch of a ride after one of its calls at the place `home`,
    as positions in the list `at` indexes, first meetings only."""
    stretches, ride, started = [], [], False
    for stop in list(pattern) + [None]:
        if stop is None or entry_of.get(stop) == home:
            if started:
                stretches.append(list(dict.fromkeys(
                    at[ids[entry_of[s]]] for s in ride
                    if s in entry_of and ids[entry_of[s]] in at)))
            ride, started = [], True
        else:
            ride.append(stop)
    return stretches


def line_patterns(schedule, route_id):
    """{stop pattern: [trip_id]} for the whole line, both ways round."""
    patterns = {}
    for direction in directions_of(schedule, route_id):
        for pattern, trip_ids in patterns_of(schedule, route_id, direction).items():
            patterns.setdefault(pattern, []).extend(trip_ids)
    return patterns


def rode_past_an_end(schedule, result, origins, destinations):
    """The stops the answer's trip calls at between its two ends that are one
    of those ends again: a shorter ride was on the same trip."""
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT stop_id FROM stop_times WHERE trip_id = :t "
            "AND stop_sequence > :o AND stop_sequence < :d"),
            {"t": result.get("trip_id"), "o": result["origin_stop_sequence"],
             "d": result["destination_stop_time"]["Sequence"]}).fetchall()
    return [r[0] for r in rows if r[0] in origins or r[0] in destinations]


SPREAD = 6


def _spread(first, last, count=SPREAD):
    """`count` positions from first to last, both included, evenly apart."""
    if last - first + 1 <= count:
        return list(range(first, last + 1))
    return sorted({first + round(n * (last - first) / (count - 1)) for n in range(count)})


def sample_origins(pattern):
    """Six stops spread along the ride, the first and the one before last
    among them (every stop of a short ride): a fault at a stop in the
    first or last third, a branch point say, is not left to chance."""
    return _spread(0, max(0, len(pattern) - 2))


def sample_pairs(pattern):
    """First to last, first to middle, middle to last: the ends and a leg;
    then one hop between each two of six stops spread along the ride, the
    short journeys most riders make."""
    seen = []
    first, last = 0, len(pattern) - 1
    middle = len(pattern) // 2
    spread = _spread(first, last)
    for pair in ((first, last), (first, middle), (middle, last),
                 *zip(spread, spread[1:])):
        o, d = pair
        if o < d and pattern[o] != pattern[d] and pair not in seen:
            seen.append(pair)
    return seen


class Check:
    """Every verification made for one case, as records: the verdict, the
    line a reader sees, and for a pair the asked and answered sides as
    fields. conftest.py writes them to results.txt and results.json."""

    def __init__(self):
        self.records = []

    def note(self, ok, text, **fields):
        self.records.append({"ok": bool(ok), "text": text, **fields})

    @property
    def failures(self):
        return [r["text"] for r in self.records if not r["ok"]]


# Cases known to fail, each with what breaks the promise. The marks are
# strict: the day a fix lands, its marks have to go with it, which is how a
# fix PR and the test that turns green arrive together. On main the stop
# selector, the swapped pair and the train match each carry a set of marks;
# this branch passes every one of them, so the dict is empty here, and a
# case that regresses gets its mark back with the reason.
KNOWN: dict[str, str] = {}


def _zip_table(archive, name):
    """The rows of one table of the zip, headers and values stripped as
    pygtfs strips them (a feed may pad its columns to a fixed width)."""
    if name not in archive.namelist():
        return []
    with archive.open(name) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in reader]


def _zip_lines(path):
    """({route_id: route_type}, {route_id: [direction_id]}) read off the zip.

    Collecting the cases only needs which routes are trains and which
    directions each one runs; reading the two tables costs milliseconds
    where building the db costs a second a fixture, and the db is then built
    by the first case that asks for it. The directions are the ones the db
    holds before any repair: the repair turns a trip round, it never gives a
    direction to a line that has none.
    """
    with zipfile.ZipFile(path / "static.zip") as archive:
        types_of = {r["route_id"]: int(r["route_type"] or 0)
                    for r in _zip_table(archive, "routes.txt")}
        directions = {}
        for trip in _zip_table(archive, "trips.txt"):
            value = trip.get("direction_id", "")
            directions.setdefault(trip["route_id"], set()).add(
                int(value) if value != "" else None)
    return types_of, {route: sorted(d for d in found if d is not None) or [None]
                      for route, found in directions.items()}


def _cases():
    cases = []
    if not FIXTURES.is_dir():
        return cases
    for path in sorted(FIXTURES.iterdir()):
        if not (path / "manifest.json").is_file():
            continue
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        routes_kept = manifest.get("routes_kept")
        if not manifest.get("static_only") or not routes_kept:
            continue
        route_types, directions = _zip_lines(path)
        for label, ids in sorted(routes_kept.items()):
            ids = [ids] if isinstance(ids, str) else ids
            for route_id in ids:
                train = route_types.get(route_id) == 2
                kinds = TRAIN_KINDS if train else KINDS
                shown = label if len(ids) == 1 else f"{label}({route_id[-8:]})"
                for direction in directions.get(route_id, [None]):
                    for kind in kinds:
                        case_id = f"{path.name}-{shown}-d{direction}-{kind}"
                        marks = ([pytest.mark.xfail(strict=True, reason=KNOWN[case_id])]
                                 if case_id in KNOWN else [])
                        cases.append(pytest.param(
                            path.name, route_id, direction, kind,
                            id=case_id, marks=marks))
    return cases


CASES = _cases()


@pytest.mark.parametrize("fixture,route_id,direction,kind", CASES)
def test_journeys(record_property, fixture, route_id, direction, kind):
    fx = fixture_of(fixture)
    # HA sets its default zone once at startup from the configured one, which
    # on an install reading a French network is the French one; left in UTC
    # the query picks its calendar day in UTC while the departures are
    # compared in Paris, two different days for a night line
    dt_util.set_default_time_zone(dt_util.get_time_zone(fx.agency_tz))
    check = Check()
    if fx.route_types.get(route_id) == 2:
        check_train_route(check, fx, route_id, direction, kind)
    else:
        check_route(check, fx, route_id, direction, kind)
    record_property("case", {"fixture": fixture, "route": route_id,
                             "direction": direction, "kind": kind})
    record_property("checks", check.records)
    assert not check.failures, "\n".join(check.failures)


def check_route(check, fx, route_id, direction, kind):
    """The flow's promises on one line, the trips of one direction sampled.

    The rider is offered one list per line, a place per entry, and picks
    where they are, then where they go; no direction is asked, the pair and
    the order of the stops on a trip say which way it is, and a loop's
    terminus keeps the rotation get_pair_direction settles. So the list, the
    destinations and the departures are asked the way the flow asks them,
    with no direction; `direction` only says which trips are sampled.
    """
    schedule = fx.schedule
    everything = line_patterns(schedule, route_id)
    grouped = patterns_of(schedule, route_id, direction)
    entries, ids, entry_of = line_places(fx, route_id)
    offered = get_stop_list(schedule, route_id, None)
    # the stops some trip of the line takes riders on at
    boarded = {stop for pattern, trip_ids in everything.items()
               for stop in pattern if fx.boards(trip_ids, stop)}

    if kind == "stop_list":
        offered_ids = [entry.split(": ", 1)[0] for entry in offered]
        # The entries read "STOP: Name (12)", the number being the
        # stop_sequence the selector showed; a stop offered twice is one
        # stop_id under two of those numbers, which is what a reader has to
        # be told to find it again in the feed.
        offered_at = {}
        for entry, stop_id in zip(offered, offered_ids):
            place = entry.rsplit(" (", 1)[-1].rstrip(")") if " (" in entry else "?"
            offered_at.setdefault(stop_id, []).append(place)
        repeated = {stop_id: places for stop_id, places in offered_at.items()
                    if len(places) > 1}
        text = "the stop list offers a stop twice"
        if repeated:
            text += ": " + listed([f"{named(fx, stop_id)} at "
                                   + " and ".join(places)
                                   for stop_id, places in repeated.items()])
        check.note(not repeated, text,
                   repeated={stop_id: places
                             for stop_id, places in repeated.items()})
        # A place is one entry, whatever its records: TAO line A offered
        # Jules Verne twice and each choice hid half the trams, Zou 653
        # offered each pole of Pont de la Brague, one per side of the road.
        twice = [(a, b) for i, a in enumerate(offered_ids) for b in offered_ids[i + 1:]
                 if b in fx.siblings_of(a)]
        text = "the list offers one place twice"
        if twice:
            text += ": " + listed([f"{named(fx, a)} and {named(fx, b)}" for a, b in twice])
        # folded keeps what the check recorded when it only looked at two
        # platforms of one station listed one after the other
        folded = [(a, b) for a, b in zip(offered_ids, offered_ids[1:])
                  if fx.station_of(a) and fx.station_of(a) == fx.station_of(b)]
        check.note(not twice, text, twice=[list(pair) for pair in twice],
                   folded=[list(pair) for pair in folded])
        # The origin list is the line's places less those nobody gets on
        # at: a call the feed flags as set-down only on every trip (a
        # night train's morning stops, a coach's way into town, a
        # terminus reached on its own record) is nowhere to get on. The
        # places kept come in the line's order.
        way_on = {n for stop, n in entry_of.items() if stop in boarded}
        shut = [ids[n] for n in range(len(ids)) if n not in way_on]
        wrongly = [s for s in offered_ids if s in shut]
        text = "a place nobody gets on at is offered"
        if wrongly:
            text += ": " + listed([named(fx, s) for s in wrongly])
        check.note(not wrongly, text, shut=shut, wrongly=wrongly)
        check.note(True, "places nobody gets on at, left out of the origin list"
                   + (": " + listed([named(fx, s) for s in shut]) if shut else ": none"),
                   shut=shut)
        in_line_order = [s for s in ids if s in set(offered_ids)] == offered_ids
        check.note(in_line_order, "the origin list keeps the line's order",
                   offered=offered_ids)
        for pattern in grouped:
            unoffered = [stop for stop in pattern
                         if stop not in entry_of
                         or (ids[entry_of[stop]] not in offered_ids and stop in boarded)]
            text = "a trip serves a stop the list does not offer"
            if unoffered:
                text += (": " + listed([named(fx, stop) for stop in unoffered])
                         + f" (on the ride {pattern[0]} .. {pattern[-1]})")
            check.note(not unoffered, text, unoffered=list(unoffered))
            known = [entry_of[stop] for stop in pattern if stop in entry_of]
            ends = (known[0], known[-1]) if known else ()
            ordered = all(rides_in_order(piece, len(ids), ends) for piece in pieces_of(known))
            check.note(ordered, "the list contradicts the riding order "
                                f"{pattern[0]} .. {pattern[-1]}")
        if direction == 0:
            # Which end comes first: the way most trips labelled 0 ride the
            # list, each trip counted, its steps along the list counted
            # (text is a message in this function, the query needs the sqlalchemy one)
            from sqlalchemy.sql import text as sql_text
            forward = backward = 0
            for pattern, trip_ids in grouped.items():
                with schedule.engine.connect() as conn:
                    labelled = conn.execute(sql_text(
                        "SELECT COUNT(*) FROM trips WHERE direction_id = 0 AND trip_id IN :t"
                    ).bindparams(bindparam("t", expanding=True)), {"t": list(trip_ids)}).scalar()
                known = [entry_of[stop] for stop in pattern if stop in entry_of]
                forward += labelled * sum(1 for a, b in zip(known, known[1:]) if b > a)
                backward += labelled * sum(1 for a, b in zip(known, known[1:]) if b < a)
            check.note(forward >= backward,
                       f"the list starts at {named(fx, ids[0])}: direction 0 trips take "
                       f"{forward} steps along it and {backward} against it",
                       forward=forward, backward=backward)
        return

    route_type = str(fx.route_types.get(route_id))
    if kind == "destinations":
        # From an origin, the trips that call at it and the rest of their
        # ride: the list must hold every place such a trip reaches, once, in
        # the order the ride makes, and nothing no trip through that origin
        # reaches, whichever way round the line it goes.
        for pattern, pattern_trips in grouped.items():
            for o in sample_origins(pattern):
                if pattern[o] not in entry_of:
                    continue
                origin = ids[entry_of[pattern[o]]]
                offered = [entry.split(": ", 1)[0] for entry in
                           get_destination_stop_list(schedule, route_id, None, origin)]
                at = {stop_id: n for n, stop_id in enumerate(offered)}
                who = f"from {named(fx, origin)}"
                if not any(s in boarded for s in fx.siblings_of(origin)):
                    # nobody gets on here: the flow never asks, and asked
                    # anyway there is nowhere to go
                    check.note(not offered, f"{who}, where nobody gets on: "
                               f"{len(offered)} destinations offered",
                               origin=origin, offered=offered, no_way_on=True)
                    continue
                twice = sorted({s for s in offered if offered.count(s) > 1})
                text = f"a destination is offered twice {who}"
                if twice:
                    text += ": " + listed([named(fx, s) for s in twice])
                check.note(not twice, text, origin=origin, twice=twice)
                # the places after the origin some trip of this ride sets
                # riders down at; a ride counts from its first call at the
                # origin with a way on
                after = [stop for stop in pattern[o + 1:]
                         if entry_of.get(stop) != entry_of[origin]
                         and fx.alights(pattern_trips, stop)]
                if not fx.boards(pattern_trips, pattern[o]):
                    after = []
                missing = [s for s in dict.fromkeys(after)
                           if s not in entry_of or ids[entry_of[s]] not in at]
                text = f"a stop this ride reaches {who} is not offered"
                if missing:
                    text += (": " + listed([named(fx, s) for s in missing])
                             + f" (on the ride {pattern[0]} .. {pattern[-1]})")
                check.note(not missing, text, origin=origin, missing=missing)
                reachable = set()
                for other, other_trips in everything.items():
                    hits = [i for i, s in enumerate(other)
                            if entry_of.get(s) == entry_of[origin]
                            and fx.boards(other_trips, s)]
                    if hits:
                        reachable.update(ids[entry_of[s]] for s in other[hits[0] + 1:]
                                         if s in entry_of and fx.alights(other_trips, s))
                stray = [s for s in offered if s not in reachable]
                text = f"a destination no trip reaches {who} is offered"
                if stray:
                    text += ": " + listed([named(fx, s) for s in stray])
                check.note(not stray, text, origin=origin, stray=stray)
                # Riding order across every trip of the line, one branch at a
                # time where the rides leave it open. Read from all the
                # line's trips: a place follows the places any of them calls
                # at just before it on its way from the origin (counted again
                # from a later call at the origin, a place met again on a
                # ride starting a new stretch). Among the places free to come
                # next, one that follows the place just listed goes on with
                # the branch in progress; otherwise, and between several, the
                # busiest by the trips of the rides reaching it, then the
                # nearest by the fewest stops, then the list's order. When
                # none is free (a loop's terminus, reached both ways round),
                # the same among the places left.
                fewest, before, trips_at = {}, {}, {}
                for other in everything:
                    count, previous, stretch, ride = None, None, set(), set()
                    for s in list(other) + [None]:
                        if s is not None and s not in entry_of:
                            continue
                        if s is None or entry_of[s] == entry_of[origin]:
                            for e in ride:
                                trips_at[e] = trips_at.get(e, 0) + len(everything[other])
                            if s is None:
                                break
                            count, previous, stretch, ride = 0, None, set(), set()
                            continue
                        e = ids[entry_of[s]]
                        if count is None:
                            continue
                        count += 1
                        ride.add(e)
                        fewest[e] = min(fewest.get(e, count), count)
                        before.setdefault(e, set())
                        if e in stretch:
                            stretch = {e}
                        elif previous is not None and previous != e:
                            before[e].add(previous)
                            stretch.add(e)
                        else:
                            stretch.add(e)
                        previous = e

                def busiest(e):
                    return (-trips_at.get(e, 0), fewest.get(e, 0), ids.index(e))

                listed_before, ordered, first_break, last = set(), True, None, None
                for e in offered:
                    left = [x for x in offered if x not in listed_before]
                    pool = [x for x in left if not (before.get(x, set()) - listed_before)] or left
                    going_on = [x for x in pool if last in before.get(x, set())]
                    expected = min(going_on or pool, key=busiest)
                    if e != expected and ordered:
                        ordered, first_break = False, [e, expected]
                    listed_before.add(e)
                    last = e
                check.note(ordered, f"the destinations {who} are not in riding order, "
                           f"one branch at a time where the rides leave it open"
                           + (f" ({named(fx, first_break[0])} before {named(fx, first_break[1])})"
                              if first_break else ""),
                           origin=origin,
                           order=[[s, trips_at.get(s, 0), fewest.get(s, 0)] for s in offered])
                # And along this ride, the order it makes: counted again from
                # a later call at the origin, one reshuffle allowed where it
                # touches the ride's own last stop (a terminus's quays). From
                # a loop's terminus every stop is reached both ways round, so
                # there nearest first is the order and this one is recorded
                # as not applying.
                terminus = any(entry_of.get(other[0]) == entry_of[origin]
                               and entry_of.get(other[-1]) == entry_of[origin]
                               for other in everything)
                along = True
                disagree = []
                # the other rides' stretches from this place, the list's
                # way of a pair a variant of the ride runs the other way
                others = [stretch for other in everything if other != pattern
                          for stretch in stretches_from(other, entry_of[origin],
                                                        entry_of, ids, at)]
                ride = []
                for stop in pattern[o + 1:] + (None,):
                    if stop is None or entry_of.get(stop) == entry_of[origin]:
                        known = [at[ids[entry_of[s]]] for s in ride
                                 if s in entry_of and ids[entry_of[s]] in at]
                        ends = (known[-1],) if known else ()
                        for piece in pieces_of(known):
                            if rides_in_order(piece, len(offered) * 4, ends, ways=(True,)):
                                continue
                            excused = variants_disagree(piece, others)
                            if excused:
                                disagree += [[offered[a], offered[b]] for a, b in excused]
                            else:
                                along = False
                        ride = []
                    else:
                        ride.append(stop)
                check.note(along or terminus,
                           f"the destinations {who} contradict the riding order "
                           f"{pattern[0]} .. {pattern[-1]}"
                           + (" (a loop's terminus: nearest first applies)" if terminus else "")
                           + (f" (variants disagree on {listed([' before '.join(p) for p in disagree])})"
                              if disagree and along else ""),
                           origin=origin, loop_terminus=terminus, along=along,
                           variants_disagree=disagree)
        return

    if kind == "next_service":
        pairs = []
        for pattern, trip_ids in grouped.items():
            ends = fx.rider_ends(pattern, trip_ids)
            if ends is None:
                check.note(True, f"the ride {pattern[0]} .. {pattern[-1]} has no timed "
                           f"call to get on at before one to get off at",
                           no_rider_ends=True)
                continue
            pairs.append((pattern[ends[0]], pattern[ends[1]]))
        check_next_service(check, fx, route_type, pairs)
        return

    if kind == "towards":
        seen = set()
        for pattern in grouped:
            for o in sample_origins(pattern):
                if pattern[o] not in entry_of or entry_of[pattern[o]] in seen:
                    continue
                home = entry_of[pattern[o]]
                seen.add(home)
                check_towards(check, fx, route_id, everything, ids, entry_of, home)
        return

    hass = fx.hass()
    with freeze_time(fx.instant_on("1970-01-01")) as clock:
        for pattern, trip_ids in sorted(grouped.items()):
            if any(stop not in entry_of for stop in pattern):
                check.note(False, "a pattern stop has no entry")
                continue
            day = service_date(schedule, trip_ids)
            if day is None:
                # the feed runs these trips on no day (Kraków's all-zero
                # services, MBTA's "canonical" templates): nothing to ask
                check.note(True, f"the feed runs no trip of the ride "
                           f"{pattern[0]} .. {pattern[-1]} on any day",
                           never_runs=True, trips=len(trip_ids))
                continue
            if kind == "midnight":
                ends = fx.rider_ends(pattern, trip_ids)
                if ends is None:
                    check.note(True, f"the ride {pattern[0]} .. {pattern[-1]} has no timed "
                               f"call to get on at before one to get off at",
                               no_rider_ends=True)
                    continue
                check_midnight(check, fx, clock, hass, route_id, route_type,
                               direction, None, pattern[ends[0]:ends[1] + 1],
                               entries, entry_of)
                continue
            clock.move_to(fx.instant_on(day))
            for o, d in sample_pairs(pattern):
                origin, destination = ids[entry_of[pattern[o]]], ids[entry_of[pattern[d]]]
                if origin == destination:
                    # a racket or a loop from its terminus round to it: two
                    # records of one place, which the list offers once, so no
                    # entry can ask it; recorded, not asked
                    asked = asked_of(pattern, o, d, route_id, None)
                    check.note(True, f"asked {pattern[o]} -> {pattern[d]} on {route_id}: "
                               f"one place ({named(fx, origin)}), not a journey the list offers",
                               asked=asked, got=None, same_place=True)
                    continue
                kept = gtfs_helper.get_pair_direction(schedule, route_id, origin, destination)
                data = _data_for(schedule, route_id, route_type, entries,
                                 entry_of, pattern[o], pattern[d], kept)
                origins, reached = fx.siblings_of(origin), fx.siblings_of(destination)
                if kind == "pairs" and not (fx.boards(trip_ids, pattern[o])
                                            and fx.alights(trip_ids, pattern[d])):
                    # the feed forbids one end of this ride: nothing is the
                    # answer, whatever the clock says
                    result = get_next_departure(hass, data)
                    asked = asked_of(pattern, o, d, route_id, kept)
                    got = got_of(result)
                    check.note(not result, f"asked {pattern[o]} -> {pattern[d]} on "
                               f"{route_id}: no way {'on' if not fx.boards(trip_ids, pattern[o]) else 'off'}"
                               f" on this ride, got {got['trip'] if got else 'nothing'}",
                               asked=asked, got=got, forbidden=True)
                    continue
                if kind == "pairs" and not (fx.times(trip_ids, pattern[o])
                                            and fx.times(trip_ids, pattern[d])):
                    # the feed leaves one end of this ride untimed: its trips
                    # are never the answer, another ride timed at both
                    # places may be
                    result = get_next_departure(hass, data)
                    asked = asked_of(pattern, o, d, route_id, kept)
                    got = got_of(result)
                    check.note(not result or result.get("trip_id") not in trip_ids,
                               f"asked {pattern[o]} -> {pattern[d]} on {route_id}: "
                               f"no time at the {'start' if not fx.times(trip_ids, pattern[o]) else 'end'}"
                               f" of this ride, got {got['trip'] if got else 'nothing'}",
                               asked=asked, got=got, untimed=True)
                    continue
                if kind == "pairs":
                    result = get_next_departure(hass, data)
                    ok = (isinstance(result, dict) and result
                          and result.get("origin_stop_id") in origins
                          and result.get("destination_stop_id") in reached
                          and result["origin_stop_sequence"]
                          < result["destination_stop_time"]["Sequence"]
                          and result["arrival_time"] >= result["departure_time"])
                    asked = asked_of(pattern, o, d, route_id, kept)
                    got = got_of(result)
                    check.note(ok, answered(asked, got), asked=asked, got=got)
                    if result:
                        # the answer is the entry's own line, listed in
                        # time order with one item per list per departure
                        shape = shape_of(result, fx.route_short_names[route_id])
                        check.note(shape["ok"], shaped(asked, shape),
                                   asked=asked, got=got, shape=shape)
                        # each departure names the record it leaves from, a
                        # record of the asked place
                        leaving = result.get("next_departures_origin_stop_id") or []
                        elsewhere = [s for s in leaving if s not in origins]
                        check.note(bool(leaving) and not elsewhere,
                                   f"asked {origin} -> {destination} on {route_id}: "
                                   f"{len(leaving)} departures leave from "
                                   f"{sorted(set(leaving))}"
                                   + (f", not the asked place: {sorted(set(elsewhere))}" if elsewhere else ""),
                                   asked=asked, got=got, leaving=sorted(set(leaving)))
                        # and it rides the direction the entry keeps, when it
                        # keeps one (a loop's terminus); otherwise the pair
                        # alone decides, and the direction ridden is recorded
                        rode = str(result.get("trip_direction_id"))
                        if kept is not None:
                            check.note(rode == str(kept),
                                       f"asked d{kept} on {route_id}: "
                                       f"trip {got['trip']} rides d{rode}",
                                       asked=asked, got=got)
                        else:
                            check.note(True,
                                       f"asked no direction on {route_id}: "
                                       f"trip {got['trip']} rides d{rode}",
                                       asked=asked, got=got)
                    if ok:
                        # and it is the shortest ride on its trip: a trip
                        # passing an end twice does not board the rider on
                        # the pole across the road for the long way round
                        past = rode_past_an_end(schedule, result, origins, reached)
                        check.note(not past,
                                   f"asked {origin} -> {destination} on {route_id}: "
                                   f"trip {got['trip']} calls at an end again on the way"
                                   + (f" ({listed(past)})" if past else ""),
                                   asked=asked, got=got)
                    # and the first departure listed is the next one the feed
                    # has, before dawn and at midday: a later ride of the day
                    # would pass every check above
                    check_next_ride(check, fx, clock, hass, data, day, result,
                                    route_id, kept, origin, destination, origins,
                                    reached, asked)
                else:
                    swapped = dict(data, origin=data["destination"],
                                   destination=data["origin"],
                                   direction=str(gtfs_helper.get_pair_direction(
                                       schedule, route_id, destination, origin)))
                    result = get_next_departure(hass, swapped)
                    # Both ways round are offered, so the reverse pair is a
                    # journey whenever some trip of the line rides it. The
                    # promise is that an answer, when there is one, matches a
                    # ride the line actually makes; answering nothing stays
                    # acceptable, the pattern that rides it may not run on
                    # the frozen day.
                    served = served_between(everything, reached, origins)
                    honest = not result or (
                        served
                        and result["origin_stop_sequence"]
                        < result["destination_stop_time"]["Sequence"]
                        and result["arrival_time"] >= result["departure_time"])
                    asked = asked_of(pattern, d, o, route_id, None, served=served)
                    got = got_of(result)
                    check.note(honest, answered(asked, got), asked=asked, got=got)


PARALLEL = ("next_departures_lines", "next_departures_headsign",
            "next_departures_trip_id",
            "next_departures_destination_arrival_times",
            "next_departures_durations",
            "next_departures_origin_stop_id")


def shape_of(result, short_name):
    """How the answer's lists hold together: departures in time order
    without a repeat, every companion list one item per departure, one line
    only and it is the entry's, durations that are whole minutes at or
    above zero. Returned as fields so results.json keeps them."""
    departures = result.get("next_departures") or []
    lengths = {key: len(result.get(key) or []) for key in PARALLEL}
    # an item reads "<instant> (<short>/<long>)"; the long name may hold
    # brackets of its own (Carris' "Lisboa (Oriente) - Vale da Amoreira"),
    # the instant never does, so the line starts at the first one
    lines = sorted({item.split(" (", 1)[-1][:-1].split("/", 1)[0]
                    for item in result.get("next_departures_lines") or []})
    durations = result.get("next_departures_durations") or []
    shape = {
        "departures": len(departures),
        "ordered": departures == sorted(departures) and len(set(departures)) == len(departures),
        "parallel": all(n == len(departures) for n in lengths.values()),
        "lengths": lengths,
        "lines": lines,
        "one_line": lines == [str(short_name)],
        "duration": result.get("duration"),
        "durations_sane": (isinstance(result.get("duration"), int)
                           and result["duration"] >= 0
                           and all(isinstance(d, int) and d >= 0 for d in durations)),
    }
    shape["ok"] = bool(departures) and all(
        shape[key] for key in ("ordered", "parallel", "one_line", "durations_sane"))
    return shape


def shaped(asked, shape):
    """The one line a reader sees for the answer's shape."""
    head = (f"asked {asked['origin']} -> {asked['destination']} on "
            f"{asked['route']}: {shape['departures']} departures")
    faults = []
    if not shape["departures"]:
        faults.append("none listed")
    if not shape["ordered"]:
        faults.append("not in time order")
    if not shape["parallel"]:
        faults.append("lists of unequal length " + ", ".join(
            f"{k.replace('next_departures_', '')} {n}" for k, n in shape["lengths"].items()))
    if not shape["one_line"]:
        faults.append(f"lines {shape['lines']}")
    if not shape["durations_sane"]:
        faults.append(f"duration {shape['duration']!r}")
    if not faults:
        return f"{head}, one line {shape['lines'][0]}, duration {shape['duration']} min"
    return f"{head}: " + "; ".join(faults)


NEXT_RIDE_CLOCKS = (datetime.time(0, 5), datetime.time(12, 0))


def check_next_ride(check, fx, clock, hass, data, day, result, route_id, kept,
                    origin, destination, origins, reached, asked):
    """The first departure get_next_departure lists is the next ride the
    feed has for the pair (Fixture.next_ride), at 00:05, the answer already
    in hand, and at 12:00 of the same day, when a fixture's few trips have
    often all left and the next ride is another day's; with none left in the
    calendar the answer is empty. The clock is put back to 00:05 for the
    next pair."""
    zone = zoneinfo.ZoneInfo(fx.agency_tz)
    for at in NEXT_RIDE_CLOCKS:
        now = fx.instant_on(day, at)
        if at != NEXT_RIDE_CLOCKS[0]:
            clock.move_to(now)
            result = get_next_departure(hass, data)
        expected = fx.next_ride(route_id, kept, origins, reached, now)
        # the departure the sensor shows, and the head of the list it shows
        # beside it: the two are built apart, so both are read
        shown = (result or {}).get("departure_time")
        shown = shown.astimezone(zone) if isinstance(shown, datetime.datetime) else None
        listed_first = (result or {}).get("next_departures") or []
        first = (datetime.datetime.fromisoformat(listed_first[0]).astimezone(zone)
                 if listed_first else None)
        if expected is None:
            ok = shown is None and first is None
        else:
            ok = all(got is not None and abs(got.timestamp() - expected[0].timestamp()) < 60
                     for got in (shown, first))
        check.note(ok, (
            f"at {at:%H:%M} on {day}, {origin} -> {destination} on {route_id}: "
            f"the feed's next ride "
            + (f"leaves {expected[0]:%m-%d %H:%M} (trip {expected[1]})" if expected
               else "is none left in the calendar")
            + ", got " + (f"{shown:%m-%d %H:%M}" if shown else "nothing")
            + ", listed first " + (f"{first:%m-%d %H:%M}" if first else "nothing")),
            asked=dict(asked, at=f"{at:%H:%M}"), got=got_of(result),
            next_expected=([expected[0].isoformat(), expected[1]] if expected else None),
            next_got=shown.isoformat() if shown else None,
            next_listed=first.isoformat() if first else None)
    clock.move_to(fx.instant_on(day))


def check_midnight(check, fx, clock, hass, route_id, route_type, direction,
                   query_direction, pattern, entries, stood_for):
    """Asked at 23:50 from the ends of the pattern, on a day the pair runs
    (one followed by another such day when the feed has it): nothing already
    gone is listed, and what the feed still has to offer is there: the
    day's own late trips, the ones timed past 24:00 included, since they
    leave after the clock turns, and the next day's trips. The expected
    side is read from stop_times and the calendar tables, so a departure
    dropped at the day change shows up as a missing crossing."""
    schedule = fx.schedule
    origin, destination = pattern[0], pattern[-1]
    origins, destinations = fx.siblings_of(origin), fx.siblings_of(destination)
    if destination in origins:
        # a racket or a loop ends where it starts: one place, which the list
        # offers once, so no entry can ask it; recorded, not asked
        for include_tomorrow in (False, True):
            check.note(True, f"at 23:50{' with tomorrow' if include_tomorrow else ''}, "
                       f"{origin} -> {destination} on {route_id}: one place "
                       f"({named(fx, origin)}), not a journey the list offers",
                       origin=origin, destination=destination, same_place=True,
                       include_tomorrow=include_tomorrow)
        return
    ids = [entry.split(": ", 1)[0] for entry in entries]
    query_direction = gtfs_helper.get_pair_direction(
        schedule, route_id, ids[stood_for[origin]], ids[stood_for[destination]])
    days = pair_service_days(schedule, origin, destination, route_type,
                             route_id, query_direction)
    who = f"{origin} -> {destination} on {route_id}"
    if query_direction is not None:
        who += f" d{query_direction}"
    if not days:
        check.note(False, f"no service day for {who}",
                   origin=origin, destination=destination)
        return
    day = next((d for d in days if shifted(d, 1) in days), days[0])
    tomorrow_runs = shifted(day, 1) in days
    late = late_departures(schedule, route_id, query_direction, origins,
                           destinations, day)
    past_midnight = [t for t in late if gtfs_seconds(t) >= 24 * 3600]
    now = fx.instant_on(day, datetime.time(23, 50))
    clock.move_to(now)
    zone = zoneinfo.ZoneInfo(fx.agency_tz)
    day_date = datetime.date.fromisoformat(day)
    # include_tomorrow is no longer read: the query lists the next
    # departures whatever day they fall on, so asking without it asked the
    # same thing again under a looser promise. Recorded, not asked.
    check.note(True, f"at 23:50 on {day}, {who}: without tomorrow is not an option "
               f"any more, the departures run on into the next days",
               origin=origin, destination=destination, include_tomorrow=False,
               retired=True)
    for include_tomorrow in (True,):
        data = _data_for(schedule, route_id, route_type, entries, stood_for,
                         origin, destination, query_direction,
                         include_tomorrow=include_tomorrow)
        result = get_next_departure(hass, data)
        listed = sorted(
            datetime.datetime.fromisoformat(iso).astimezone(zone)
            for iso in (result.get("next_departures") if result else []) or [])
        gone = [t for t in listed if t < now - datetime.timedelta(minutes=1)]
        crossing = [t for t in listed if t.date() > day_date]
        expect_some = bool(late) or (include_tomorrow and tomorrow_runs)
        expect_crossing = bool(past_midnight) or (include_tomorrow and tomorrow_runs)
        ok = (not gone
              and (listed or not expect_some)
              and (crossing or not expect_crossing))
        asked = {"origin": origin, "destination": destination,
                 "route": route_id, "direction": query_direction,
                 "day": day, "at": "23:50", "include_tomorrow": include_tomorrow,
                 "feed_late": late, "feed_past_midnight": len(past_midnight),
                 "tomorrow_runs": tomorrow_runs}
        got = {"listed": [t.isoformat() for t in listed],
               "past_midnight": len(crossing), "gone": len(gone),
               "trip": result.get("trip_id") if result else None}
        check.note(ok, (
            f"at 23:50 on {day}{' with tomorrow' if include_tomorrow else ''}, "
            f"{who}: the feed keeps {len(late)} departures, {len(past_midnight)} "
            f"past midnight, tomorrow {'runs' if tomorrow_runs else 'rests'}; "
            f"got {len(listed)} listed, {len(crossing)} past midnight, "
            f"{len(gone)} already gone"
            + (f" (first {listed[0]:%Y-%m-%d %H:%M})" if listed else "")),
            asked=asked, got=got)


def check_next_service(check, fx, route_type, pairs):
    """For each pair: asked on the first day it runs, the answer is that
    day; asked the day after, the next day the feed holds within the
    horizon or nothing; asked after its last day, nothing within the
    horizon. The expected side is read from the calendar tables directly,
    so a wrong day or an ignored table shows up as a wrong date."""
    seen = set()
    for origin, destination in pairs:
        if (origin, destination) in seen or origin == destination:
            continue
        seen.add((origin, destination))
        days = pair_service_days(fx.schedule, origin, destination, route_type)
        who = f"{origin} -> {destination}"
        if not days:
            check.note(False, f"no service day for {who}",
                       origin=origin, destination=destination)
            continue
        first, last = days[0], days[-1]
        for asked in (first, shifted(first, 1), shifted(last, 1)):
            expected = next_of(days, asked)
            got = get_next_service_date(fx.schedule, origin, destination,
                                        asked, route_type)
            check.note(got == expected,
                       f"asked on {asked} for {who}: expected {expected}, "
                       f"got {got}", origin=origin, destination=destination,
                       asked=asked, expected=expected, got=got)


def check_towards(check, fx, route_id, everything, ids, entry_of, home):
    """The way question from one origin, read from the line's trips.

    A ride runs from a call at the origin to the trip's next call at it, or
    its end. A way is the terminus of the ride's trip, as the bus shows it;
    the next stop comes with it only when that terminus is a loop's (both
    rotations end there) or the origin itself. A trip ending short of a
    terminus goes the way of the trips that leave for the same next stop and
    pass its end, or every stop of its ride but the end (a pole of its own).
    The question is asked when there are two ways or more,
    and each answer's destinations are exactly the places its rides reach.
    """
    schedule = fx.schedule
    origin = ids[home]
    who = f"from {named(fx, origin)}"
    line = [[entry_of[s] for s in pattern if s in entry_of] for pattern in everything]
    # the places a ride sets riders down at: a turnback or a relief point
    # on the way (Brisbane's G:link) is ridden past, never reached
    set_down = {pattern: {entry_of[s] for s in pattern
                          if s in entry_of and fx.alights(trip_ids, s)}
                for pattern, trip_ids in everything.items()}
    loop_termini = {k[0] for k in line if k and k[0] == k[-1]}
    terminus = home in loop_termini
    rides = {}
    for known, pattern in zip(line, everything):
        # a ride starts at a call at the origin a rider gets on at; the
        # others still end the ride before (Zou 620 only sets down at Pont
        # des Gabres on its way into Cannes: no way out there)
        on = [fx.boards(everything[pattern], s) for s in pattern if s in entry_of]
        ride, way_on = None, True
        for n, boards in list(zip(known, on)) + [(home, True)]:
            if n == home:
                if ride and way_on:
                    end = known[-1]
                    key = (end, ride[0] if end in loop_termini or end == home else None)
                    rides.setdefault(key, []).append((ride, pattern))
                ride, way_on = [], boards
            elif ride is not None:
                ride.append(n)
    folded = {}
    for key, calls in rides.items():
        if key[1] is not None:
            continue
        for other, other_calls in rides.items():
            if other != key and other[1] is None and any(
                    ride[0] == mine[0]
                    and (key[0] in [p for p in ride if p != ride[-1]]
                         or {p for p in mine if p != mine[-1]} <= set(ride))
                    for ride, _pattern in other_calls for mine, _mine_pattern in calls):
                folded[key] = other
                break
    ways_seen = {}
    for key, calls in rides.items():
        chain = []
        while key in folded and key not in chain:
            chain.append(key)
            key = folded[key]
        if key in chain:
            # two poles of one terminus fold into each other: the code keeps
            # the smallest stop_id, the entries are compared on ids
            cycle = chain[chain.index(key):]
            key = min(cycle, key=lambda k: tuple(ids[n] if n is not None else "" for n in k))
        ways_seen.setdefault("|".join(ids[n] for n in key if n is not None), []).extend(calls)
    expected = len(ways_seen) >= 2
    ways = gtfs_helper.get_towards(schedule, route_id, origin)
    check.note(bool(ways) == expected and (not ways or len(ways) == len(ways_seen)),
               f"{len(ways)} ways asked {who}, the trips go {len(ways_seen)} ways"
               + (" (a loop's terminus)" if terminus else ""),
               origin=origin, asked=[list(w) for w in ways], ways=len(ways_seen))
    if not ways:
        return
    labels = [label for _way, label in ways]
    check.note(len(set(labels)) == len(labels), f"the ways {who} read {labels}",
               origin=origin, labels=labels)
    whole = [e.split(": ", 1)[0] for e in
             get_destination_stop_list(schedule, route_id, None, origin)]
    sides = {}
    for way, label in ways:
        mine = ways_seen.get(way, [])
        offered = [e.split(": ", 1)[0] for e in
                   get_destination_stop_list(schedule, route_id, None, origin, way)]
        sides[way] = offered
        reached = list(dict.fromkeys(ids[n] for ride, pattern in mine for n in ride
                                     if n in set_down[pattern]))
        missing = [s for s in reached if s not in offered]
        stray = [s for s in offered if s not in reached]
        check.note(bool(mine) and not missing and not stray,
                   f"towards {label} {who}: {len(offered)} destinations"
                   + (f", missing {listed([named(fx, s) for s in missing])}" if missing else "")
                   + (f", no ride that way reaches {listed([named(fx, s) for s in stray])}" if stray else ""),
                   origin=origin, way=way, offered=offered, missing=missing, stray=stray)
        # the order a ride first meets its places: coming back past them
        # (Palm Bus 21 out round its loop and back down the same street)
        # meets places already listed
        at = {s: i for i, s in enumerate(offered)}
        along = True
        disagree = []
        mets = [list(dict.fromkeys(at[ids[n]] for n in ride if ids[n] in at))
                for ride, _pattern in mine]
        for i, met in enumerate(mets):
            if rides_in_order(met, len(offered) * 4, met[-1:], ways=(True,)):
                continue
            excused = variants_disagree(met, mets[:i] + mets[i + 1:])
            if excused:
                disagree += [[offered[a], offered[b]] for a, b in excused]
            else:
                along = False
        check.note(along, f"towards {label} {who}: the destinations "
                          f"follow every ride that way"
                          + (f" (variants disagree on {listed([' before '.join(p) for p in disagree])})"
                             if disagree and along else ""),
                   origin=origin, way=way, variants_disagree=disagree)
        if terminus and offered:
            # at a loop's terminus the answer is the rotation: the entry
            # keeps a label the trips riding that way carry
            far = offered[-1]
            kept = gtfs_helper.get_pair_direction(schedule, route_id, origin, far, way)
            trip_ids = [trip_id for ride, pattern in mine if ids.index(far) in ride
                        for trip_id in everything[pattern]]
            with schedule.engine.connect() as conn:
                carried = {str(row[0]) for row in conn.execute(text(
                    "SELECT DISTINCT direction_id FROM trips WHERE trip_id IN :t"
                ).bindparams(bindparam("t", expanding=True)), {"t": trip_ids or [""]})}
            check.note(kept is not None and str(kept) in carried,
                       f"towards {label} {who} to {named(fx, far)}: "
                       f"the entry keeps direction {kept}, trips that way carry {sorted(carried)}",
                       origin=origin, way=way, kept=kept, carried=sorted(carried))
    lost = [s for s in whole if not any(s in side for side in sides.values())]
    check.note(not lost and set(whole) == set().union(*map(set, sides.values())),
               f"the ways {who} make the whole destination list"
               + (f", lost {listed([named(fx, s) for s in lost])}" if lost else ""),
               origin=origin, lost=lost)


def asked_of(pattern, a, b, route, direction, served=None):
    """The asked side of a pair, with where the two stops sit in the
    sequence being checked, so the line can be read without opening the
    zip: stop 35 to stop 1 of a 35-stop ride is a journey against the
    direction. `served` says whether this direction rides it at all."""
    asked = {"origin": pattern[a], "destination": pattern[b], "route": route,
             "direction": direction, "from_stop": a + 1, "to_stop": b + 1,
             "ride_length": len(pattern)}
    if served is not None:
        asked["served"] = served
    return asked


def got_of(result, by_name=False):
    """What get_next_departure gave back: the stops, the line and direction
    of the trip it took them from, and that trip. None when it gave nothing."""
    if not result:
        return None
    if by_name:
        return {"origin": result.get("origin_stop_name"),
                "destination": result.get("destination_stop_name"),
                "route": result.get("route_short_name"),
                "direction": result.get("trip_direction_id"),
                "trip": result.get("trip_id")}
    return {"origin": result.get("origin_stop_id"),
            "destination": result.get("destination_stop_id"),
            "route": result.get("route_id"),
            "direction": result.get("trip_direction_id"),
            "trip": result.get("trip_id")}


def named(fx, stop_id):
    """A stop as a reader can look it up: its id and the name it carries."""
    name = fx.stop_names.get(stop_id)
    return f"{stop_id} {name}" if name else stop_id


def listed(items, limit=3):
    """The first few of a list, then how many were left out."""
    shown = ", ".join(items[:limit])
    rest = len(items) - limit
    return f"{shown}, and {rest} more" if rest > 0 else shown


def answered(asked, got):
    """The one line a reader sees for a pair: the asked side, then the
    answered side, both rendered from the same records results.json holds."""
    where = (f"stop {asked['from_stop']} to stop {asked['to_stop']} of a "
             f"{asked['ride_length']}-stop ride")
    if asked.get("served") is False:
        where += ", this direction does not make it"
    on = f" on {asked['route']}"
    if asked["direction"] is not None:
        on += f" d{asked['direction']}"
    head = f"asked {asked['origin']} -> {asked['destination']}{on} ({where})"
    if not got:
        return f"{head}: no departure"
    return (f"{head}: got {got['origin']} -> {got['destination']} on "
            f"{got['route']} d{got['direction']}, trip {got['trip']}")


def _data_for(schedule, route_id, route_type, entries, position,
              origin, destination, direction, include_tomorrow=False):
    return {
        "schedule": schedule,
        "gtfs_dir": ".", "file": "fixture",
        "route_type": route_type,
        "origin": entries[position[origin]],
        "destination": entries[position[destination]],
        "direction": str(direction),
        # the rotation a loop's entry keeps, the one direction the query reads
        "loop_direction": direction,
        "route": route_id,
        "offset": 0,
        "include_tomorrow": include_tomorrow,
    }


def check_train_route(check, fx, route_id, direction, kind):
    schedule = fx.schedule
    short_name = fx.route_short_names[route_id]
    hass = fx.hass()
    grouped = patterns_of(schedule, route_id, direction)
    if kind == "next_service":
        pairs = []
        for pattern in grouped:
            for o, d in sample_pairs(pattern)[:1]:
                pairs.append((fx.stop_names[pattern[o]], fx.stop_names[pattern[d]]))
        check_next_service(check, fx, "2", pairs)
        return
    if kind == "stations":
        check_train_stations(check, fx, route_id, direction)
        return
    if kind == "destinations":
        check_train_destinations(check, fx, route_id, direction)
        return
    with freeze_time(fx.instant_on("1970-01-01")) as clock:
        for pattern, trip_ids in sorted(grouped.items()):
            day = service_date(schedule, trip_ids)
            if day is None:
                # the feed runs these trips on no day (Kraków's all-zero
                # services, MBTA's "canonical" templates): nothing to ask
                check.note(True, f"the feed runs no trip of the ride "
                           f"{pattern[0]} .. {pattern[-1]} on any day",
                           never_runs=True, trips=len(trip_ids))
                continue
            clock.move_to(fx.instant_on(day))
            for o, d in sample_pairs(pattern):
                name_o = fx.stop_names[pattern[o]]
                name_d = fx.stop_names[pattern[d]]
                if name_o == name_d:
                    continue
                if not (fx.boards(trip_ids, pattern[o]) and fx.alights(trip_ids, pattern[d])):
                    # a set-down only station, a station the train passes:
                    # not a journey, whatever runs
                    data = {
                        "schedule": schedule,
                        "gtfs_dir": ".", "file": "fixture",
                        "route_type": "2",
                        "origin": name_o, "destination": name_d,
                        "direction": 0, "route": "train",
                        "line": short_name,
                        "offset": 0, "include_tomorrow": False,
                    }
                    result = get_next_departure(hass, data)
                    asked = asked_of([fx.stop_names[s] for s in pattern], o, d,
                                     short_name, None)
                    got = got_of(result, by_name=True)
                    check.note(not result, f"asked {name_o} -> {name_d} on {short_name}: "
                               f"no way {'on' if not fx.boards(trip_ids, pattern[o]) else 'off'}"
                               f" on this ride, got {got['trip'] if got else 'nothing'}",
                               asked=asked, got=got, forbidden=True)
                    continue
                data = {
                    "schedule": schedule,
                    "gtfs_dir": ".", "file": "fixture",
                    "route_type": "2",
                    "origin": name_o, "destination": name_d,
                    "direction": 0, "route": "train",
                    "line": short_name,
                    "offset": 0, "include_tomorrow": False,
                }
                result = get_next_departure(hass, data)
                ok = (isinstance(result, dict) and result
                      and result.get("origin_stop_name") == name_o
                      and result.get("destination_stop_name") == name_d
                      and result.get("route_short_name") == short_name
                      and result["origin_stop_sequence"]
                      < result["destination_stop_time"]["Sequence"]
                      and result["arrival_time"] >= result["departure_time"])
                asked = asked_of([fx.stop_names[s] for s in pattern], o, d,
                                 short_name, None)
                got = got_of(result, by_name=True)
                check.note(ok, answered(asked, got), asked=asked, got=got)


def _train_data(fx, short_name, origins, destinations):
    """A train entry that ticked these stations at each end."""
    return {
        "schedule": fx.schedule,
        "gtfs_dir": ".", "file": "fixture",
        "route_type": "2",
        "origin": origins[0], "destination": destinations[0],
        "origin_stations": list(origins),
        "destination_stations": list(destinations),
        "direction": 0, "route": "train",
        "line": short_name,
        "offset": 0, "include_tomorrow": False,
    }


def first_call_at(schedule, trip_id, names):
    """The stop_id at which a trip first calls at one of these stations."""
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT st.stop_id, s.stop_name FROM stop_times st "
            "INNER JOIN stops s ON s.stop_id = st.stop_id "
            "WHERE st.trip_id = :t ORDER BY st.stop_sequence"),
            {"t": trip_id}).fetchall()
    return next((stop_id for stop_id, name in rows if name in names), "")


def check_train_stations(check, fx, route_id, direction):
    """Several stations ticked at one end, the way a rider ticks the station
    and the coach station its replacement coaches leave from (SNCF K8+:
    Paris Austerlitz and Paris-Austerlitz Routiere). The two ends of each
    pattern are paired with the same end of every other pattern: where the
    coaches and the trains start apart, or end apart (P8 leaves Orleans on
    one name for both, and reaches Paris on two). The answer must be every
    departure each pairing gives alone, no more, no less; the next service
    date the earliest of theirs; the return test true when one of them is.
    And each departure says what rides it: a replacement bus (714) exactly
    when it leaves from a coach stop, the line's own type otherwise. On a
    line that has coach stops, at least one coach departure must have been
    read, or the check proved nothing."""
    schedule = fx.schedule
    short_name = fx.route_short_names[route_id]
    line_type = fx.route_types[route_id]
    hass = fx.hass()
    grouped = patterns_of(schedule, route_id, direction)
    firsts = list(dict.fromkeys(fx.stop_names[p[0]] for p in sorted(grouped)))
    lasts = list(dict.fromkeys(fx.stop_names[p[-1]] for p in sorted(grouped)))
    prefix = gtfs_helper.COACH_STOP_PREFIX
    coach_line = any(stop.startswith(prefix) for p in grouped for stop in p)
    coaches_read = 0
    with freeze_time(fx.instant_on("1970-01-01")) as clock:
        for pattern, trip_ids in sorted(grouped.items()):
            day = service_date(schedule, trip_ids)
            if day is None:
                # the feed runs these trips on no day (Kraków's all-zero
                # services, MBTA's "canonical" templates): nothing to ask
                check.note(True, f"the feed runs no trip of the ride "
                           f"{pattern[0]} .. {pattern[-1]} on any day",
                           never_runs=True, trips=len(trip_ids))
                continue
            clock.move_to(fx.instant_on(day))
            name_o = fx.stop_names[pattern[0]]
            name_d = fx.stop_names[pattern[-1]]
            if name_o == name_d:
                continue
            # (the stations ticked at the origin, at the destination, and the
            # single pairings they stand for)
            tickings = [([name_o, other], [name_d], [([name_o], [name_d]), ([other], [name_d])])
                        for other in firsts if other not in (name_o, name_d)]
            tickings += [([name_o], [name_d, other], [([name_o], [name_d]), ([name_o], [other])])
                         for other in lasts if other not in (name_o, name_d)]
            for origins, destinations, singles in tickings:
                where = f"{' + '.join(origins)} -> {' + '.join(destinations)} on {day}"
                alone = [get_next_departure(hass, _train_data(fx, short_name, o, d))
                         for o, d in singles]
                both = get_next_departure(hass, _train_data(fx, short_name, origins, destinations))
                trips = (both or {}).get("next_departures_trip_id", [])
                # the list runs on over the days and stops at ten, so two
                # stations ticked answer the ten earliest departures the
                # stations alone give between them, each with its trip
                alone_pairs = sorted(set(
                    (at, trip) for a in alone
                    for at, trip in zip((a or {}).get("next_departures", []),
                                        (a or {}).get("next_departures_trip_id", []))))
                expected = alone_pairs[:len(trips) or 10]
                got = list(zip((both or {}).get("next_departures", []), trips))
                check.note(got == expected,
                           f"{where}: {len(got)} departures {got[:3]}, the stations alone "
                           f"give {len(expected)} {expected[:3]}")

                kinds = (both or {}).get("next_departures_route_types", [])
                wanted = [gtfs_helper.RAIL_REPLACEMENT_BUS
                          if first_call_at(schedule, t, origins).startswith(prefix)
                          else line_type for t in trips]
                check.note(kinds == wanted,
                           f"{where}: route types {kinds}, expected {wanted}")
                coaches_read += wanted.count(gtfs_helper.RAIL_REPLACEMENT_BUS)

                dates = [get_next_service_date(schedule, o[0], d[0], day, "2",
                                               line=short_name) for o, d in singles]
                date = get_next_service_date(schedule, origins[0], destinations[0], day, "2",
                                             line=short_name, origin_names=origins,
                                             dest_names=destinations)
                earliest = min((d for d in dates if d), default=None)
                check.note(date == earliest,
                           f"{where}: next service {date}, the stations alone "
                           f"give {dates}")

                single = [stations.has_train_trip_between(schedule, o, d, short_name)
                          for o, d in singles]
                multi = stations.has_train_trip_between(schedule, origins, destinations,
                                                           short_name)
                check.note(multi == any(single),
                           f"{where}: trip test {multi}, the stations alone give {single}")
    if coach_line:
        check.note(coaches_read > 0,
                   "the line has coach stops but no departure read left from one")

    # the picker's labels: every station of the route with the modes calling
    # there, both directions, when the route mixes them; nothing otherwise
    called = {}
    for d in directions_of(schedule, route_id):
        for pattern in patterns_of(schedule, route_id, d):
            for stop in pattern:
                called.setdefault(fx.stop_names[stop], set()).add(
                    "coach" if stop.startswith(prefix) else "train")
    mixed = set().union(*called.values()) == {"train", "coach"} if called else False
    modes = stations.get_station_modes(schedule, route_id)
    check.note(modes == (called if mixed else {}),
               f"station modes {modes}, the trips call at {called}")


def check_train_destinations(check, fx, route_id, direction):
    """The arrival screen of the train flow. From each station this route's
    trips call at in this direction, it offers every station a trip of the
    line calls at after it, once, and nothing else; each with the modes it
    is reached by, a coach stop being a coach. The line is the route's code,
    so every route sharing it counts, both ways, the way the departures are
    held to it; with no code, the route alone."""
    schedule = fx.schedule
    short_name = fx.route_short_names[route_id]
    # the code the flow stores: None for a line the feed names by its long
    # name alone, which then holds to its own route. Taken as it was, None
    # made every nameless rail route of the feed one line (Metro-North,
    # Amtrak: the stations of the others counted as missing)
    code = short_name if short_name is not None and str(short_name).strip() else None
    prefix = gtfs_helper.COACH_STOP_PREFIX
    line_routes = [r for r, name in fx.route_short_names.items()
                   if name == code
                   and int(fx.route_types[r]) in gtfs_helper.RAIL_ROUTE_TYPES] if code else [route_id]

    def ridden(routes):
        reached = {}
        for r in routes:
            for d in directions_of(schedule, r):
                for pattern, trip_ids in patterns_of(schedule, r, d).items():
                    names = [fx.stop_names[s] for s in pattern]
                    for i, origin in enumerate(names):
                        if not fx.boards(trip_ids, pattern[i]):
                            continue
                        for j in range(i + 1, len(names)):
                            if names[j] != origin and fx.alights(trip_ids, pattern[j]):
                                reached.setdefault(origin, {}).setdefault(
                                    names[j], set()).add(
                                    "coach" if pattern[j].startswith(prefix) else "train")
        return reached

    by_line, by_route = ridden(line_routes), ridden([route_id])
    origins = sorted({fx.stop_names[s]
                      for p in patterns_of(schedule, route_id, direction) for s in p})
    for origin in origins:
        for line, ridden_from in ((code, by_line), (None, by_route)):
            offered = stations.get_train_destination_list(
                schedule, route_id, origin, line)
            expected = ridden_from.get(origin, {})
            missing = sorted(set(expected) - set(offered))
            extra = sorted(set(offered) - set(expected))
            modes = sorted(n for n in set(offered) & set(expected)
                           if offered[n] != expected[n])
            check.note(offered == expected,
                       f"from {origin} (line {line}): {len(offered)} offered, "
                       f"{len(expected)} ridden to; missing {missing}, extra {extra}, "
                       f"modes differ at {modes}")


def test_variants_disagree_excuses_only_a_pair_another_ride_runs_the_lists_way():
    # list order 0 1 2 3; this ride meets 2 before 1
    assert variants_disagree([0, 2, 1, 3], [[0, 1, 2, 3]]) == [(2, 1)]
    # no other ride runs 1 before 2: the step is the list's fault
    assert variants_disagree([0, 2, 1, 3], [[0, 2, 3]]) is None
    assert variants_disagree([0, 2, 1, 3], []) is None
    # a ride in the list's order has nothing to excuse
    assert variants_disagree([0, 1, 2, 3], []) == []
