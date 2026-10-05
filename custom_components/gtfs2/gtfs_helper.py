"""The departures of a journey entry: the query (_fetch_departure_rows), its
reading (_interpret_departure_rows), and the next departure a sensor shows
(get_next_departure)."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import datetime
import json
import re
import logging
import threading
from typing import TYPE_CHECKING, Any
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text


from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .clocks import (_day_offset, _on_service_day, _removed_on, _row_instant, _runs_on, agency_zone,
                     row_zone, zone_of)
from .const import (
    id_of,
    CONF_DESTINATION_STATIONS,
    CONF_ORIGIN_STATIONS,
    DEFAULT_PATH,
    )
from .datasource import check_extracting
from .rt_feed import on_service_day
from .stop_rules import (COACH_STOP_PREFIX, RAIL_ROUTE_TYPES, RAIL_ROUTE_TYPES_SQL, _alights, _boards,
                         _no_call_between, _place_group, _place_group_of, entry_lines, entry_stations,
                         line_codes_where, station_names_in, stop_ids_in)

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


# GTFS extended route type: Rail Replacement Bus Service
RAIL_REPLACEMENT_BUS = 714


def departure_route_type(route_type: str | int | None, origin_stop_id: str | None) -> str | int | None:
    """The route_type of one departure: its line's, unless the line is rail
    and the departure leaves from a coach stop, which makes it a rail
    replacement bus."""
    try:
        rail = route_type is not None and int(route_type) in RAIL_ROUTE_TYPES
    except (TypeError, ValueError):
        return route_type
    if rail and str(origin_stop_id or "").startswith(COACH_STOP_PREFIX):
        return RAIL_REPLACEMENT_BUS
    return route_type


# How far ahead get_next_service_date is allowed to look. A route that has not
# run for three months is not "resuming later", it is out of the feed, and an
# unbounded scan would walk the whole calendar to say so.
NEXT_SERVICE_HORIZON_DAYS = 90


def get_next_service_date(schedule: Schedule | str | None, origin_id: str, dest_id: str,
                          from_date: str, route_type: str = "3",
                          horizon: int = NEXT_SERVICE_HORIZON_DAYS, line: str | list[str] | None = None,
                          origin_names: list[str] | None = None,
                          destination_names: list[str] | None = None, route: str | None = None,
                          direction: str | int | None = None) -> str | None:
    """Return the first date on or after from_date that this trip runs, or None.

    include_tomorrow only ever reaches J+1, so a line that rests over the
    weekend or a holiday leaves the sensor blank with nothing to show. This
    answers the question the user actually asks in that gap: not "is there a
    bus today", but "when is the next one".

    Both calendar shapes are read, because feeds use either: calendar holds
    weekday flags over a validity window, calendar_dates holds explicit
    additions and removals. TAO publishes everything through calendar_dates
    with every weekday flag at 0, so reading calendar alone would find nothing.

    Returns a plain 'YYYY-MM-DD' string, and None when no service is found
    within horizon: a route can legitimately have no trips left at all.

    For a train, origin_id and dest_id are station names; origin_names and
    destination_names, when given, are every station the entry ticked at each end,
    and line holds the answer to the line the flow picked, or to the lines
    ticked, as the departures are held to them.

    route and direction hold the answer to the entry's line and, at a
    loop's terminus, its way round, as the departures are held to them:
    without them a day this line rests but another line serves the same
    two places read as a day it runs, and the sensor announced a service
    that does not exist.
    """
    # the coordinator calls this with whatever get_gtfs returned, which is a
    # sentinel string or None when the datasource is unusable. Matched by
    # shape, not by class: anything schedule-shaped may query
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("No usable schedule to look up the next service date (%s)", schedule or "empty")
        return None
    # the trips the departures are read among (_departure_candidates),
    # read once for a schedule and a pair whichever of the two asks first
    candidates_sql, params, _origin = _departure_candidates(
        route_type, origin_id, dest_id, direction, route, line,
        origin_names, destination_names)

    sql = f"""
        with recursive dates(d) as (
            select date(:from_date)
            union all
            select date(d, '+1 day') from dates
            where d < date(:from_date, :horizon)
        ),
        serving as (
            select distinct service_id from ({_CANDIDATES_FED})
        )
        select min(dates.d) from dates
        where exists (
            select 1 from serving s
            inner join calendar cal on cal.service_id = s.service_id
            where cal.start_date <= dates.d and cal.end_date >= dates.d
              and {_runs_on("dates.d", "cal")}
              and not {_removed_on("s.service_id", "dates.d")})
        or exists (
            select 1 from serving s
            inner join calendar_dates cd on cd.service_id = s.service_id
            where cd.date = dates.d and cd.exception_type = 1)
    """  # noqa: S608

    try:
        candidates = _candidate_pairs(schedule, candidates_sql, params)
        with schedule.engine.connect() as conn:
            row = conn.execute(text(sql), {
                "candidates": candidates,
                "from_date": from_date,
                "horizon": f"+{int(horizon)} days",
            }).fetchone()
    except SQLAlchemyError as ex:
        # never let a lookup that only enriches an attribute break the update
        _LOGGER.warning("Could not determine next service date: %s", ex)
        return None

    result = row[0] if row else None
    _LOGGER.debug("Next service date for %s -> %s from %s: %s",
                  origin_id, dest_id, from_date, result)
    return str(result)[:10] if result else None


def _feed_now(schedule: Schedule, route: str | None = None, offset: int = 0) -> str:
    """This moment as the feed writes its clocks: in its agency's zone.

    The query lays the stored clocks on service days and compares them
    with now, and a clock is the local time where the network runs.
    SQLite's own 'now', 'localtime' is the zone of the process, often UTC
    in a container, and Home Assistant's is where the user lives: either
    way a network in another zone, or a process left on UTC, dropped or
    kept the wrong hours of departures. The route's agency first, the
    feed's first agency otherwise, Home Assistant's zone when the feed
    names none. Naive, as the query's own datetimes are. offset is the
    entry's walking time, in minutes: a departure sooner is out of reach.
    """
    zone = agency_zone(schedule, route)
    moment = dt_util.now() + datetime.timedelta(minutes=offset)
    if zone is not None:
        moment = moment.astimezone(zone)
    return moment.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _several_stops(origin_ids: list[str], destination_ids: list[str]) -> tuple[str, str, str, dict[str, str]]:
    """(origin where, destination where, ride where, their parameters) of a
    bus or tram entry getting on, or off, at more than one stop: every place
    of each end, each trip listed once, where the rider first gets on and
    last gets off, as a train through two stations is. The shortest ride
    still holds within one place: a loop calls at a place twice (Palm Bus
    21 at Gare SNCF de Cannes), and the rider makes the second call."""
    origin_in, params = stop_ids_in("origin", origin_ids)
    dest_in, dest_params = stop_ids_in("dest", destination_ids)
    params.update(dest_params)
    origins, ends = _place_group_of(origin_in), _place_group_of(dest_in)
    # one place a stop ticked, each read once: "the place of this call",
    # asked of every row, read the whole stops table each time
    origin_places = [_place_group_of(f":origin_stop_{n}") for n in range(len(origin_ids))]
    end_places = [_place_group_of(f":dest_stop_{n}") for n in range(len(destination_ids))]

    def same_place(call: str, other: str, places: list[str]) -> str:
        return "(" + " OR ".join(f"({call}.stop_id IN {p} AND {other}.stop_id IN {p})" for p in places) + ")"

    ride = f"""AND NOT EXISTS (
                SELECT 1 FROM stop_times between_stop
                WHERE between_stop.trip_id = trip.trip_id
                  AND between_stop.stop_sequence > origin_stop_time.stop_sequence
                  AND between_stop.stop_sequence < destination_stop_time.stop_sequence
                  AND (({same_place("origin_stop_time", "between_stop", origin_places)} AND {_boards("between_stop")})
                       OR ({same_place("destination_stop_time", "between_stop", end_places)}
                           AND {_alights("between_stop")})))
              AND NOT EXISTS (
                SELECT 1 FROM stop_times earlier
                WHERE earlier.trip_id = trip.trip_id
                  AND earlier.stop_sequence < origin_stop_time.stop_sequence
                  AND earlier.stop_id IN {origins}
                  AND NOT {same_place("origin_stop_time", "earlier", origin_places)}
                  AND {_boards("earlier")})
              AND NOT EXISTS (
                SELECT 1 FROM stop_times later
                WHERE later.trip_id = trip.trip_id
                  AND later.stop_sequence > destination_stop_time.stop_sequence
                  AND later.stop_id IN {ends}
                  AND NOT {same_place("destination_stop_time", "later", end_places)}
                  AND {_alights("later")})"""
    return ("AND origin_stop_time.stop_id IN " + origins,
            "AND destination_stop_time.stop_id IN " + ends, ride, params)


def _departure_candidates(route_type: str, origin: str, destination: str,
                          direction: str | int | None = None, route: str | None = None,
                          line: str | list[str] | None = None, origin_names: list[str] | None = None,
                          destination_names: list[str] | None = None,
                          ) -> tuple[str, dict[str, Any], str]:
    """(SQL, its parameters, the origin as matched) of the trips riding from
    one end of an entry to the other, whatever the time: what
    _fetch_departure_rows reads its departures among (_candidate_pairs).
    See _fetch_departure_rows for the arguments."""
    if route_type == "2":
        route_type_where = f"route.route_type in ({RAIL_ROUTE_TYPES_SQL})"
        # The station is matched on the exact name the flow offered. A prefix
        # match also boarded the rider at any station whose name extends the
        # asked one (Champagnole, Champagnole Paul-Emile Victor), whichever
        # departed first. Every station the entry ticked at each end counts,
        # the coach station its replacement coaches leave from included.
        start_station_id = str(origin)
        end_station_id = str(destination)
        origin_names = origin_names or [origin]
        destination_names = destination_names or [destination]
        origin_in, name_params = station_names_in("origin", origin_names)
        dest_in, dest_params = station_names_in("dest", destination_names)
        name_params.update(dest_params)
        start_station_where = f"AND origin_stop_time.stop_id in (select stop_id from stops where stop_name IN {origin_in})"
        end_station_where = f"AND destination_stop_time.stop_id in (select stop_id from stops where stop_name IN {dest_in})"
        # several stations at one end: a train calling at Orleans then at
        # Les Aubrais rode the pair twice, one row a station. It is read
        # where the rider first gets on and last gets off, at the time it
        # leaves the first. One station at each end reads as it always did
        shortest_ride_where = ""
        if len(set(origin_names)) > 1:
            shortest_ride_where += f"""
              AND NOT EXISTS (
                SELECT 1 FROM stop_times earlier
                WHERE earlier.trip_id = trip.trip_id
                  AND earlier.stop_sequence < origin_stop_time.stop_sequence
                  AND earlier.stop_id in (select stop_id from stops where stop_name IN {origin_in})
                  AND {_boards("earlier")})"""
        if len(set(destination_names)) > 1:
            shortest_ride_where += f"""
              AND NOT EXISTS (
                SELECT 1 FROM stop_times later
                WHERE later.trip_id = trip.trip_id
                  AND later.stop_sequence > destination_stop_time.stop_sequence
                  AND later.stop_id in (select stop_id from stops where stop_name IN {dest_in})
                  AND {_alights("later")})"""
        # the train flow does not ask for a direction, and it stores the
        # picked lines' codes instead of a route id: the departures hold to
        # those lines, so stations shared by several lines do not mix theirs
        direction_where = ""
        route_where, line_params = line_codes_where("route.route_short_name", line)
        name_params.update(line_params)
        _LOGGER.debug("Setting up TRAIN Route for start/end : %s / %s, line: %s", start_station_id, end_station_id, line)
    else:
        route_type_where = "1=1"
        name_params = {}
        start_station_id = id_of(origin)
        end_station_id = id_of(destination)
        # both ends are matched on the whole place, every record of it: the
        # entry holds one record, the vehicle may call at another (the other
        # side of the road, the other quay of a terminus)
        origin_group = _place_group("origin_station_id")
        end_group = _place_group("end_station_id")
        start_station_where = "AND origin_stop_time.stop_id IN " + origin_group
        end_station_where = "AND destination_stop_time.stop_id IN " + end_group
        # a trip passing a place twice offers the pair twice (Palm Bus 21 calls
        # at Gare SNCF de Cannes on its way out and on its way back): the ride
        # is the shortest one, no other call at either end between the two.
        # Only a call the rider could use counts: at Kennington a trip calls
        # twice, the second time with no way on or off, and counted as one it
        # lost the trip altogether
        shortest_ride_where = "AND " + _no_call_between(
            "trip", "origin_stop_time", "destination_stop_time", origin_group, end_group)
        origin_ids = list(dict.fromkeys(id_of(name) for name in origin_names or [origin]))
        destination_ids = list(dict.fromkeys(id_of(name) for name in destination_names or [destination]))
        if len(origin_ids) > 1 or len(destination_ids) > 1:
            start_station_where, end_station_where, shortest_ride_where, name_params = _several_stops(
                origin_ids, destination_ids)
        direction_where = ("AND (trip.direction_id = :direction OR trip.direction_id IS NULL)"
                           if str(direction) in ("0", "1") else "")
        # a place is shared by every line calling at it: the entry's line only
        route_where = "AND trip.route_id = :route" if route else ""
        _LOGGER.debug("Setting up Route for start/end : %s / %s ", start_station_id, end_station_id)

    # the trips that ride from one end to the other, whatever the time: read
    # once for a database and a pair, then handed to the query (_candidate_pairs)
    candidates_sql = f"""
            SELECT trip.trip_id, trip.service_id,
                   {_day_offset("origin_stop_time.departure_time")} AS day_offset,
                   origin_stop_time.stop_id AS origin_stop_id,
                   destination_stop_time.stop_id AS destination_stop_id,
                   origin_stop_time.stop_sequence AS origin_stop_sequence,
                   destination_stop_time.stop_sequence AS destination_stop_sequence
            FROM trips trip
            INNER JOIN routes route ON route.route_id = trip.route_id
            INNER JOIN stop_times origin_stop_time ON trip.trip_id = origin_stop_time.trip_id
            INNER JOIN stop_times destination_stop_time ON trip.trip_id = destination_stop_time.trip_id
            WHERE {route_type_where}
              {start_station_where}
              {end_station_where}
              {direction_where}
              {route_where}
              {shortest_ride_where}
              AND origin_stop_time.stop_sequence < destination_stop_time.stop_sequence
              AND {_boards("origin_stop_time")}
              AND {_alights("destination_stop_time")}
              -- GTFS lets a call off the timepoints go untimed (Clemson leaves
              -- three calls in four so): no time, nothing to list
              AND origin_stop_time.arrival_time IS NOT NULL
              AND origin_stop_time.departure_time IS NOT NULL
              AND destination_stop_time.arrival_time IS NOT NULL
              AND destination_stop_time.departure_time IS NOT NULL
    """  # noqa: S608
    params = {
        "origin_station_id": start_station_id,
        "end_station_id": end_station_id,
        "direction": int(str(direction)) if str(direction) in ("0", "1") else None,
        "route": route,
        **name_params,
    }
    return candidates_sql, params, start_station_id


def _fetch_departure_rows(route_type: str, origin: str, destination: str, schedule: Schedule,
                          direction: str | int | None = None, route: str | None = None,
                          line: str | list[str] | None = None, origin_names: list[str] | None = None,
                          destination_names: list[str] | None = None,
                          window: tuple[str, str] | None = None,
                          limit: int = 30, offset: int = 0) -> tuple[list[dict[str, Any]], str]:
    """Run the static-GTFS SQL query and return matching rows as plain dicts.

    direction is only given by an entry at a loop's terminus
    (get_pair_direction, stored as loop_direction); the pair and the order of
    the stops decide it everywhere else, and the direction older entries
    store is not read. line, origin_names and destination_names belong to
    the train path: the line code the flow picked, or the codes ticked
    ([] or None for every rail line), and every station the entry ticked
    at each end.

    The sensor reads the next `limit` departures from now. The timetable
    export (write_timetable_file) reads whole service days instead:
    window is (first, last), two YYYY-MM-DD service dates, and every
    departure from now on those days comes back, `limit` then being a
    safeguard rather than the list's length. Without a window the query is
    the sensor's, unchanged."""
    candidates_sql, params, start_station_id = _departure_candidates(
        route_type, origin, destination, direction, route, line,
        origin_names, destination_names)
    window_where = "AND vd.date BETWEEN :window_first AND :window_last" if window else ""
    ## QUERY candidate_trips and cal_expand are used to construct a list of valida_dates, i.e a list where services run
    ## valid_dates is then used in the main query
    sql_query = f"""
       WITH RECURSIVE
          candidate_trips AS MATERIALIZED ({_CANDIDATES_FED}),
          -- the service days read start as far back as the latest departure
          -- of these trips asks for: a call at 48:10 leaves two days after
          -- its service day, at least yesterday
          back_days(n) AS (
            SELECT max(1, coalesce(max(day_offset), 0)) FROM candidate_trips
          ),
          cal_expand(service_id, d, end_date, monday, tuesday, wednesday, thursday, friday, saturday, sunday) AS (
            SELECT service_id, MAX(start_date, date(:now, '-' || (SELECT n FROM back_days) || ' days')), end_date,
                   monday, tuesday, wednesday, thursday, friday, saturday, sunday
            FROM calendar
            WHERE service_id IN (SELECT service_id FROM candidate_trips)
              -- a period over before the first day read gives no day: its
              -- first row would otherwise be that day, past its end_date
              AND end_date >= date(:now, '-' || (SELECT n FROM back_days) || ' days')
            UNION ALL
            SELECT service_id, date(d, '+1 day'), end_date, monday, tuesday, wednesday, thursday, friday, saturday, sunday
            FROM cal_expand
            WHERE d < end_date
          ),
          valid_dates AS MATERIALIZED (
            SELECT service_id, d AS date
            FROM cal_expand
            WHERE {_runs_on("d")}
            AND NOT {_removed_on("cal_expand.service_id", "cal_expand.d")}
            UNION
                SELECT cd2.service_id, cd2.date
                FROM calendar_dates cd2
                WHERE cd2.service_id IN (SELECT service_id FROM candidate_trips)
                  AND cd2.exception_type = 1
            )
        SELECT distinct trip.trip_id, trip.route_id, trip.trip_headsign, trip.direction_id, trip.trip_short_name,
               route.route_long_name, route.route_short_name,
               route.route_type AS route_type,
               start_station.stop_id as origin_stop_id,
               start_station.stop_name as origin_stop_name,
               start_station.stop_timezone as origin_stop_timezone,
               agency.agency_timezone as agency_timezone,
               time(origin_stop_time.arrival_time) AS origin_arrival_time,
               {_on_service_day("vd.date", "origin_stop_time.arrival_time")} AS origin_arrival_dt,
               time(origin_stop_time.departure_time) AS origin_depart_time,
			   {_on_service_day("vd.date", "origin_stop_time.departure_time")} AS origin_depart_dt,
               vd.date AS origin_depart_date,
               origin_stop_time.drop_off_type AS origin_drop_off_type,
               origin_stop_time.pickup_type AS origin_pickup_type,
               origin_stop_time.shape_dist_traveled AS origin_dist_traveled,
               origin_stop_time.stop_headsign AS origin_stop_headsign,
               origin_stop_time.stop_sequence AS origin_stop_sequence,
               origin_stop_time.timepoint AS origin_stop_timepoint,
               end_station.stop_id as dest_stop_id,
               end_station.stop_name as dest_stop_name,
               end_station.stop_timezone as dest_stop_timezone,
               time(destination_stop_time.arrival_time) AS dest_arrival_time,
               {_on_service_day("vd.date", "destination_stop_time.arrival_time")} AS dest_arrival_dt,
               time(destination_stop_time.departure_time) AS dest_depart_time,
               {_on_service_day("vd.date", "destination_stop_time.departure_time")} AS dest_depart_dt,
               destination_stop_time.drop_off_type AS dest_drop_off_type,
               destination_stop_time.pickup_type AS dest_pickup_type,
               destination_stop_time.shape_dist_traveled AS dest_dist_traveled,
               destination_stop_time.stop_headsign AS dest_stop_headsign,
               destination_stop_time.stop_sequence AS dest_stop_sequence,
               destination_stop_time.timepoint AS dest_stop_timepoint
        FROM candidate_trips ct
        INNER JOIN trips trip ON trip.trip_id = ct.trip_id
        INNER JOIN stop_times origin_stop_time ON origin_stop_time.trip_id = trip.trip_id AND origin_stop_time.stop_sequence = ct.origin_stop_sequence
        INNER JOIN stops start_station ON origin_stop_time.stop_id = start_station.stop_id
        INNER JOIN stop_times destination_stop_time ON destination_stop_time.trip_id = trip.trip_id AND destination_stop_time.stop_sequence = ct.destination_stop_sequence
        INNER JOIN stops end_station ON destination_stop_time.stop_id = end_station.stop_id
        INNER JOIN routes route ON route.route_id = trip.route_id
        INNER JOIN agency agency ON route.agency_id = agency.agency_id
        INNER JOIN valid_dates vd ON vd.service_id = trip.service_id
        WHERE {_on_service_day("vd.date", "origin_stop_time.departure_time")} >= datetime(:now)
          {window_where}
        ORDER BY vd.date, origin_stop_time.departure_time
        LIMIT {int(limit)};
    """  # noqa: S608

    query_params = {
        **params,
        "route_type": route_type,
        "window_first": window[0] if window else None,
        "window_last": window[1] if window else None,
        # this moment on the network's clock, see _feed_now
        "now": _feed_now(schedule, route, offset),
    }
    _LOGGER.debug("SQL statement:\n%s", sql_query)
    _LOGGER.debug("SQL parameters:\n%s", query_params)
    query_params["candidates"] = _candidate_pairs(schedule, candidates_sql, query_params)

    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), query_params).fetchall()

    return [row_cursor._asdict() for row_cursor in rows], start_station_id


# the columns of a candidate trip, in the order _candidate_pairs keeps them
_CANDIDATE_COLUMNS = ("trip_id", "service_id", "day_offset", "origin_stop_id",
                      "destination_stop_id", "origin_stop_sequence", "destination_stop_sequence")
_CANDIDATES_FED = "SELECT " + ", ".join(
    f"json_extract(value, '$[{n}]') AS {column}" for n, column in enumerate(_CANDIDATE_COLUMNS)
) + " FROM json_each(:candidates)"
# {(schedule id, query, parameters): (schedule, candidates as json)}, the
# latest last; the schedule is kept so its id is not reused while here
_CANDIDATES: dict[tuple[int, str, tuple[tuple[str, Any], ...]], tuple[Schedule, str]] = {}
_CANDIDATES_GUARD = threading.Lock()
_CANDIDATES_KEEP = 64


def _candidate_pairs(schedule: Schedule, candidates_sql: str, params: Mapping[str, Any]) -> str:
    """The trips riding from one end of a departure query to the other, as
    json for json_each, read once for a schedule and a pair.

    They do not depend on the time, and reading them was most of each
    reading of the timetable: 2 of 2.8 s on TAO tram A, every quarter of
    an hour and each time the departure shown left. The schedule is the
    same object as long as its database is the same one (schedule_for),
    so a new edition is read afresh.
    """
    names = sorted(set(re.findall(r":(\w+)", candidates_sql)))
    key = (id(schedule), candidates_sql, tuple((n, params.get(n)) for n in names))
    with _CANDIDATES_GUARD:
        found = _CANDIDATES.pop(key, None)
        if found is not None and found[0] is schedule:
            _CANDIDATES[key] = found
            return found[1]
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(candidates_sql), {n: params.get(n) for n in names}).fetchall()
    fed = json.dumps([list(row) for row in rows])
    with _CANDIDATES_GUARD:
        while len(_CANDIDATES) >= _CANDIDATES_KEEP:
            _CANDIDATES.pop(next(iter(_CANDIDATES)))
        _CANDIDATES[key] = (schedule, fed)
    return fed


def _departure_timetable(rows: Iterable[Mapping[str, Any]], now: datetime.datetime,
                         now_local_tz: datetime.datetime) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    """[((departure, trip_id), row)] of the rows not gone yet, in departure
    order, each row marked first or last of its service day."""
    timetable: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        depart_dt_str = row["origin_depart_dt"]    # already a correct full instant
        try:
            depart_dt = _row_instant(depart_dt_str, None)
        except ValueError:
            _LOGGER.warning("Could not parse departure datetime: %s", depart_dt_str)
            continue

        # already departed? The row's clock is the network's, so it is laid
        # in the network's zone before it is compared: read naive against
        # Home Assistant's clock, a network in another zone lost or kept
        # the wrong hour of departures
        zone = zone_of(row.get("agency_timezone"), row.get("origin_stop_timezone"))
        if zone is not None:
            if depart_dt.replace(tzinfo=zone) <= now_local_tz:
                continue
        elif depart_dt <= now:
            continue

        idx = (depart_dt_str, str(row["trip_id"]))
        if idx in timetable:
            # a trip reached from two quays of the origin: expected, kept once
            _LOGGER.debug("Duplicate timetable key: %s, trip_id: %s", idx, row["trip_id"])
            continue
        # the service day, a real ISO date beyond tomorrow
        timetable[idx] = {**row, "day": row["origin_depart_date"], "first": False, "last": False}

    ordered = sorted(timetable.items())
    last_of_day: dict[str, dict[str, Any]] = {}
    for _, value in ordered:
        if value["origin_depart_date"] not in last_of_day:
            value["first"] = True
        last_of_day[value["origin_depart_date"]] = value
    for value in last_of_day.values():
        value["last"] = True
    return ordered


def _departure_zones(hass: HomeAssistant,
                     item: Mapping[str, Any]) -> tuple[datetime.tzinfo | None, datetime.tzinfo | None]:
    """(origin zone, destination zone) a departure's clock is read in: the
    agency's at both ends, else the origin stop's, with the destination
    stop's at its end when the agency gives none; Home Assistant's when
    nothing is said."""
    if hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration")
    # as the departures service reads them (row_zone)
    timezone, timezone_dest = row_zone(item, "origin"), row_zone(item, "dest")
    _LOGGER.debug("Defined orig timezone: %s, dest timezone: %s", timezone, timezone_dest)
    return timezone, timezone_dest


def _next_departure_lists(upcoming: list[tuple[datetime.datetime, dict[str, Any]]],
                          timezone_dest: datetime.tzinfo | None) -> dict[str, list[Any]]:
    """The next_departures* lists of the sensor, one entry per departure of
    upcoming [(departure instant, row)], in its order."""
    lists: dict[str, list[Any]] = {key: [] for key in (
        "next_departures", "next_departures_lines", "next_departures_headsign",
        "next_departures_trip_id", "next_departures_destination_arrival_times",
        "next_departures_durations", "next_departures_origin_stop_id",
        "next_departures_route_types")}
    for departure, value in upcoming:
        # dest_arrival_dt is already the correct instant - no rollover guessing needed
        arrival = _row_instant(value["dest_arrival_dt"], timezone_dest)
        at = dt_util.as_utc(departure).isoformat()
        # a line named by its long name only read "None/<long name>";
        # a trip with no headsign, its destination given on each call,
        # read "None"
        line = "/".join(name for name in (value["route_short_name"], value["route_long_name"]) if name)
        headsign = value["trip_headsign"] or value.get("origin_stop_headsign")
        lists["next_departures"].append(at)
        lists["next_departures_lines"].append(f"{at} ({line})")
        lists["next_departures_headsign"].append(f"{at} ({headsign})")
        lists["next_departures_trip_id"].append(str(value["trip_id"]))
        lists["next_departures_destination_arrival_times"].append(dt_util.as_utc(arrival).isoformat())
        # both ends are known here, so serve the theoretical duration
        # ready-made rather than leaving every card to subtract the
        # paired lists themselves
        lists["next_departures_durations"].append(round((arrival - departure).total_seconds() / 60))
        # the record it leaves from: a place may be served from either
        lists["next_departures_origin_stop_id"].append(str(value.get("origin_stop_id")))
        # a train line may list a coach among its departures: each one
        # says what rides it, so a card can draw a bus for that one
        lists["next_departures_route_types"].append(
            departure_route_type(value.get("route_type"), value.get("origin_stop_id")))
    return lists


def _stop_time(item: Mapping[str, Any], end: str, arrival: datetime.datetime,
               departure: datetime.datetime) -> dict[str, Any]:
    """The origin_stop_time or destination_stop_time attribute of a
    departure, end being "origin" or "dest"."""
    return {
        "Arrival Time": dt_util.as_utc(arrival).isoformat(),
        "Departure Time": dt_util.as_utc(departure).isoformat(),
        "Drop Off Type": item[f"{end}_drop_off_type"],
        "Pickup Type": item[f"{end}_pickup_type"],
        "Shape Dist Traveled": item[f"{end}_dist_traveled"],
        "Headsign": item[f"{end}_stop_headsign"],
        "Sequence": item[f"{end}_stop_sequence"],
        "Timepoint": item[f"{end}_stop_timepoint"],
    }


def _interpret_departure_rows(hass: HomeAssistant, rows: Iterable[Mapping[str, Any]],
                               start_station_id: str | None, now: datetime.datetime,
                               now_local_tz: datetime.datetime) -> dict[str, Any]:
    """Turn raw SQL-shaped rows into the `next_departure` dict."""
    _LOGGER.debug("Interpret rows: %s", rows)
    timetable = _departure_timetable(rows, now, now_local_tz)
    if not timetable:
        # No departure to show. Keep returning an empty dict: callers test this
        # value for truth and then read the fields of a real departure, so a
        # non-empty "there is nothing" would be read as a departure and crash.
        # The date of the next service is published separately, by the
        # coordinator, through get_next_service_date.
        _LOGGER.debug("No items found in gtfs")
        return {}
    _LOGGER.debug("Departure(s) found for station %s @ %s -> %s", start_station_id, *timetable[0])
    timezone, timezone_dest = _departure_zones(hass, timetable[0][1])

    # the next ten, read again in the zone the first one set
    upcoming: list[tuple[datetime.datetime, dict[str, Any]]] = []
    for key, value in timetable:
        departure = _row_instant(key[0], timezone)
        if departure > now_local_tz:
            upcoming.append((departure, value))
            if len(upcoming) >= 10:
                break
    _LOGGER.debug("Timetable Remaining Departures on this Start/Stop: %s", upcoming)
    if not upcoming:
        # every departure found is already gone: the same empty dict as
        # when none was found, for the same callers
        _LOGGER.debug("No items found in gtfs")
        return {}

    depart_time, item = upcoming[0]
    arrival_time = _row_instant(item["dest_arrival_dt"], timezone_dest)
    return {
        "trip_id": item["trip_id"],
        "route_id": item["route_id"],
        "route_short_name": item["route_short_name"],
        "trip_direction_id": item["direction_id"],
        "trip_short_name": item["trip_short_name"],
        "day": item["day"],
        "first": item["first"],
        "last": item["last"],
        "origin_stop_id": item["origin_stop_id"],
        "origin_stop_sequence": item["origin_stop_sequence"],
        "origin_stop_name": item["origin_stop_name"],
        "departure_time": depart_time,
        "arrival_time": arrival_time,
        "duration": round((arrival_time - depart_time).total_seconds() / 60),
        "origin_stop_time": _stop_time(
            item, "origin", _row_instant(item["origin_arrival_dt"], timezone), depart_time),
        "origin_stop_timezone": item["origin_stop_timezone"],
        "destination_stop_time": _stop_time(
            item, "dest", arrival_time, _row_instant(item["dest_depart_dt"], timezone_dest)),
        "destination_stop_timezone": item["dest_stop_timezone"],
        "destination_stop_id": item["dest_stop_id"],
        "destination_stop_name": item["dest_stop_name"],
        **_next_departure_lists(upcoming, timezone_dest),
    }

def _departure_clocks(_data: Mapping[str, Any]) -> tuple[datetime.datetime, datetime.datetime]:
    """now (naive, offset applied) and now in the local zone, the way the
    departures are read against them."""
    offset = _data["offset"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_local_tz = dt_util.now() + datetime.timedelta(minutes=offset)
    return now, now_local_tz


def drop_departure_trips(hass: HomeAssistant, _data: Mapping[str, Any],
                         struck: Mapping[str, str | None]) -> dict[str, Any]:
    """The departures again, without the trips the realtime feed struck out.

    struck is {trip_id: start_date or None} as struck_trips reads it: a
    trip is dropped on the service day the feed names, and every day when
    it names none. Read from the rows the last static refresh kept, so the
    board moves on to the next trip that runs, with its own arrival,
    headsign and duration, rather than losing the head fields. Returns
    what get_next_departure would, {} when nothing is left.
    """
    rows = _data.get("departure_rows") or []
    if not struck or not rows:
        return _data.get("next_departure") or {}
    kept = [row for row in rows
            if str(row.get("trip_id")) not in struck
            or not on_service_day(struck[str(row.get("trip_id"))], row.get("origin_depart_date"))]
    if len(kept) == len(rows):
        return _data.get("next_departure") or {}
    _LOGGER.debug("Dropping %s struck departures out of %s", len(rows) - len(kept), len(rows))
    now, now_local_tz = _departure_clocks(_data)
    return _interpret_departure_rows(
        hass, kept, _data.get("departure_rows_origin"), now, now_local_tz)


def journey_data(schedule: Schedule | str | None, data: Mapping[str, Any],
                 options: Mapping[str, Any]) -> dict[str, Any]:
    """The journey an entry asks the departure query for: its two ends, its
    line and its source, from the entry's data and options. The sensor's
    refresh and the departures service both start from it, so they answer
    for the same journey."""
    return {
        "schedule": schedule,
        "origin": data["origin"],
        "destination": data["destination"],
        # a train entry's every station at each end, only on the entries
        # that ticked them: the others keep the shape they always had
        **{key: data[key] for key in (CONF_ORIGIN_STATIONS, CONF_DESTINATION_STATIONS)
           if data.get(key)},
        "offset": options["offset"] if "offset" in options else 0,
        "gtfs_dir": DEFAULT_PATH,
        "name": data["name"],
        "file": data["file"],
        "route_type": data["route_type"],
        "route": data["route"],
        # kept only at a loop's terminus, absent everywhere else
        "loop_direction": data.get("loop_direction"),
        # a train entry's line code: its departures hold to that line
        "line": data.get("line"),
        # or the codes ticked on its options screen, [] for every rail
        # line, only on the entries made with it: the others keep the
        # shape they always had
        **({"lines": data["lines"]} if "lines" in data else {}),
    }


def departure_query_args(_data: Mapping[str, Any]) -> dict[str, Any]:
    """What an entry's departures are asked with beyond its two ends, the
    same for the sensor and for the timetable export: the direction kept at
    a loop's terminus, the entry's line, and on the train path the line
    codes the entry holds to and every station ticked at each end."""
    return {
        "direction": _data.get("loop_direction"),
        "route": id_of(_data.get("route")) or None,
        "line": entry_lines(_data),
        "origin_names": entry_stations(_data, "origin"),
        "destination_names": entry_stations(_data, "destination"),
    }


def first_departure_row(data: Mapping[str, Any], from_day: str) -> dict[str, Any] | None:
    """The entry's first departure row on the service day from_day or
    after: the next day it runs at all, then its first departure that day;
    None when the calendar has none in its horizon."""
    args = departure_query_args(data)
    day = get_next_service_date(
        data["schedule"], id_of(data["origin"]), id_of(data["destination"]),
        from_day, data["route_type"], **args)
    if not day:
        return None
    # the rows come in time order: the first is the one
    rows, _origin = _fetch_departure_rows(
        data["route_type"], data["origin"], data["destination"], data["schedule"],
        window=(day, day), limit=1, **args)
    return rows[0] if rows else None


def shown_ends(data: Mapping[str, Any], departure: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """(route_id, direction, origin stop id, destination stop id) of the
    departure shown, the entry's own where the departure names none: once
    the last departure of the day is gone, the entry still says which line,
    way and stops the sensor follows."""
    return (
        str(departure.get("route_id") or id_of(data.get("route"))),
        # a direction of 0 is a real one: only a missing key falls back
        str(departure.get("trip_direction_id", data.get("direction"))),
        str(departure.get("origin_stop_id") or id_of(data.get("origin"))),
        str(departure.get("destination_stop_id") or id_of(data.get("destination"))),
    )


def get_next_departure(hass: HomeAssistant, _data: dict[str, Any]) -> dict[str, Any]:
    """Get next departures from data."""
    _LOGGER.debug("Get next departure with data: %s", _data)
    if check_extracting(hass, _data['gtfs_dir'],_data['file']):
        _LOGGER.debug("Cannot get next departures on this datasource as still unpacking: %s", _data["file"])
        return {}

    schedule = _data["schedule"]
    # get_gtfs hands back a sentinel string or None when the datasource is
    # unusable (zip or sqlite missing, dates all in the future): querying
    # that raises in SQLAlchemy, far from the cause, on every update.
    # Matched by shape, not by class: anything schedule-shaped may query
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no departures", _data["file"], schedule or "empty")
        return {}
    route_type = _data["route_type"]

    now, now_local_tz = _departure_clocks(_data)

    # Fetch all departures

    rows, start_station_id = _fetch_departure_rows(
        route_type, _data["origin"], _data["destination"], schedule,
        # the walking time is asked of the query: cut from its answer, it
        # left nothing of the next 30 departures of a frequent line
        offset=int(_data.get("offset") or 0), **departure_query_args(_data))
    # kept beside the departures: a realtime refresh that learns of a
    # cancelled trip reads them again without it (drop_departure_trips),
    # rather than showing the struck trip as on time until the next
    # static refresh
    _data["departure_rows"] = rows
    _data["departure_rows_origin"] = start_station_id

    return _interpret_departure_rows(hass, rows, start_station_id, now, now_local_tz)
