"""The train side of the config flow: stations instead of stops.

A train entry is configured by station name, not by stop_id, because the
feed files one record per platform and the same station under several ids.
So the flow asks which stations a line calls at (get_station_list), which
of them a train reaches from a given one (get_train_destination_list),
whether any train runs between two names (has_train_trip_between), and
whether a line mixes coaches and trains (get_station_modes), and which
lines an entry rides, for its map files (train_entry_routes), and which
stations each line serves of those asked (train_line_ends). The boarding
rules and the station-name matching they lean on are stop_rules', where
the departure query reads them too.
"""
from __future__ import annotations

from collections.abc import Mapping
import csv
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any
import zipfile

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import text

from .gtfs_db import real_path
from .gtfs_filter import table_rows
from .stop_rules import (COACH_STOP_PREFIX, RAIL_ROUTE_TYPES, RAIL_ROUTE_TYPES_SQL, _alights, _boards,
                         entry_lines, entry_stations, line_codes_where, station_names_in)

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


class RailIndex:
    """The trains of a feed, read from its zip: what the station screens
    ask from the stations first, before the lines are imported.

    A source holds the lines its sensors asked for, not the network: the
    stations and the lines of every train come from the zip kept beside
    it. Only what those screens read is kept (the rail routes, their trips,
    their calls with who gets on and off, the stops' names), in an
    in-memory database the functions below query as they query a source.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine


def rail_index(zip_path: str) -> RailIndex | None:
    """The RailIndex of a feed's zip, None when it cannot be read. Blocking,
    one pass over stop_times.txt: for the executor."""
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    try:
        with zipfile.ZipFile(zip_path) as zin:
            routes = [(r["route_id"], r.get("route_short_name"), r.get("route_long_name"), r.get("route_type"))
                      for r in table_rows(zin, "routes.txt") if _is_rail_type(r.get("route_type"))]
            rail = {r[0] for r in routes}
            trips = [(t["trip_id"], t["route_id"]) for t in table_rows(zin, "trips.txt")
                     if t.get("route_id") in rail]
            riding = {t[0] for t in trips}
            calls = [(c["trip_id"], c["stop_id"], int(c.get("stop_sequence") or 0),
                      c.get("pickup_type") or None, c.get("drop_off_type") or None)
                     for c in table_rows(zin, "stop_times.txt") if c.get("trip_id") in riding]
            called = {c[1] for c in calls}
            stops = [(s["stop_id"], s.get("stop_name")) for s in table_rows(zin, "stops.txt")
                     if s.get("stop_id") in called]
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, csv.Error) as ex:
        _LOGGER.warning("Could not read the trains of %s: %s", zip_path, ex)
        return None
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.executescript("""
            create table routes (route_id text, route_short_name text, route_long_name text,
                                 route_type integer);
            create table trips (trip_id text, route_id text);
            create table stop_times (trip_id text, stop_id text, stop_sequence integer,
                                     pickup_type integer, drop_off_type integer);
            create table stops (stop_id text, stop_name text);
        """)
        cur.executemany("insert into routes values (?, ?, ?, ?)", routes)
        cur.executemany("insert into trips values (?, ?)", trips)
        cur.executemany("insert into stop_times values (?, ?, ?, ?, ?)", calls)
        cur.executemany("insert into stops values (?, ?)", stops)
        cur.executescript("""
            create index rail_trips_route on trips (route_id);
            create index rail_trips_trip on trips (trip_id);
            create index rail_calls_trip on stop_times (trip_id);
            create index rail_calls_stop on stop_times (stop_id);
            create index rail_stops_name on stops (stop_name);
        """)
        raw.commit()
    finally:
        raw.close()
    _LOGGER.debug("Rail index of %s: %s lines, %s trips, %s calls", zip_path, len(routes), len(trips), len(calls))
    return RailIndex(engine)


def _is_rail_type(value: str | None) -> bool:
    try:
        return int(str(value).strip()) in RAIL_ROUTE_TYPES
    except ValueError:
        return False


def get_train_routes_between(schedule: Schedule | RailIndex, origin_name: str, destination_name: str,
                             board_also: list[str], alight_also: list[str]) -> list[str]:
    """The route ids of the rail trips riding from the departure or a
    station on the way to the arrival or a station on the way, the real
    departure or arrival at one end at least: the lines a journey on every
    line imports, those it may hold to on its options screen."""
    origin_in, params = station_names_in("origin", [origin_name, *board_also])
    dest_in, dest_params = station_names_in("dest", [destination_name, *alight_also])
    params.update(dest_params)
    sql = f"""
    SELECT distinct t.route_id
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
        and d.stop_sequence > o.stop_sequence
    inner join stops sd on sd.stop_id = d.stop_id
    where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
      and so.stop_name in {origin_in}
      and sd.stop_name in {dest_in}
      and (so.stop_name = :origin or sd.stop_name = :destination)
      and {_boards("o")} and {_alights("d")}
    order by t.route_id
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {**params, "origin": origin_name,
                                        "destination": destination_name}).fetchall()
    return [str(r[0]) for r in rows]


def train_routes_both_ways(schedule: Schedule | RailIndex, origin_name: str,
                           destination_name: str) -> list[str]:
    """The route ids the trains between two stations picked first ride,
    either way round, any station between boarded or got off at: what its
    source has to hold before the options screen and the return read it.

    Both ways, since the return is the journey's mirror and a line may run
    one way only under its code (SNCF: K8+ out, P8 back). The ones riding
    the way asked come first: an import stops at the first line that
    fails, and those are the ones the journey cannot do without.
    """
    wanted: list[str] = []
    for start, end in ((origin_name, destination_name), (destination_name, origin_name)):
        between = get_train_stations_between(schedule, start, end)
        for route_id in get_train_routes_between(schedule, start, end, between, between):
            if route_id not in wanted:
                wanted.append(route_id)
    return wanted


def has_train_trip_between(schedule: Schedule | RailIndex, origin_name: str | list[str],
                           destination_name: str | list[str],
                           line: str | list[str] | None = None) -> bool:
    """Whether any rail trip serves both ends, in this order.

    The train path works with station names rather than stop ids, matched
    the way get_next_departure does: on the exact name, at any of the
    stations the entry ticked at each end. Each end is a name or a list of
    them. Held to the lines the flow picked, a code or a list of them,
    like the departures themselves; every rail line when none.
    """
    origin_names = [origin_name] if isinstance(origin_name, str) else origin_name
    destination_names = ([destination_name] if isinstance(destination_name, str)
                         else destination_name)
    origin_in, params = station_names_in("origin", origin_names)
    dest_in, dest_params = station_names_in("dest", destination_names)
    params.update(dest_params)
    line_where, line_params = line_codes_where("r.route_short_name", line)
    params.update(line_params)
    sql = f"""
    SELECT 1
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
    inner join stops sd on sd.stop_id = d.stop_id
    where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
      and so.stop_name in {origin_in}
      and sd.stop_name in {dest_in}
      and o.stop_sequence < d.stop_sequence
      and {_boards("o")} and {_alights("d")}
      {line_where}
    limit 1
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()
    _LOGGER.debug("Train trip between %s and %s (line %s): %s",
                  origin_names, destination_names, line, bool(row))
    return bool(row)


def get_station_list(schedule: Schedule | RailIndex, route_id: str | None = None) -> list[str]:
    """List the distinct stop names, for feeds where stop ids are unusable.

    A station shows up in GTFS as several stops, one per platform or mode, so
    the ids cannot be offered as they are. The names repeat instead: on a
    regional rail feed, 925 stops come down to 379 names.

    Held to a route, the list is where its trains take riders on: a station
    every train of the line passes, or only sets down at (a night train's
    morning stops), is nowhere to get on (see _boards). Without one, where
    a train of any rail line does: the stations picked first.
    """
    _LOGGER.debug("Getting station list for route: %s", route_id)
    if route_id:
        # read from the line's trips, through the trips(route_id) index
        # check_datasource_index gives every datasource: asked of every
        # stop of the network whether one of the line's trips calls there,
        # the SNCF took 1.3 to 2.1 s a line, IDFM 1 to 1.3 s
        sql = f"""
        SELECT distinct s.stop_name
        from trips t
        inner join stop_times st on st.trip_id = t.trip_id
        inner join stops s on s.stop_id = st.stop_id
        where t.route_id = :route_id
          and {_boards("st")}
        order by s.stop_name
        """  # noqa: S608
    else:
        sql = f"""
        SELECT distinct s.stop_name
        from trips t
        inner join routes r on r.route_id = t.route_id
        inner join stop_times st on st.trip_id = t.trip_id
        inner join stops s on s.stop_id = st.stop_id
        where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
          and {_boards("st")}
        order by s.stop_name
        """  # noqa: S608
    with schedule.engine.connect() as conn:
        # bound, not inlined: a route_id is the feed's own text, and one
        # carrying a quote ("L'Express") would end the literal and the screen
        rows = conn.execute(text(sql), {"route_id": str(route_id)}).fetchall()
    stations = [r[0] for r in rows if r[0]]
    _LOGGER.debug("Stations returned: %s", len(stations))
    return stations


def _stop_mode(stop_id: str) -> str:
    """"coach" or "train": the mode an SNCF stop serves, told by its id
    alone (COACH_STOP_PREFIX)."""
    return "coach" if str(stop_id).startswith(COACH_STOP_PREFIX) else "train"


def get_station_modes(schedule: Schedule | RailIndex, route_id: str | None) -> dict[str, set[str]]:
    """{station name: {"train", "coach"}} for the stations a rail route calls
    at, when its trips mix trains and coaches; {} on a line of one mode.

    The train flow offers names, and a coach station the feed names on its
    own ("Paris-Austerlitz Routiere") does not read as one, nor does a name
    both modes share ("Orleans") say that coaches call there too. The mode is
    read from the stop, the only place the feed says it (COACH_STOP_PREFIX).
    """
    if not route_id:
        return {}
    sql = """
    SELECT distinct s.stop_name, s.stop_id
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    inner join stops s on s.stop_id = st.stop_id
    where t.route_id = :route_id
    """
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"route_id": str(route_id)}).fetchall()
    modes: dict[str, set[str]] = {}
    for name, stop_id in rows:
        if name:
            modes.setdefault(name, set()).add(_stop_mode(stop_id))
    mixed = set().union(*modes.values()) == {"train", "coach"} if modes else False
    _LOGGER.debug("Station modes for route %s: %s", route_id, modes if mixed else "one mode")
    return modes if mixed else {}


def get_line_code(schedule: Schedule, route_id: str | None) -> str | None:
    """The code a train entry holds its departures to: the route_short_name
    of the route picked, None when the feed gives it none.

    Not the label the route screen shows: that one falls back to the long
    name when the short one is empty (Metro-North "New Haven", Amtrak
    "Wolverine"), which no route_short_name equals, and every station of
    those lines then led nowhere.
    """
    with schedule.engine.connect() as conn:
        row = conn.execute(text("SELECT route_short_name FROM routes WHERE route_id = :route_id"),
                           {"route_id": str(route_id or "")}).fetchone()
    code = row[0] if row else None
    return code if code is not None and str(code).strip() else None


def get_train_destination_list(schedule: Schedule | RailIndex, route_id: str | None, origin_name: str,
                               line: str | list[str] | None = None) -> dict[str, set[str]]:
    """{station name: {"train", "coach"}} for the stations a trip of the line
    really reaches from the departure station, and by which of the two.

    The arrival screen of the train flow offers only these: the departures
    are matched on the exact name and the line, so a station no trip rides
    to from the departure could never answer. A trip rides one mode from end
    to end (SNCF: no trip mixes coach and train stops), so a train station
    leads to the train arrivals and a coach station to the coach ones. The
    trips are held to the line's code like the departures, to the route
    itself when the line has none, and to every rail line without a line
    or a route (the journey that rides them all).
    """
    line_where, line_params = line_codes_where("r.route_short_name", line)
    if line_where:
        scope = line_where[len("AND "):]
    elif route_id:
        scope = "t.route_id = :route_id"
    else:
        scope = "1=1"
    sql = f"""
    SELECT distinct sd.stop_name, sd.stop_id
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
        and d.stop_sequence > o.stop_sequence
    inner join stops sd on sd.stop_id = d.stop_id
    where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
      and so.stop_name = :origin
      and {scope}
      and {_boards("o")} and {_alights("d")}
    """  # noqa: S608
    params = {"origin": origin_name, "route_id": str(route_id or ""), **line_params}
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    reached: dict[str, set[str]] = {}
    for name, stop_id in rows:
        if name and name != origin_name:
            reached.setdefault(name, set()).add(_stop_mode(stop_id))
    _LOGGER.debug("Train destinations from %s (line %s, route %s): %s",
                  origin_name, line, route_id, len(reached))
    return dict(sorted(reached.items()))


def get_train_stations_between(schedule: Schedule | RailIndex, origin_name: str, destination_name: str,
                               line: str | list[str] | None = None) -> list[str]:
    """The stations strictly between the departure and the arrival, on the
    rail trips that ride from one to the other: where the options screen
    offers to get on, or off, as well. A week of works can end some trains
    short of the station (SNCF, October 2026: K8+ and K6+ trains from Les
    Aubrais, not Orleans); boarding at Les Aubrais too keeps them."""
    line_where, params = line_codes_where("r.route_short_name", line)
    sql = f"""
    SELECT distinct sm.stop_name
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
        and d.stop_sequence > o.stop_sequence
    inner join stops sd on sd.stop_id = d.stop_id
    inner join stop_times m on m.trip_id = t.trip_id
        and m.stop_sequence > o.stop_sequence and m.stop_sequence < d.stop_sequence
    inner join stops sm on sm.stop_id = m.stop_id
    where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
      and so.stop_name = :origin
      and sd.stop_name = :destination
      and {_boards("o")} and {_alights("d")}
      {line_where}
    order by sm.stop_name
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"origin": origin_name, "destination": destination_name,
                                        **params}).fetchall()
    between = [r[0] for r in rows if r[0] and r[0] not in (origin_name, destination_name)]
    _LOGGER.debug("Stations between %s and %s (lines %s): %s",
                  origin_name, destination_name, line, len(between))
    return between


def get_train_lines_between(schedule: Schedule | RailIndex, origin_name: str, destination_name: str,
                            board_also: list[str], alight_also: list[str]) -> dict[str, str]:
    """{line code: its long names} of the rail trips riding from the
    departure or a station boarded as well to the arrival or a station got
    off at as well, the real departure or the real arrival at one end at
    least: the lines the options screen offers to hold the journey to. A
    code alone says nothing to most riders ("K8+"), its long name does
    ("Paris - Orleans"); one code may name several routes (SNCF P8)."""
    origin_in, params = station_names_in("origin", [origin_name, *board_also])
    dest_in, dest_params = station_names_in("dest", [destination_name, *alight_also])
    params.update(dest_params)
    sql = f"""
    SELECT distinct r.route_short_name, r.route_long_name
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
        and d.stop_sequence > o.stop_sequence
    inner join stops sd on sd.stop_id = d.stop_id
    where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
      and so.stop_name in {origin_in}
      and sd.stop_name in {dest_in}
      and (so.stop_name = :origin or sd.stop_name = :destination)
      and {_boards("o")} and {_alights("d")}
    order by r.route_short_name, r.route_long_name
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {**params, "origin": origin_name,
                                        "destination": destination_name}).fetchall()
    names: dict[str, list[str]] = {}
    for code, long_name in rows:
        if code is None or not str(code).strip():
            continue
        found = names.setdefault(str(code), [])
        if long_name and str(long_name).strip() and str(long_name).strip() not in found:
            found.append(str(long_name).strip())
    lines = {code: " / ".join(found) for code, found in names.items()}
    _LOGGER.debug("Lines from %s to %s: %s", origin_name, destination_name, lines)
    return lines


def train_line_ends(schedule: Schedule, origin_names: list[str], destination_names: list[str],
                    line: str | None) -> tuple[list[str], list[str]]:
    """(the departure stations, the arrival stations) one line serves of
    those asked, in the order asked: where a train of it takes riders on
    for one of the arrivals, and where one sets them down coming from one
    of the departures. ([], []) when no train of it rides between them;
    line None for every rail line, a line the feed gives no code.

    One sensor a line ticked, each with the stations its own trains call
    at: a line that never leaves from one of the stations ticked does not
    name it. Some days count: the K6+ mostly comes from Tours by Les
    Aubrais, and twelve of its trips in the feed of October 2026 leave
    from Orleans.
    """
    origin_in, params = station_names_in("origin", origin_names)
    dest_in, dest_params = station_names_in("dest", destination_names)
    params.update(dest_params)
    line_where, line_params = line_codes_where("r.route_short_name", line)
    params.update(line_params)
    sql = f"""
    SELECT distinct so.stop_name, sd.stop_name
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
        and d.stop_sequence > o.stop_sequence
    inner join stops sd on sd.stop_id = d.stop_id
    where r.route_type in ({RAIL_ROUTE_TYPES_SQL})
      and so.stop_name in {origin_in}
      and sd.stop_name in {dest_in}
      and {_boards("o")} and {_alights("d")}
      {line_where}
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    boarded = {row[0] for row in rows}
    reached = {row[1] for row in rows}
    return ([name for name in origin_names if name in boarded],
            [name for name in destination_names if name in reached])


def train_entry_routes(gtfs_dir: str, data: Mapping[str, Any]) -> list[str]:
    """The lines a trip of which runs from one of a train entry's stations
    to one of the other's, read from its source's database; [] when it
    cannot be read.

    A train entry stores "train" for its line and rides whatever line
    serves its two stations: the map files its departures wrote are named
    after those lines, and removing the entry has to find them. Blocking,
    made for the executor.
    """
    db_file = real_path(gtfs_dir, data.get("file") or "")
    if not data.get("file") or not os.path.exists(db_file):
        return []
    origin_in, params = station_names_in("origin", entry_stations(data, "origin"))
    dest_in, dest_params = station_names_in("dest", entry_stations(data, "destination"))
    params.update(dest_params)
    # the lines the entry holds to, as its departures do: a line the entry
    # leaves out wrote no file of its, and may be another entry's
    line_where, line_params = line_codes_where("r.route_short_name", entry_lines(data))
    params.update(line_params)
    sql = f"""
    select distinct t.route_id from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
    inner join stops sd on sd.stop_id = d.stop_id
    where so.stop_name in {origin_in} and sd.stop_name in {dest_in}
      and o.stop_sequence < d.stop_sequence
      {line_where}
    """  # noqa: S608
    try:
        conn = sqlite3.connect(db_file, timeout=10)
        try:
            return [str(row[0]) for row in conn.execute(sql, params)]
        finally:
            conn.close()
    except sqlite3.Error as ex:
        _LOGGER.warning("Could not read the lines of train entry %s: %s", data.get("name"), ex)
        return []
