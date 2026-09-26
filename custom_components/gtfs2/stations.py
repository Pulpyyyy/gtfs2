"""The train side of the config flow: stations instead of stops.

A train entry is configured by station name, not by stop_id, because the
feed files one record per platform and the same station under several ids.
So the flow asks which stations a line calls at (get_station_list), which
of them a train reaches from a given one (get_train_destination_list),
whether any train runs between two names (has_train_trip_between), and
whether a line mixes coaches and trains (get_station_modes). The boarding
rules and the station-name matching they lean on stay in gtfs_helper,
where the departure query uses them too.
"""
from __future__ import annotations

import logging

from sqlalchemy.sql import text

from .gtfs_helper import COACH_STOP_PREFIX, RAIL_ROUTE_TYPES, RAIL_ROUTE_TYPES_SQL, _alights, _boards, station_names_in

_LOGGER = logging.getLogger(__name__)


def has_train_trip_between(schedule, origin_name, destination_name, line=None):
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


def get_station_list(schedule, route_id=None):
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


def get_station_modes(schedule, route_id):
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
    modes = {}
    for name, stop_id in rows:
        if name:
            modes.setdefault(name, set()).add(
                "coach" if str(stop_id).startswith(COACH_STOP_PREFIX) else "train")
    mixed = set().union(*modes.values()) == {"train", "coach"} if modes else False
    _LOGGER.debug("Station modes for route %s: %s", route_id, modes if mixed else "one mode")
    return modes if mixed else {}


def get_line_code(schedule, route_id):
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


def get_train_destination_list(schedule, route_id, origin_name, line=None):
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
    rail = ",".join(str(t) for t in RAIL_ROUTE_TYPES)
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
    where r.route_type in ({rail})
      and so.stop_name = :origin
      and {scope}
      and {_boards("o")} and {_alights("d")}
    """  # noqa: S608
    params = {"origin": origin_name, "line": line, "route_id": str(route_id or "")}
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    reached = {}
    for name, stop_id in rows:
        if name and name != origin_name:
            reached.setdefault(name, set()).add(
                "coach" if str(stop_id).startswith(COACH_STOP_PREFIX) else "train")
    _LOGGER.debug("Train destinations from %s (line %s, route %s): %s",
                  origin_name, line, route_id, len(reached))
    return dict(sorted(reached.items()))
