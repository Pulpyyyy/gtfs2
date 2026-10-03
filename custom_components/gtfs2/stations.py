"""The train side of the config flow: stations instead of stops.

A train entry is configured by station name, not by stop_id, because the
feed files one record per platform and the same station under several ids.
So the flow asks which stations a line calls at (get_station_list), which
of them a train reaches from a given one (get_train_destination_list),
whether any train runs between two names (has_train_trip_between), and
whether a line mixes coaches and trains (get_station_modes), and which
lines an entry rides, for its map files (train_entry_routes). The boarding
rules and the station-name matching they lean on are stop_rules', where
the departure query reads them too.
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any

from sqlalchemy.sql import text

from .gtfs_db import real_path
from .stop_rules import (COACH_STOP_PREFIX, RAIL_ROUTE_TYPES_SQL, _alights, _boards,
                         entry_stations, station_names_in)

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


def has_train_trip_between(schedule: Schedule, origin_name: str | list[str],
                           destination_name: str | list[str], line: str | None = None) -> bool:
    """Whether any rail trip serves both ends, in this order.

    The train path works with station names rather than stop ids, matched
    the way get_next_departure does: on the exact name, at any of the
    stations the entry ticked at each end. Each end is a name or a list of
    them. Held to one line when the flow picked one, like the departures
    themselves.
    """
    origin_names = [origin_name] if isinstance(origin_name, str) else origin_name
    destination_names = ([destination_name] if isinstance(destination_name, str)
                         else destination_name)
    origin_in, params = station_names_in("origin", origin_names)
    dest_in, dest_params = station_names_in("dest", destination_names)
    params.update(dest_params)
    line_where = "and r.route_short_name = :line" if line else ""
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
        row = conn.execute(text(sql), {**params, "line": line}).fetchone()
    _LOGGER.debug("Train trip between %s and %s (line %s): %s",
                  origin_names, destination_names, line, bool(row))
    return bool(row)


def get_station_list(schedule: Schedule, route_id: str | None = None) -> list[str]:
    """List the distinct stop names, for feeds where stop ids are unusable.

    A station shows up in GTFS as several stops, one per platform or mode, so
    the ids cannot be offered as they are. The names repeat instead: on a
    regional rail feed, 925 stops come down to 379 names.

    Held to a route, the list is where its trains take riders on: a station
    every train of the line passes, or only sets down at (a night train's
    morning stops), is nowhere to get on (see _boards).
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
        sql = "SELECT distinct s.stop_name from stops s order by s.stop_name"
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


def get_station_modes(schedule: Schedule, route_id: str | None) -> dict[str, set[str]]:
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


def get_train_destination_list(schedule: Schedule, route_id: str | None, origin_name: str,
                               line: str | None = None) -> dict[str, set[str]]:
    """{station name: {"train", "coach"}} for the stations a trip of the line
    really reaches from the departure station, and by which of the two.

    The arrival screen of the train flow offers only these: the departures
    are matched on the exact name and the line, so a station no trip rides
    to from the departure could never answer. A trip rides one mode from end
    to end (SNCF: no trip mixes coach and train stops), so a train station
    leads to the train arrivals and a coach station to the coach ones. The
    trips are held to the line's code like the departures, to the route
    itself when the line has none.
    """
    scope = "r.route_short_name = :line" if line else "t.route_id = :route_id"
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
    params = {"origin": origin_name, "line": line, "route_id": str(route_id or "")}
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    reached: dict[str, set[str]] = {}
    for name, stop_id in rows:
        if name and name != origin_name:
            reached.setdefault(name, set()).add(_stop_mode(stop_id))
    _LOGGER.debug("Train destinations from %s (line %s, route %s): %s",
                  origin_name, line, route_id, len(reached))
    return dict(sorted(reached.items()))


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
    sql = f"""
    select distinct t.route_id from trips t
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
    inner join stops sd on sd.stop_id = d.stop_id
    where so.stop_name in {origin_in} and sd.stop_name in {dest_in}
      and o.stop_sequence < d.stop_sequence
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
