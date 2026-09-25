"""Support for GTFS Integration."""
from __future__ import annotations

import datetime
import sqlite3
import re
import logging
import statistics
import os
import shutil
import pygtfs
from sqlalchemy.sql import text
import multiprocessing
import zipfile


import homeassistant.util.dt as dt_util
from homeassistant.helpers import entity_registry as er

from .direction_repair import repair_trip_directions
from .const import (
    CONF_API_KEY,
CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    CONF_ACCEPT_HEADER_PB,
    CONF_INNER_ZIP,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY,
    DEFAULT_LOCAL_STOP_RADIUS,
    DEFAULT_PATH_RT,
    DEFAULT_PATH,
    ICON,
    ICONS,
    DOMAIN,
    TIME_STR_FORMAT
    )
from .gtfs_rt_helper import (get_rt_route_trip_statuses, get_gtfs_rt, get_gtfs_feed_entities,
                             struck_trips, on_service_day)
from .gtfs_rt_helper import safe_file_part  # noqa: F401  a provider test reads it here
from .route_names import (get_routes_in_zip, _adds_to, _leave_out_expired, _look_alikes,
                          _natural, _route_label, _set_apart, _set_apart_by_ends,
                          _set_apart_by_span, look_alike_ends, route_ends, route_spans)
from .freshness import stage_zip, adopt_zip
from .gtfs_filter import feed_info_unreadable, zip_only_future_dates
from .feed_window import last_service_day
from .key_mask import fetch
from .rt_source import with_query_key

_LOGGER = logging.getLogger(__name__)


# How far ahead get_next_service_date is allowed to look. A route that has not
# run for three months is not "resuming later", it is out of the feed, and an
# unbounded scan would walk the whole calendar to say so.
NEXT_SERVICE_HORIZON_DAYS = 90

# The SNCF files the coaches that stand in for its trains under the train line
# itself, so route_type calls them rail. Only the stop tells them apart: a
# coach calls at "StopPoint:OCECar TER-87543009" where the train calls at
# "StopPoint:OCETrain TER-87543009", same station, same name. On the national
# feed of September 2026, 283 of the 582 rail lines carry such coaches, and no
# coach shares a single stop_id with a train. "Navette" is not one of them: it
# is the rail shuttle between Tours and Saint-Pierre-des-Corps.
COACH_STOP_PREFIX = "StopPoint:OCECar "
# GTFS extended route type: Rail Replacement Bus Service
RAIL_REPLACEMENT_BUS = 714
RAIL_ROUTE_TYPES = (2, *range(100, 118))
# the same, as the queries write it
RAIL_ROUTE_TYPES_SQL = ",".join(str(t) for t in RAIL_ROUTE_TYPES)


def departure_route_type(route_type, origin_stop_id):
    """The route_type of one departure: its line's, unless the line is rail
    and the departure leaves from a coach stop, which makes it a rail
    replacement bus."""
    try:
        rail = int(route_type) in RAIL_ROUTE_TYPES
    except (TypeError, ValueError):
        return route_type
    if rail and str(origin_stop_id or "").startswith(COACH_STOP_PREFIX):
        return RAIL_REPLACEMENT_BUS
    return route_type


def entry_stations(data, end):
    """Every station a train entry matches at one end, "origin" or
    "destination": the ones ticked on the station screen, or the single name
    an entry created before that screen took several holds."""
    names = data.get(f"{end}_stations") or [data.get(end)]
    return [str(name) for name in names if name]


def train_entry_routes(gtfs_dir, data):
    """The lines a trip of which runs from one of a train entry's stations
    to one of the other's, read from its source's database; [] when it
    cannot be read.

    A train entry stores "train" for its line and rides whatever line
    serves its two stations: the map files its departures wrote are named
    after those lines, and removing the entry has to find them. Blocking,
    made for the executor.
    """
    db_file = os.path.join(gtfs_dir, (data.get("file") or "") + ".sqlite")
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


def station_names_in(prefix, names):
    """An SQL "(:prefix_name_0, ...)" for a list of station names, and its
    parameters.

    A train entry may name several stations at one end: the station, and the
    coach station its replacement coaches leave from, which the feed files as
    a station of its own under another name (SNCF K8+: "Paris Austerlitz" for
    the trains, "Paris-Austerlitz Routiere" 240 m away for the coaches).
    Nothing in the feed links the two, so the rider ticks both.
    """
    names = [str(name) for name in names or [] if name] or [""]
    keys = [f"{prefix}_name_{n}" for n in range(len(names))]
    return "(" + ", ".join(f":{key}" for key in keys) + ")", dict(zip(keys, names))


def get_next_service_date(schedule, origin_id, dest_id, from_date, route_type="3",
                          horizon=NEXT_SERVICE_HORIZON_DAYS, line=None,
                          origin_names=None, dest_names=None, route=None,
                          direction=None):
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
    dest_names, when given, are every station the entry ticked at each end,
    and line holds the answer to the line the flow picked, as the departures
    are held to it.

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
    line_join = line_where = ""
    if route_type == "2":
        # trains match on the exact stop_name, like get_next_departure does
        origin_in, params = station_names_in("origin", origin_names or [origin_id])
        dest_in, dest_params = station_names_in("dest", dest_names or [dest_id])
        params.update(dest_params)
        origin_where = ("o.stop_id in (select stop_id from stops "
                        f"where stop_name in {origin_in})")
        dest_where = ("x.stop_id in (select stop_id from stops "
                      f"where stop_name in {dest_in})")
        # held to rail, as the departures are: two stations of one name can
        # also be served by a bus the train sensor never lists
        line_join = "inner join routes r on r.route_id = t.route_id"
        line_where = f"and r.route_type in ({RAIL_ROUTE_TYPES_SQL})"
        if line:
            # without it, a day the line rests but another one serves the
            # same stations (P8 beside K8+) read as a day it runs
            line_where += " and r.route_short_name = :line"
            params["line"] = line
    else:
        # the whole place at each end, as the departures are matched
        origin_where = "o.stop_id in " + _place_group("origin")
        dest_where = "x.stop_id in " + _place_group("dest")
        params = {"origin": origin_id, "dest": dest_id}
        if route:
            line_where = "and t.route_id = :route"
            params["route"] = route
        if str(direction) in ("0", "1"):
            line_where += " and (t.direction_id = :direction or t.direction_id is null)"
            params["direction"] = int(direction)

    sql = f"""
        with recursive dates(d) as (
            select date(:from_date)
            union all
            select date(d, '+1 day') from dates
            where d < date(:from_date, :horizon)
        ),
        serving as (
            select distinct t.service_id
            from trips t
            inner join stop_times o on o.trip_id = t.trip_id
            inner join stop_times x on x.trip_id = t.trip_id
            {line_join}
            where {origin_where} and {dest_where}
              and o.stop_sequence < x.stop_sequence
              and {_boards("o")} and {_alights("x")}
              {line_where}
        )
        select min(dates.d) from dates
        where exists (
            select 1 from serving s
            inner join calendar cal on cal.service_id = s.service_id
            where cal.start_date <= dates.d and cal.end_date >= dates.d
              and (case cast(strftime('%w', dates.d) as int)
                     when 0 then cal.sunday   when 1 then cal.monday
                     when 2 then cal.tuesday  when 3 then cal.wednesday
                     when 4 then cal.thursday when 5 then cal.friday
                     else cal.saturday end) = 1
              and not exists (
                  select 1 from calendar_dates cx
                  where cx.service_id = s.service_id
                    and cx.date = dates.d and cx.exception_type = 2))
        or exists (
            select 1 from serving s
            inner join calendar_dates cd on cd.service_id = s.service_id
            where cd.date = dates.d and cd.exception_type = 1)
    """  # noqa: S608

    try:
        with schedule.engine.connect() as conn:
            row = conn.execute(text(sql), {
                **params,
                "from_date": from_date,
                "horizon": f"+{int(horizon)} days",
            }).fetchone()
    except Exception as ex:  # pylint: disable=broad-except
        # never let a lookup that only enriches an attribute break the update
        _LOGGER.warning("Could not determine next service date: %s", ex)
        return None

    result = row[0] if row else None
    _LOGGER.debug("Next service date for %s -> %s from %s: %s",
                  origin_id, dest_id, from_date, result)
    return str(result)[:10] if result else None


def _feed_now(schedule, route=None):
    """This moment as the feed writes its clocks: in its agency's zone.

    The query lays the stored clocks on service days and compares them
    with now, and a clock is the local time where the network runs.
    SQLite's own 'now', 'localtime' is the zone of the process, often UTC
    in a container, and Home Assistant's is where the user lives: either
    way a network in another zone, or a process left on UTC, dropped or
    kept the wrong hours of departures. The route's agency first, the
    feed's first agency otherwise, Home Assistant's zone when the feed
    names none. Naive, as the query's own datetimes are.
    """
    name = None
    try:
        with schedule.engine.connect() as conn:
            row = None
            if route:
                row = conn.execute(text(
                    "SELECT agency.agency_timezone FROM routes "
                    "JOIN agency ON agency.agency_id = routes.agency_id "
                    "WHERE routes.route_id = :route"), {"route": route}).fetchone()
            if not row or not row[0]:
                row = conn.execute(text(
                    "SELECT agency_timezone FROM agency "
                    "WHERE agency_timezone IS NOT NULL AND agency_timezone <> '' "
                    "LIMIT 1")).fetchone()
            name = row[0] if row else None
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read the agency's zone, using Home Assistant's: %s", ex)
    zone = dt_util.get_time_zone(name) if name else None
    moment = dt_util.now()
    if zone is not None:
        moment = moment.astimezone(zone)
    return moment.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _fetch_departure_rows(route_type, origin, destination, schedule, direction=None, route=None,
                          line=None, origin_names=None, destination_names=None,
                          window=None, limit=30):
    """Run the static-GTFS SQL query and return matching rows as plain dicts.

    direction is only given by an entry at a loop's terminus
    (get_pair_direction, stored as loop_direction); the pair and the order of
    the stops decide it everywhere else, and the direction older entries
    store is not read. line, origin_names and destination_names belong to
    the train path: the line code the flow picked, and every station the
    entry ticked at each end.

    The sensor reads the next `limit` departures from now. The timetable
    export (write_timetable_file) reads whole service days instead:
    window is (first, last), two YYYY-MM-DD service dates, and every
    departure from now on those days comes back, `limit` then being a
    safeguard rather than the list's length. Without a window the query is
    the sensor's, unchanged."""
    if route_type == "2":
        route_type_where = f"route.route_type in ({RAIL_ROUTE_TYPES_SQL})"
        # The station is matched on the exact name the flow offered. A prefix
        # match also boarded the rider at any station whose name extends the
        # asked one (Champagnole, Champagnole Paul-Emile Victor), whichever
        # departed first. Every station the entry ticked at each end counts,
        # the coach station its replacement coaches leave from included.
        start_station_id = str(origin)
        end_station_id = str(destination)
        origin_in, name_params = station_names_in("origin", origin_names or [origin])
        dest_in, dest_params = station_names_in("dest", destination_names or [destination])
        name_params.update(dest_params)
        start_station_where = f"AND origin_stop_time.stop_id in (select stop_id from stops where stop_name IN {origin_in})"
        end_station_where = f"AND destination_stop_time.stop_id in (select stop_id from stops where stop_name IN {dest_in})"
        shortest_ride_where = ""
        # the train flow does not ask for a direction, and it stores the
        # picked line's code instead of a route id: the departures hold to
        # that line, so stations shared by several lines do not mix theirs
        direction_where = ""
        route_where = "AND route.route_short_name = :line" if line else ""
        _LOGGER.debug("Setting up TRAIN Route for start/end : %s / %s, line: %s", start_station_id, end_station_id, line)
    else:
        route_type_where = "1=1"
        name_params = {}
        start_station_id = origin.split(': ')[0]
        end_station_id = destination.split(': ')[0]
        # both ends are matched on the whole place, every record of it: the
        # entry holds one record, the vehicle may call at another (the other
        # side of the road, the other quay of a terminus)
        origin_group = _place_group("origin_station_id")
        end_group = _place_group("end_station_id")
        start_station_where = "AND origin_stop_time.stop_id IN " + origin_group
        end_station_where = "AND destination_stop_time.stop_id IN " + end_group
        # a trip passing a place twice offers the pair twice (Palm Bus 21 calls
        # at Gare SNCF de Cannes on its way out and on its way back): the ride
        # is the shortest one, no other call at either end between the two
        shortest_ride_where = f"""AND NOT EXISTS (
                SELECT 1 FROM stop_times between_stop
                WHERE between_stop.trip_id = trip.trip_id
                  AND between_stop.stop_sequence > origin_stop_time.stop_sequence
                  AND between_stop.stop_sequence < destination_stop_time.stop_sequence
                  AND (between_stop.stop_id IN {origin_group}
                       OR between_stop.stop_id IN {end_group}))"""
        direction_where = ("AND (trip.direction_id = :direction OR trip.direction_id IS NULL)"
                           if str(direction) in ("0", "1") else "")
        # a place is shared by every line calling at it: the entry's line only
        route_where = "AND trip.route_id = :route" if route else ""
        _LOGGER.debug("Setting up Route for start/end : %s / %s ", start_station_id, end_station_id)

    window_where = "AND vd.date BETWEEN :window_first AND :window_last" if window else ""
    ## QUERY candidate_trips and cal_expand are used to construct a list of valida_dates, i.e a list where services run
    ## valid_dates is then used in the main query
    sql_query = f"""
       WITH RECURSIVE
          candidate_trips AS MATERIALIZED (
            SELECT trip.trip_id, trip.service_id,
                   CAST(julianday(date(origin_stop_time.departure_time)) - julianday('1970-01-01') AS INTEGER) AS day_offset,
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
          ),
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
            WHERE (
                (CAST(strftime('%w', d) AS INTEGER) = 0 AND sunday    = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 1 AND monday    = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 2 AND tuesday   = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 3 AND wednesday = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 4 AND thursday  = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 5 AND friday    = 1) OR
                (CAST(strftime('%w', d) AS INTEGER) = 6 AND saturday  = 1)
            )
            AND NOT EXISTS (
              SELECT 1 FROM calendar_dates cd
              WHERE cd.service_id = cal_expand.service_id
                AND cd.date = cal_expand.d AND cd.exception_type = 2
            )
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
               datetime(vd.date || ' ' || time(origin_stop_time.arrival_time),'+' || CAST(julianday(date(origin_stop_time.arrival_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS origin_arrival_dt,
               time(origin_stop_time.departure_time) AS origin_depart_time,
			   datetime(vd.date || ' ' || time(origin_stop_time.departure_time),'+' || CAST(julianday(date(origin_stop_time.departure_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS origin_depart_dt,
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
               datetime(vd.date || ' ' || time(destination_stop_time.arrival_time),'+' || CAST(julianday(date(destination_stop_time.arrival_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS dest_arrival_dt,
               time(destination_stop_time.departure_time) AS dest_depart_time,
               datetime(vd.date || ' ' || time(destination_stop_time.departure_time),'+' || CAST(julianday(date(destination_stop_time.departure_time)) - julianday('1970-01-01') AS INTEGER) || ' days') AS dest_depart_dt,
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
        WHERE datetime(
                vd.date || ' ' || time(origin_stop_time.departure_time),
                -- the whole day offset, as in the SELECT: a call past 48:00
                -- is two days on, not one
                '+' || CAST(julianday(date(origin_stop_time.departure_time)) - julianday('1970-01-01') AS INTEGER) || ' days'
              ) >= datetime(:now)
          {window_where}
        ORDER BY vd.date, origin_stop_time.departure_time
        LIMIT {int(limit)};
    """  # noqa: S608

    query_params = {
        "origin_station_id": start_station_id,
        "end_station_id": end_station_id,
        "direction": int(direction) if str(direction) in ("0", "1") else None,
        "route": route,
        "line": line,
        "route_type": route_type,
        "window_first": window[0] if window else None,
        "window_last": window[1] if window else None,
        # this moment on the network's clock, see _feed_now
        "now": _feed_now(schedule, route),
        **name_params,
    }
    _LOGGER.debug("SQL statement:\n%s", sql_query)
    _LOGGER.debug("SQL parameters:\n%s", query_params)

    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), query_params).fetchall()

    return [row_cursor._asdict() for row_cursor in rows], start_station_id


def _interpret_departure_rows(hass, rows, start_station_id, now, now_local_tz,
                               now_date_local_tz, now_time):
    """Turn raw SQL-shaped rows into the `next_departure` dict."""
    _LOGGER.debug("Interpret rows: %s", rows)
    timetable = {}
    for row in rows:
        service_date = row["origin_depart_date"]  # service day, for grouping only
        depart_dt_str = row["origin_depart_dt"]    # already a correct full instant
        try:
            depart_dt = datetime.datetime.strptime(depart_dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            _LOGGER.warning("Could not parse departure datetime: %s", depart_dt_str)
            continue

        # already departed? The row's clock is the network's, so it is laid
        # in the network's zone before it is compared: read naive against
        # Home Assistant's clock, a network in another zone lost or kept
        # the wrong hour of departures
        row_zone_name = row.get("agency_timezone") or row.get("origin_stop_timezone")
        row_zone = dt_util.get_time_zone(row_zone_name) if row_zone_name else None
        if row_zone is not None:
            if depart_dt.replace(tzinfo=row_zone) <= now_local_tz:
                continue
        elif depart_dt <= now:
            continue

        day_label = service_date  # real ISO date beyond tomorrow

        idx = (depart_dt_str, str(row["trip_id"]))
        if idx in timetable:
            # a trip reached from two quays of the origin: expected, kept once
            _LOGGER.debug("Duplicate timetable key: %s, trip_id: %s", idx, row["trip_id"])
            continue
        timetable[idx] = {**row, "day": day_label, "first": False, "last": False}

    dates_seen = {}
    for idx in sorted(timetable.keys()):
        d = timetable[idx]["origin_depart_date"]
        dates_seen.setdefault(d, []).append(idx)
    for date_key, idxs in dates_seen.items():
        timetable[idxs[0]]["first"] = True
        timetable[idxs[-1]]["last"] = True

    item = {}
    for key in sorted(timetable.keys()):
        item = timetable[key]
        _LOGGER.debug("Departure(s) found for station %s @ %s -> %s", start_station_id, key, item)
        break
    _LOGGER.debug("Item(s) from SQL: %s", item)

    if item == {}:
        # No departure to show. Keep returning an empty dict: callers test this
        # value for truth and then read the fields of a real departure, so a
        # non-empty "there is nothing" would be read as a departure and crash.
        # The date of the next service is published separately, by the
        # coordinator, through get_next_service_date.
        _LOGGER.debug("No items found in gtfs")
        return {}

    # Define timezone related attribs
    if hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration")
        timezone = "UTC"
    else:
        timezone = dt_util.get_time_zone(hass.config.time_zone)
        _LOGGER.debug("Timezone HA: %s",timezone)
    _LOGGER.debug("Default timezone: %s",timezone)
    _LOGGER.debug("Agency timezone: %s",item["agency_timezone"])
    _LOGGER.debug("Origin stop timezone: %s",item["origin_stop_timezone"])
    _LOGGER.debug("Dest stop timezone: %s",item["dest_stop_timezone"])
    if item["agency_timezone"] is not None:
        _LOGGER.debug("Setting Orig & Dest TZ based on Agency: %s",item["agency_timezone"])
        timezone = dt_util.get_time_zone(item["agency_timezone"])
        timezone_dest = dt_util.get_time_zone(item["agency_timezone"])
    elif item["origin_stop_timezone"] is not None:
        _LOGGER.debug("Setting Orig & Dest TZ based on origin stop: %s",item["origin_stop_timezone"])
        timezone = dt_util.get_time_zone(item["origin_stop_timezone"])
        timezone_dest = dt_util.get_time_zone(item["origin_stop_timezone"])
    if item["dest_stop_timezone"] is not None and item["agency_timezone"] is None:
        _LOGGER.debug("Setting Dest TZ based on dest stop: %s",item["dest_stop_timezone"])
        timezone_dest = dt_util.get_time_zone(item["dest_stop_timezone"])
    else:
        timezone_dest = timezone
    _LOGGER.debug("Defined orig timezone: %s, dest timezone: %s",timezone,timezone_dest)
    _LOGGER.debug("Defined now incl. offset (if configured): %s",now_local_tz)

    # create upcoming timetable, use timezone before resetting to UTC and reset 'item' to match with timezone
    timetable_remaining = []
    ix = 0
    item = {}
    max_remaining = 10
    for key in sorted(timetable.keys()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        if upcoming > now_local_tz:
            if ix == 0 :
                _LOGGER.debug("Resetting item")
                item = timetable[key]
                ix = ix + 1
            _LOGGER.debug("Adding departure in defined timezone: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, ix: %s", upcoming, now_local_tz, key, ix)
            timetable_remaining.append(dt_util.as_utc(upcoming).isoformat())
            if len(timetable_remaining) >= max_remaining:
                break
    _LOGGER.debug("Timetable Remaining Departures on this Start/Stop: %s", timetable_remaining)
    if item == {}:
        # every departure found is already gone: the same empty dict as
        # when none was found, for the same callers
        _LOGGER.debug("No items found in gtfs")
        return {}

    # create upcoming timetable with line info, headsign and trips
    timetable_remaining_line = []
    timetable_remaining_headsign = []
    timetable_upcoming_trips = []
    timetable_upcoming_arrivals = []
    timetable_upcoming_durations = []
    timetable_upcoming_origin_stops = []
    max_remaining = 10
    count = 0
    timetable_upcoming_route_types = []
    for key, value in sorted(timetable.items()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        # dest_arrival_dt is already the correct instant - no rollover guessing needed
        upcoming_arrival = datetime.datetime.strptime(
            value["dest_arrival_dt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone_dest)
        if upcoming > now_local_tz:
            _LOGGER.debug("Adding list item for departure/key: %s, Upcoming: %s, Value: %s", key, upcoming, value )
            timetable_remaining_line.append(
                str(dt_util.as_utc(upcoming).isoformat())  + " (" + str(value["route_short_name"]) +  str( ("/" + value["route_long_name"])  if value["route_long_name"] else "") + ")"
            )
            timetable_remaining_headsign.append(
                str(dt_util.as_utc(upcoming).isoformat()) + " (" + str(value["trip_headsign"]) + ")"
            )
            timetable_upcoming_trips.append(
                str(value["trip_id"])
            )
            timetable_upcoming_arrivals.append(
                dt_util.as_utc(upcoming_arrival).isoformat()
            )
            # both ends are known here, so serve the theoretical duration
            # ready-made rather than leaving every card to subtract the
            # paired lists themselves
            timetable_upcoming_durations.append(
                round((upcoming_arrival - upcoming).total_seconds() / 60)
            )
            # the record it leaves from: a place may be served from either
            timetable_upcoming_origin_stops.append(str(value.get("origin_stop_id")))
            # a train line may list a coach among its departures: each one
            # says what rides it, so a card can draw a bus for that one
            timetable_upcoming_route_types.append(
                departure_route_type(value.get("route_type"), value.get("origin_stop_id"))
            )
            count += 1
            if count >= max_remaining:
                break

    # origin/dest arrival & departure, make datetime and apply timezone
    origin_depart = datetime.datetime.strptime(item["origin_depart_dt"], "%Y-%m-%d %H:%M:%S")
    origin_arrival = datetime.datetime.strptime(item["origin_arrival_dt"], "%Y-%m-%d %H:%M:%S")
    dest_arrival = datetime.datetime.strptime(item["dest_arrival_dt"], "%Y-%m-%d %H:%M:%S")
    dest_depart = datetime.datetime.strptime(item["dest_depart_dt"], "%Y-%m-%d %H:%M:%S")

    _LOGGER.debug("Origin depart time: %s, Dest depart time: %s", origin_depart, dest_depart)

    depart_time = origin_depart.replace(tzinfo=timezone)
    arrival_time = dest_arrival.replace(tzinfo=timezone_dest)
    origin_arrival_time = dt_util.as_utc(origin_arrival.replace(tzinfo=timezone)).isoformat()
    origin_depart_time = dt_util.as_utc(origin_depart.replace(tzinfo=timezone)).isoformat()
    dest_arrival_time = dt_util.as_utc(dest_arrival.replace(tzinfo=timezone_dest)).isoformat()
    dest_depart_time = dt_util.as_utc(dest_depart.replace(tzinfo=timezone_dest)).isoformat()


    origin_stop_time = {
        "Arrival Time": origin_arrival_time,
        "Departure Time": origin_depart_time,
        "Drop Off Type": item["origin_drop_off_type"],
        "Pickup Type": item["origin_pickup_type"],
        "Shape Dist Traveled": item["origin_dist_traveled"],
        "Headsign": item["origin_stop_headsign"],
        "Sequence": item["origin_stop_sequence"],
        "Timepoint": item["origin_stop_timepoint"],
    }

    destination_stop_time = {
        "Arrival Time": dest_arrival_time,
        "Departure Time": dest_depart_time,
        "Drop Off Type": item["dest_drop_off_type"],
        "Pickup Type": item["dest_pickup_type"],
        "Shape Dist Traveled": item["dest_dist_traveled"],
        "Headsign": item["dest_stop_headsign"],
        "Sequence": item["dest_stop_sequence"],
        "Timepoint": item["dest_stop_timepoint"],
    }

    data_returned = {
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
        "origin_stop_time": origin_stop_time,
        "origin_stop_timezone": item["origin_stop_timezone"],
        "destination_stop_time": destination_stop_time,
        "destination_stop_timezone": item["dest_stop_timezone"],
        "destination_stop_id": item["dest_stop_id"],
        "destination_stop_name": item["dest_stop_name"],
        "next_departures": timetable_remaining,
        "next_departures_lines": timetable_remaining_line,
        "next_departures_headsign": timetable_remaining_headsign,
        "next_departures_trip_id": timetable_upcoming_trips,
        "next_departures_destination_arrival_times": timetable_upcoming_arrivals,
        "next_departures_durations": timetable_upcoming_durations,
        "next_departures_origin_stop_id": timetable_upcoming_origin_stops,
        "next_departures_route_types": timetable_upcoming_route_types,
    }

    return data_returned

def _departure_clocks(_data):
    """now (naive, offset applied), now in the local zone, its date and
    the clock, the way the departures are read against them."""
    offset = _data["offset"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_local_tz = dt_util.now() + datetime.timedelta(minutes=offset)
    return (now, now_local_tz, now_local_tz.strftime(dt_util.DATE_STR_FORMAT),
            now.strftime(TIME_STR_FORMAT))


def drop_departure_trips(hass, _data, struck):
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
    now, now_local_tz, now_date_local_tz, now_time = _departure_clocks(_data)
    return _interpret_departure_rows(
        hass, kept, _data.get("departure_rows_origin"), now, now_local_tz,
        now_date_local_tz, now_time)


def departure_query_args(_data):
    """What an entry's departures are asked with beyond its two ends, the
    same for the sensor and for the timetable export: the direction kept at
    a loop's terminus, the entry's line, and on the train path the line
    code the flow picked and every station ticked at each end."""
    return {
        "direction": _data.get("loop_direction"),
        "route": (_data.get("route") or "").split(": ")[0] or None,
        "line": str(_data.get("line", "") or "").strip() or None,
        "origin_names": entry_stations(_data, "origin"),
        "destination_names": entry_stations(_data, "destination"),
    }


def get_next_departure(hass, _data):
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

    now, now_local_tz, now_date_local_tz, now_time = _departure_clocks(_data)

    # Fetch all departures

    rows, start_station_id = _fetch_departure_rows(
        route_type, _data["origin"], _data["destination"], schedule,
        **departure_query_args(_data))
    # kept beside the departures: a realtime refresh that learns of a
    # cancelled trip reads them again without it (drop_departure_trips),
    # rather than showing the struck trip as on time until the next
    # static refresh
    _data["departure_rows"] = rows
    _data["departure_rows_origin"] = start_station_id

    return _interpret_departure_rows(
        hass, rows, start_station_id, now, now_local_tz,
        now_date_local_tz, now_time
    )


def get_gtfs(hass, path, data, update=False):
    _LOGGER.debug("Getting gtfs with data: %s", data)
    _headers = None
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    url = data["url"]
    url = with_query_key(url, data)
    if data.get(CONF_API_KEY_LOCATION, None) == "header":
      if data.get(CONF_API_KEY, None):
        _headers = {data.get(CONF_API_KEY_NAME, "api_key"): data[CONF_API_KEY]}
    file = data["file"] + ".zip"
    sqlite = data["file"] + ".sqlite"
    check_source_dates = data.get("check_source_dates", False)
    journal = os.path.join(gtfs_dir, filename + ".sqlite-journal")
    if check_extracting(hass, gtfs_dir,filename) and not update :
        _LOGGER.debug("Cannot use this datasource as still unpacking: %s", filename)
        return "extracting"
    if update and data["extract_from"] == "url":
        _pending_remove = os.path.exists(os.path.join(gtfs_dir, file))
    else:
        _pending_remove = False
    # a feed whose every service lies ahead is refused BEFORE anything is
    # removed: the check used to run once the database was gone, so its
    # "keeping the current data" had nothing left to keep
    if (check_source_dates and update and data["extract_from"] == "zip"
            and os.path.exists(os.path.join(gtfs_dir, file))
            and zip_only_future_dates(os.path.join(gtfs_dir, file))):
        _LOGGER.info("New file contains only dates in the future, keeping the current data")
        return
    if update and data["extract_from"] == "zip" and os.path.exists(os.path.join(gtfs_dir, file)) and os.path.exists(os.path.join(gtfs_dir, sqlite)):
        os.remove(os.path.join(gtfs_dir, sqlite))
        if os.path.exists(journal):
                os.remove(journal)        
    # a built database answers on its own: the zip only matters to rebuild
    # it. Missing, it was fetched again on every call, and a host down
    # turned a working datasource into "no_data_file"
    served = not update and os.path.exists(os.path.join(gtfs_dir, sqlite))
    if data["extract_from"] == "zip" and not served:
        if not os.path.exists(os.path.join(gtfs_dir, file)):
            _LOGGER.error("The given GTFS zipfile was not found")
            return "no_zip_file"
    if data["extract_from"] == "url" and not served:
        if update or not os.path.exists(os.path.join(gtfs_dir, file)):
            try:
                # some providers answer 403 to the default requests user agent;
                # _headers is None unless an api key is used in a header
                _get_headers = dict(_headers or {})
                _get_headers.setdefault("User-Agent", "home-assistant-gtfs2")
                r = fetch("get", url, headers=_get_headers, allow_redirects=True, timeout=15, stream=True)
                r.raise_for_status()
                # verify before removing anything: a download that turns out
                # not to be a zip must leave the datasource as it was
                # a source built from one network of an envelope takes that
                # member out of it, never the envelope itself
                staged = stage_zip(r, os.path.join(gtfs_dir, file), data.get(CONF_INNER_ZIP))
                if staged is None:
                    return "no_data_file"
                if check_source_dates and update and zip_only_future_dates(staged):
                    # read on the download itself, before the current data
                    # is removed or the zip replaced
                    _LOGGER.info("New file contains only dates in the future, keeping the current data")
                    os.remove(staged)
                    return
                if _pending_remove:
                    # the staged download is the new edition: it goes in
                    # below, not out with the old one
                    remove_datasource(hass, path, filename, True, keep=(".zip.new",))
                adopt_zip(r, staged, os.path.join(gtfs_dir, file))
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.exception("The given URL or GTFS data file/folder was not found: %s", ex)
                return "no_data_file"
    
    (gtfs_root, _) = os.path.splitext(file)
    sqlite_file = f"{gtfs_root}.sqlite?check_same_thread=False&timeout=60"
    joined_path = os.path.join(gtfs_dir, sqlite_file)  

    gtfs = pygtfs.Schedule(joined_path)
    if served and not gtfs.feeds and not os.path.exists(os.path.join(gtfs_dir, file)):
        # a database file with nothing in it, and no zip to fill it from
        _LOGGER.error("Datasource %s is empty and its zip is gone", filename)
        gtfs.engine.dispose()
        return "no_zip_file" if data["extract_from"] == "zip" else "no_data_file"

    if not gtfs.feeds: 
        # a feed_info.txt pygtfs cannot read stops the whole import
        if data.get("clean_feed_info", False) or feed_info_unreadable(os.path.join(gtfs_dir, file)):
            _fork_ctx = multiprocessing.get_context("fork")
            extract = _fork_ctx.Process(target=extract_from_zip, args = (hass, gtfs,gtfs_dir,file,['shapes.txt','transfers.txt','fare_attributes.txt','levels.txt','pathways.txt','translations.txt','feed_info.txt']))
        else: 
            _fork_ctx = multiprocessing.get_context("fork")
            extract = _fork_ctx.Process(target=extract_from_zip, args = (hass, gtfs,gtfs_dir,file,['shapes.txt','transfers.txt','fare_attributes.txt','levels.txt','pathways.txt','translations.txt']))
        extract.start()
        extract.join()
        _LOGGER.info("Exiting main after start subprocess for unpacking: %s", file)
        return "extracting"
    return gtfs

def extract_from_zip(hass, gtfs, gtfs_dir, file, remove_file):
    _LOGGER.debug("Extracting gtfs file: %s", file)
    # first remove shapes from zip to avoid possibly very large db 
    remove_from_zip(remove_file,gtfs_dir, file[:-4])
    if os.fork() != 0:
        return
    drop_import_indexes(gtfs)
    pygtfs.append_feed(gtfs, os.path.join(gtfs_dir, file))
    check_datasource_index(hass, gtfs, gtfs_dir, file[:-4])
    repair_trip_directions(gtfs)

    


def remove_from_zip(delmelist,gtfs_dir,file):
    """Rewrite a kept zip without the members listed, or leave it as it was.

    The zip is the only full record of the feed, so the rewrite happens
    beside it and the original only steps aside once the new one is whole.
    A feed that breaks halfway used to leave nothing under the zip's name:
    check_extracting then saw the _temp.zip for ever and every sensor of
    the source stayed "extracting" until someone renamed the file by hand.

    Members are copied through rather than read whole: stop_times.txt of a
    national feed is bigger than the memory of the machines this runs on.
    """
    _LOGGER.debug("Removing data: %s , from zipfile: %s", delmelist, file)
    tempfile = file + "_temp.zip"
    tempfile_out = file + "_temp_out.zip"
    filename = file + ".zip"
    kept = os.path.join(gtfs_dir, filename)
    aside = os.path.join(gtfs_dir, tempfile)
    written = os.path.join(gtfs_dir, tempfile_out)
    os.rename (kept, aside)
    try:
        with zipfile.ZipFile(aside, 'r') as zin, \
             zipfile.ZipFile(written, 'w') as zout:
            for item in zin.infolist():
                if (item.filename not in delmelist):
                    with zin.open(item) as source, zout.open(item, 'w') as target:
                        shutil.copyfileobj(source, target)
        os.rename(written, kept)
        os.remove(aside)
        return True
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Could not rewrite %s without %s: %s", filename, delmelist, ex)
        # the feed goes back under its own name, whole, as if nothing had
        # been attempted
        if not os.path.exists(kept) and os.path.exists(aside):
            os.rename(aside, kept)
        if os.path.exists(written):
            try:
                os.remove(written)
            except OSError:
                pass
        return False


def get_route_list(schedule, data, with_trips_only=False, gtfs_dir=None):
    """List the routes of a datasource.

    with_trips_only skips the routes that carry no trip. A datasource holds
    every route of the network, and routes stays complete even when the trips
    of a route are not (or no longer) loaded, so offering those would send the
    user to a stop list that comes back empty.

    Routes that a prune emptied are the exception: they are kept, because the
    user is entitled to add a line the prune removed, and hiding it would leave
    no way back. They come back flagged so the caller can offer to reload the
    datasource rather than walking into an empty stop list.

    Which lines those are is read from the source zip, not from the database:
    a prune empties whatever links routes to stops, so the database can only
    report the lines it still carries. gtfs_dir enables that lookup; without it
    the pruned lines are simply not offered, as before.
    """
    _LOGGER.debug("Getting routes with data: %s", data)
    route_type_where = ""
    agency_where = ""
    trips_where = ""
    pruned = set()
    if with_trips_only:
        with_trips = "and exists (select 1 from trips t where t.route_id = r.route_id)"
        if gtfs_dir:
            in_zip = get_routes_in_zip(gtfs_dir, data["file"])
            if in_zip:
                with schedule.engine.connect() as conn:
                    loaded = {r[0] for r in conn.execute(
                        text("select distinct route_id from trips"))}
                # declared by the feed but carrying no trip here: a prune took
                # them out, and the zip can put them back
                pruned = in_zip - loaded
        trips_where = with_trips
        if pruned:
            placeholders = ", ".join(f":pr{i}" for i in range(len(pruned)))
            trips_where = f"and (exists (select 1 from trips t where t.route_id = r.route_id) or r.route_id in ({placeholders}))"
    # bound, not written into the query: an agency_id holding a quote
    # broke the list, and what the flow hands in is the user's pick
    agency_id = data["agency"].split(': ', 1)[0]
    if agency_id != "0":
        agency_where = "and r.agency_id = :agency_id"
    if data["route_type"] != "99":
        route_type_where = "and route_type = :route_type"
    sql_routes = f"""
    SELECT r.route_type, r.route_id, r.route_short_name, r.route_long_name, a.agency_name
    from routes r
    left join agency a on a.agency_id = r.agency_id
    where 1=1
    {route_type_where}
    {agency_where}
    {trips_where}
    order by agency_name
    """  # noqa: S608
    routes_list = []
    routes = []
    with schedule.engine.connect() as conn:
        params = {"agency_id": agency_id, "route_type": data["route_type"]}
        params.update({f"pr{i}": r for i, r in enumerate(sorted(pruned))})
        rows = conn.execute(text(sql_routes), params).fetchall()
    for row_cursor in rows:
        routes_list.append(list(row_cursor))
    # the lines whose long name says nothing get the two ends of the route
    # instead, read in one go rather than one query per line
    endpoints = route_ends(
        schedule, gtfs_dir, data["file"], [str(x[1]) for x in routes_list if not _adds_to(x[2], x[3])])
    for x in routes_list:
        # the value keeps route_type and route_id, which the flow parses back;
        # what follows the second ## is only ever shown to the user, so it
        # leads with the line number and where it goes, not the raw id
        route_type, route_id, short, long, agency = (str(v) for v in x)
        # route_long_name names the two ends of the line, in no particular
        # order: the direction is picked on the same screen, so no arrow here
        shown = _route_label(short, long, endpoints.get(route_id), route_id)
        if route_id in pruned:
            # a fourth field the flow reads to know the timetable is missing;
            # the label itself stays clean, the flow explains it in words
            val = f"{route_type}##{route_id}##{shown}##pruned"
        else:
            val = f"{route_type}##{route_id}##{shown}"
        routes.append(val)
    # sorted on what the user reads, and read the way a line number is: the
    # cast on route_id this used to order by is 0 for every id that is not a
    # number, which is most of them outside a small network
    # lines of two operators under one number get the agency's name
    routes = _set_apart(routes, [x[4] for x in routes_list])
    # and routes one operator publishes under one name get their two ends
    routes = _set_apart_by_ends(
        routes, look_alike_ends(schedule, gtfs_dir, data["file"], _look_alikes(routes)))
    # and the same line published once per period of validity, which the
    # zip dates even for a line whose timetable was never imported. Only
    # when something still reads the same, like the ends above
    if _look_alikes(routes):
        spans = route_spans(gtfs_dir, data["file"], [str(x[1]) for x in routes_list])
        routes = _leave_out_expired(routes, spans)
        routes = _set_apart_by_span(routes, spans)
    routes.sort(key=lambda value: _natural(value.split("##")[2]))
    _LOGGER.debug(f"routes: {routes}")
    return routes


def get_route_count(schedule, data):
    """How many routes get_route_list lists without with_trips_only.

    The route screen only shows that number. Building the whole list to
    count it read the ends of every line with no long name from
    stop_times: IDFM with every operator, 1837 lines, 25 to 47 s once many
    lines are imported, and the screen waited for it.
    """
    agency_id = data["agency"].split(': ', 1)[0]
    agency_where = "and agency_id = :agency_id" if agency_id != "0" else ""
    route_type_where = "and route_type = :route_type" if data["route_type"] != "99" else ""
    sql = f"select count(*) from routes where 1=1 {agency_where} {route_type_where}"  # noqa: S608
    with schedule.engine.connect() as conn:
        return conn.execute(
            text(sql), {"agency_id": agency_id, "route_type": data["route_type"]}).scalar()

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


# A place is what the rider waits at, whatever the feed writes it as. Most
# feeds give each side of the road a record of its own, one per direction,
# and some give one per platform: Zou files the two poles of Pont de la
# Brague, 8 m apart, under one parent station, and half of the line's trips
# are entered on the pole across the road from the way they drive. Picking a
# record hid the trips entered on the other one. So a place is the parent
# station when the feed has one; when it has none (TAO: 9 parents for 1359
# poles), the records of the same name within PLACE_LAT / PLACE_LON of each
# other, about 150 m, which gathered the three poles of Zenith, 107 m apart
# at most, and keeps apart two villages' "Centre". The box is measured from
# the record the entry holds, so the list and the queries agree on it.
PLACE_LAT = 0.00135


PLACE_LON = 0.002


def _place_group(param):
    """SQL "(...)" of every stop_id of the place of the stop bound to :param."""
    return f"""(
    select sibling.stop_id
    from stops chosen, stops sibling
    where chosen.stop_id = :{param}
      and (sibling.stop_id = chosen.stop_id
           or (chosen.parent_station is not null
               and chosen.parent_station <> ''
               and sibling.parent_station = chosen.parent_station)
           or ((chosen.parent_station is null or chosen.parent_station = '')
               and (sibling.parent_station is null or sibling.parent_station = '')
               and sibling.stop_name = chosen.stop_name
               and abs(sibling.stop_lat - chosen.stop_lat) <= {PLACE_LAT}
               and abs(sibling.stop_lon - chosen.stop_lon) <= {PLACE_LON})))"""


_STOP_GROUP = _place_group("origin")


# Whether the rider can get on, or off, at a call. stop_times says it per
# call: pickup_type and drop_off_type read 0 (or nothing) for a regular
# stop, 1 for none at all, 2 and 3 for a phone call or a word to the driver,
# which is still a way on. A 1 is not rare, and not only at the ends of a
# trip: a night train takes nobody on at its morning stops (SNCF: 2,080
# calls mid-route, 44 route-stop pairs where no trip ever boards), a coach
# sets down only on its way into town (Zou: 9,285 calls, 279 pairs), the
# Dutch feed flags 51,145 calls and 912 pairs. Offering such a call as a
# departure, or as a place to get off, sends the rider to a bus that will
# not open its door. The value is cast, pygtfs stores it as a number but a
# feed's blank is a NULL, and the db of a test may hold text.
def _boards(alias):
    """SQL: the rider can get on at this stop_times row."""
    return f"coalesce(cast({alias}.pickup_type as integer), 0) <> 1"


def _alights(alias):
    """SQL: the rider can get off at this stop_times row."""
    return f"coalesce(cast({alias}.drop_off_type as integer), 0) <> 1"


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
        near = [s for s in seeds if _same_place(info[s], info[stop_id])]
        place[stop_id] = min(near, key=lambda s: (_box_distance(info[s], info[stop_id]), seeds.index(s)))
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


def _chain_of(trips, place):
    """One order of places for the whole line, both ways round.

    direction_id cannot be trusted to split a line: on GVB tram 1 a third of
    the trips carry the other way's label, and the spec itself keeps it for
    publishing timetables, not for routing. The order of the stops can: the
    fullest piece is laid first, and every other piece is read forward or
    backward, whichever way the places it shares with the chain already
    agree with, then its places are slotted in after the place preceding
    them. A piece sharing nothing yet waits for the chain to grow.
    """
    pieces = {}
    for _trip_id, trip_stops in trips.items():
        for piece in _segments_of([place[s] for s, _seq in trip_stops]):
            pieces.setdefault(tuple(piece), 0)
            pieces[tuple(piece)] += 1
    pending = sorted(pieces, key=lambda p: (-len(p), -pieces[p], p))
    order = []
    while pending:
        waiting = []
        for piece in pending:
            position = {p: i for i, p in enumerate(order)}
            shared = [position[p] for p in piece if p in position]
            if not order:
                forward = True
            elif len(shared) < 2:
                waiting.append(piece)
                continue
            else:
                up = sum(1 for a, b in zip(shared, shared[1:]) if b > a)
                down = sum(1 for a, b in zip(shared, shared[1:]) if b < a)
                forward = up >= down
            prev = -1
            for p in (piece if forward else reversed(piece)):
                if p in position:
                    prev = order.index(p)
                    continue
                prev += 1
                order.insert(prev, p)
                position = {q: i for i, q in enumerate(order)}
        if len(waiting) == len(pending):
            # nothing left shares two places with the chain: keep them in
            # riding order at the end rather than lose them
            for piece in waiting:
                order.extend(p for p in piece if p not in order)
            break
        pending = waiting
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


def _line_of(conn, route_id, direction=None):
    """_ride_of for a route, its sampled trips kept beside: (kept,
    station_names, place, trips)."""
    rows = conn.execute(text(_STOP_ROWS), {
        "route_id": route_id, "direction": _direction_param(direction)}).fetchall()
    heading = conn.execute(text(_HEADING_ROWS), {"route_id": route_id}).fetchall()
    kept, station_names, place = _ride_of(rows, heading)
    trips, _info = _trips_of(rows)
    return kept, station_names, place, trips


def _loop_termini(trips, place):
    """The places some trip of the line starts and ends at: a loop's terminus
    (TAO 22 runs Zénith to Zénith both ways round)."""
    return {place.get(stops[0][0]) for stops in trips.values()
            if stops and place.get(stops[0][0]) == place.get(stops[-1][0])}


def _origin_boarding(conn, route_id, origin_stop_id):
    """The calls of the route at the origin's place a rider can get on at,
    as {(trip_id, stop_sequence)}: what _calls_out starts a ride from."""
    return {(row[0], row[1]) for row in conn.execute(text(f"""
        select st.trip_id, st.stop_sequence
        from stop_times st
        inner join trips t on t.trip_id = st.trip_id
        where t.route_id = :route_id and st.stop_id in {_STOP_GROUP}
        and {_boards("st")}"""), {"route_id": route_id, "origin": origin_stop_id})}  # noqa: S608


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
    # same sampling as _STOP_ROWS, on the part of each trip after the origin
    rides_sql = f"""
    with through as (
        select trip_id, min(stop_sequence) as origin_sequence
        from stop_times where stop_id in {_STOP_GROUP} and {_boards("stop_times")}
        group by trip_id
    ), ride as (
        select t.trip_id, group_concat(st.stop_sequence || ':' || st.stop_id) as stops
        from trips t
        inner join through o on o.trip_id = t.trip_id
        inner join stop_times st on st.trip_id = t.trip_id
            and st.stop_sequence > o.origin_sequence
        where t.route_id = :route_id
        and (:direction is null or t.direction_id = :direction or t.direction_id is null)
        group by t.trip_id
    )"""  # noqa: S608
    sql = rides_sql + """, sample as (
        select min(trip_id) as trip_id from ride group by stops
    )
    SELECT st.trip_id, s.stop_id, s.stop_name, st.stop_sequence, s.parent_station, station.stop_name,
           s.stop_lat, s.stop_lon
    from sample
    inner join through o on o.trip_id = sample.trip_id
    inner join stop_times st on st.trip_id = sample.trip_id
        and st.stop_sequence > o.origin_sequence
    inner join stops s on s.stop_id = st.stop_id
    left join stops station on station.stop_id = s.parent_station
    order by st.trip_id, st.stop_sequence
    """
    # how many trips each sampled ride stands for
    weights_sql = rides_sql + """
    select min(trip_id), count(*) from ride group by stops
    """
    # the records some trip through the origin sets riders down at, after it
    alighting_sql = rides_sql + f"""
    select distinct st.stop_id
    from ride
    inner join through o on o.trip_id = ride.trip_id
    inner join stop_times st on st.trip_id = ride.trip_id
        and st.stop_sequence > o.origin_sequence
    where {_alights("st")}
    """
    scope = {"route_id": route_id, "direction": _direction_param(direction)}
    with schedule.engine.connect() as conn:
        line, station_names, place, _line_trips = _line_of(conn, route_id, direction)
        rows = conn.execute(text(sql), {**scope, "origin": origin_stop_id}).fetchall()
        trip_count = dict(conn.execute(text(weights_sql), {**scope, "origin": origin_stop_id}).fetchall())
        alighting = {row[0] for row in conn.execute(text(alighting_sql), {**scope, "origin": origin_stop_id})}
        boarding = (_origin_boarding(conn, route_id, origin_stop_id)
                    if towards is not None else None)
    alightable = {place[s] for s in alighting if s in place}
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

    order, placed, last = [], set(), None
    while len(order) < len(reach):
        ready = [p for p in reach if p not in placed and not (before[p] - placed)]
        # nothing free: a loop's rotations order each other round
        pool = ready or [p for p in reach if p not in placed]
        going_on = [p for p in pool if last in before[p]]
        p = min(going_on or pool, key=lambda q: (-weight[q], reach[q], position.get(q, 0)))
        order.append(p)
        placed.add(p)
        last = p
    kept = [by_place[p] for p in order if p in by_place and p in alightable]
    stops = _entries_of(kept, _labels_of(line, station_names))
    _LOGGER.debug(f"Destinations from {origin_stop_id}: {stops}")
    return stops


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
        seq = [place[s] for s, _ in trip_stops]
        best = None
        last_origin = None
        for i, p in enumerate(seq):
            if p == origin:
                last_origin = i
            elif p == destination and last_origin is not None:
                if best is None or i - last_origin < best[1] - best[0]:
                    best = (last_origin, i)
                last_origin = None
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


def gtfs_seconds(value):
    """Seconds since the service day's midnight of a stop time, or None.

    The one reader of stop times for every module: the queries hand them
    back as pygtfs stores them, a datetime counted from 1970-01-01 (a 01:15
    departure after midnight reads '1970-01-02 01:15:00'), a database from
    another pygtfs build as bare text ('25:15:00'), a caller may already
    hold seconds or a timedelta. Three readers each took some of these
    forms and refused, or raised on, the others.
    """
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return int(value.total_seconds())
    if isinstance(value, (int, float)):
        return int(value)
    text_value = str(value).strip()
    if text_value.isdigit():
        return int(text_value)
    days = 0
    stored = re.match(r"^1970-01-(\d{2})[ T](.*)$", text_value)
    if stored:
        days = int(stored.group(1)) - 1
        text_value = stored.group(2)
    parts = text_value.split(".")[0].split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


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


def get_agency_list(schedule, data):
    _LOGGER.debug("Getting agencies with data: %s", data)
    sql_agencies = f"""
    SELECT a.agency_id, a.agency_name 
    from agency a
    order by a.agency_name
    """
    agencies_list = []
    agencies = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_agencies), {"q": "q"}).fetchall()
    for row_cursor in rows:
        agencies_list.append(list(row_cursor))
    for x in agencies_list:
        val = str(x[0]) + ": " + str(x[1])
        agencies.append(val)
    _LOGGER.debug(f"agencies: {agencies}")
    return agencies

# the databases a refresh or an import works in beside a source, never
# sources of their own: <file>.refresh.sqlite, <file>.import.sqlite and the
# filtered <file>.import.sqlite.zip
_WORK_FILE_PARTS = (".refresh", ".import")


def _list_gtfs_dir(gtfs_dir):
    os.makedirs(gtfs_dir, exist_ok=True)
    return os.listdir(gtfs_dir)


async def get_datasources(hass, path) -> dict[str]:
    """The datasources in the gtfs2 folder, by name.

    The whole name before ".sqlite": cut at the first dot, a name holding
    one came back short and named a source that does not exist, and the
    working files of a refresh or an import only folded into their source
    by the same accident.
    """
    _LOGGER.debug(f"Getting datasources for path: {path}")
    gtfs_dir = hass.config.path(path)
    files = await hass.async_add_executor_job(_list_gtfs_dir, gtfs_dir)
    datasources = sorted(
        file[:-len(".sqlite")] for file in files
        if file.endswith(".sqlite")
        and not file[:-len(".sqlite")].endswith(_WORK_FILE_PARTS))
    _LOGGER.debug(f"Datasources in folder: {datasources}")
    return datasources


async def get_zipfiles(hass, path) -> list[str]:
    """List the zip files sitting in the gtfs2 folder, without their extension.

    get_datasources lists datasources that were already extracted (.sqlite);
    this lists the archives still waiting to be extracted, so the user can pick
    one instead of typing its name.
    """
    gtfs_dir = hass.config.path(path)
    files = await hass.async_add_executor_job(_list_gtfs_dir, gtfs_dir)
    zipfiles = sorted(
        f[:-4] for f in files
        if f.endswith(".zip") and not f.endswith("_temp.zip")
        and not f.endswith("_temp_out.zip")
        # the filtered copy an import leaves while it runs
        and not f.endswith(".import.sqlite.zip")
    )
    _LOGGER.debug(f"Zip files in folder: {zipfiles}")
    return zipfiles


def remove_datasource(hass, path, filename, include_sqlite, keep=()):
    """Remove the files of a datasource. keep names the suffixes to spare:
    a refresh clearing the old edition keeps the download that replaces it."""
    gtfs_dir = hass.config.path(path)
    _LOGGER.info(f"Removing datasource: {os.path.join(gtfs_dir, filename)}.*")
    if include_sqlite and os.path.exists(os.path.join(gtfs_dir, filename + ".sqlite")):
        os.remove(os.path.join(gtfs_dir, filename + ".sqlite"))
    if os.path.exists(os.path.join(gtfs_dir, filename + "_temp.zip")):     
        os.remove(os.path.join(gtfs_dir, filename + "_temp.zip"))
    if os.path.exists(os.path.join(gtfs_dir, filename + "_temp_out.zip")):        
        os.remove(os.path.join(gtfs_dir, filename + "_temp_out.zip"))
    if os.path.exists(os.path.join(gtfs_dir, filename + ".sqlite-journal")):        
        os.remove(os.path.join(gtfs_dir, filename + ".sqlite-journal"))
    if os.path.exists(os.path.join(gtfs_dir, filename + ".zip")):
        os.remove(os.path.join(gtfs_dir, filename + ".zip"))
    # the sidecar follows the zip it describes
    if os.path.exists(os.path.join(gtfs_dir, filename + ".zip.meta.json")):
        os.remove(os.path.join(gtfs_dir, filename + ".zip.meta.json"))
    # what the fork keeps beside a source: the record of the installed
    # edition, and what a download, a refresh or an import stopped half way
    # leaves. Left behind, the record made a new source of the same name
    # look already built from an edition it never had
    leftovers = [".zip.new", ".refresh.sqlite", ".refresh.sqlite-journal",
                 ".import.sqlite", ".import.sqlite-journal", ".import.sqlite.zip"]
    if include_sqlite:
        leftovers += [".sqlite.meta.json", ".sqlite-wal", ".sqlite-shm"]
    for suffix in leftovers:
        if suffix in keep:
            continue
        if os.path.exists(os.path.join(gtfs_dir, filename + suffix)):
            os.remove(os.path.join(gtfs_dir, filename + suffix))
    return "removed"
    
def check_extracting(hass, gtfs_dir,file):
    _LOGGER.debug(f"Checking if extracting: %s", file)
    gtfs_dir = hass.config.path(gtfs_dir)
    filename = file
    journal = os.path.join(gtfs_dir, filename + ".sqlite-journal")
    tempzip = os.path.join(gtfs_dir, filename + "_temp.zip")
    if os.path.exists(journal)  or os.path.exists(tempzip):
        _LOGGER.debug("Extracting: yes")
        return True
    return False    


# the indexes the queries lean on, by table and column, under the names
# they have always been created with
DATASOURCE_INDEXES = (
    ("stop_times", "trip_id", "gtfs2_stop_times_trip_id"),
    ("stop_times", "stop_id", "gtfs2_stop_times_stop_id"),
    ("shapes", "shape_id", "gtfs2_shapes_shape_id"),
    ("stops", "stop_name", "gtfs2_stops_stop_name"),
    ("routes", "route_type", "gtfs2_routes_route_type"),
    ("trips", "route_id", "gtfs2_trips_route_id"),
)

# the database file each datasource was last checked as, (inode, mtime,
# size): the same file needs no second look, a rebuilt one gets one
_INDEX_CHECKED = {}


def drop_import_indexes(schedule):
    """Take the stop_times indexes off a database pygtfs is about to fill.

    From 0.1.10 on pygtfs declares trip_id and stop_id indexes on
    stop_times and creates them with the table, so SQLite would update both
    at every row an import inserts, millions on a large feed. Without them
    the rows go in bare and the indexes are built afterwards, in one pass
    each, as upstream chose ("apply indexes at end of extracting"): by
    check_datasource_index on a datasource, by the import's own
    _index_scratch on a scratch database. Only for a database still empty.
    """
    with schedule.engine.begin() as conn:
        names = [name for (name,) in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'stop_times' AND sql IS NOT NULL")).fetchall()]
        for name in names:
            conn.execute(text(f'DROP INDEX "{name}"'))


def check_datasource_index(hass, schedule, gtfs_dir, file):
    """Give a datasource the indexes the queries need, and its routes an agency.

    Runs before every refresh of every sensor, and asked sqlite_master
    seven times over as many connections each time. Now one connection
    reads it once, and a database file already checked is not read again
    until it changes.
    """
    _LOGGER.debug("Check datasource index for file: %s", file)
    if check_extracting(hass, gtfs_dir,file):
        _LOGGER.warning("Cannot check indexes on this datasource as still unpacking: %s", file)
        return
    # runs before get_next_departure on every refresh, so it meets the same
    # sentinels get_gtfs leaves in place of a schedule
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Cannot check indexes: datasource %s has no usable schedule (%s)", file, schedule or "empty")
        return
    db_file = os.path.join(hass.config.path(gtfs_dir), file + ".sqlite")
    try:
        stat = os.stat(db_file)
        edition = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError:
        edition = None
    if edition is not None and _INDEX_CHECKED.get(db_file) == edition:
        return

    # A single-agency feed may leave agency_id out of routes.txt, and out
    # of agency.txt as well (TAO does): then there is nothing to copy, the
    # two tables already agree on the missing value, and copying it back
    # would only log the "fix" again at every refresh. So only count the
    # routes when the agency table has an id to give them.
    sql_check_route_agency = """
    SELECT count(*) as check_agency
    FROM routes where (agency_id='None' or agency_id is null)
    and exists (select 1 from agency
                where agency_id is not null and agency_id not in ('None', ''))
    """
    sql_fix_route_agency = """
    update routes set agency_id = (select agency_id from agency
                                   where agency_id is not null
                                   and agency_id not in ('None', '') limit 1)
        where agency_id='None' or agency_id is null
    """
    with schedule.engine.connect() as conn:
        master = conn.execute(text(
            "SELECT type, name, tbl_name FROM sqlite_master WHERE type in ('index', 'view')")).fetchall()
        # an interned datasource exposes stop_times as a view: its indexes
        # live on gtfs2_stop_times and must not be recreated here
        views = {name for kind, name, _table in master if kind == "view"}
        indexed = [(table, name) for kind, name, table in master if kind == "index"]
        for table, column, index_name in DATASOURCE_INDEXES:
            if table in views or any(t == table and column in (n or "") for t, n in indexed):
                continue
            _LOGGER.info("Adding index %s to improve performance", index_name)
            conn.execute(text(f"create index {index_name} on {table}({column})"))  # noqa: S608
        if conn.execute(text(sql_check_route_agency)).scalar():
            _LOGGER.info("Fix missing agency_id in routes table")
            conn.execute(text(sql_fix_route_agency))
        conn.commit()
    try:
        stat = os.stat(db_file)
        _INDEX_CHECKED[db_file] = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except OSError:
        pass


def _tracker_position(hass, entity_id):
    """Where a person or zone is, (latitude, longitude), or (None, None).

    The entity may be gone, renamed or not loaded yet at start: its state
    is then None, and reading its attributes raised on every refresh and
    in the options screen alike.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None, None
    return state.attributes.get("latitude", None), state.attributes.get("longitude", None)


def get_local_stop_list(hass, schedule, data):
    _LOGGER.debug("Getting local stops list with data: %s", data)
    latitude, longitude = _tracker_position(hass, data['device_tracker_id'])
    if not latitude or not longitude:
        # nowhere to look around: no stop is near
        return 0
    radius= data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    sql_query = f"""
        SELECT stop.stop_id, stop.stop_name
        FROM stops stop
        where abs(stop.stop_lat - :latitude) < :radius and abs(stop.stop_lon - :longitude) < :radius
        """  
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), {"latitude": latitude, "longitude": longitude, "radius": radius}).fetchall()
    rowcount = 0
    for row_cursor in rows:
        rowcount += 1
    _LOGGER.debug("Local stops list output: %s", rowcount)
    return rowcount
        

def _build_local_stop_element(self, row, base_datetime,
                              timezone_agency, timezone_stop, now_tz,
                              apply_now_filter, feed_entities=None):
    """Build one departure element incl. realtime, for a given service date.

    base_datetime / datetime_label: both are departure_dt from the query.
    """
    self._trip_id = row["trip_id"]
    self._direction = str(row["direction_id"])
    self._trip_short_name = row["trip_short_name"]
    self._route = row["route_id"]
    self._route_id = row["route_id"]
    self._stop_id = row["stop_id"]
    self._stop_sequence = row["stop_sequence"]
    #_LOGGER.debug("Row departure_time: %s", row["departure_time"])
    #_LOGGER.debug("base_datetime / datetime_label: %s", base_datetime)

    # collect departure time from row, using agency timezone as basis, then transforming it to the stop-specific timezone (based on Amtrak)
    self._departure_datetime = datetime.datetime.strptime(
        base_datetime, "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone_agency).astimezone(tz=timezone_stop)
    self._departure_datetime_utc = dt_util.as_utc(self._departure_datetime)
    #_LOGGER.debug("Self._departure datetime in agency_tz: %s", self._departure_datetime)
    self._departure_time = self._departure_datetime.replace(tzinfo=None).strftime(TIME_STR_FORMAT)
    #_LOGGER.debug("Self._departure time in stop tz: %s", self._departure_time)

    departure_rt = "-"
    departure_rt_datetime = "-"
    delay_rt = "-"
    delay_rt_derived = "-"
    departures = []

    # Find RT if configured
    if self._realtime:
        self._get_next_service = {}
        _LOGGER.debug("Find rt for local stop route: %s - direction: %s - stop: %s - stop_sequence: %s", self._route, self._direction, self._stop_id, self._stop_sequence)
        next_service = get_rt_route_trip_statuses(self, feed_entities)
        _LOGGER.debug("Next service: %s", next_service)
        struck = struck_trips(self)
        if self._trip_id in struck and on_service_day(struck[self._trip_id], base_datetime):
            # cancelled, or not calling here: not a departure the rider can
            # take, so not one to list
            _LOGGER.debug("Trip %s at %s is struck out by the feed on %s", self._trip_id, self._stop_id, base_datetime)
            return None
        if next_service:
            svc = next_service.get(self._route, {}).get(self._direction, {}).get(self._stop_id, [])
            delays = svc.get("delays", []) if svc else []
            departures = svc.get("departures", []) if svc else []
            delay_rt = delays[0] if delays else "-"
            departure_rt = departures[0] if departures else "-"
            departure_rt_datetime = departure_rt
        _LOGGER.debug("Departure rt: %s, Delay rt: %s", departure_rt, delay_rt)

    if departure_rt != "-":
        depart_time_corrected_time = departures[0].astimezone(tz=timezone_stop)
        departure_rt = depart_time_corrected_time.replace(tzinfo=None).strftime(TIME_STR_FORMAT)
        td = abs(depart_time_corrected_time - self._departure_datetime)
        if td.seconds != 0 and depart_time_corrected_time < self._departure_datetime:
            delay_rt_derived = "-" + str(td)
        elif td.seconds != 0:
            delay_rt_derived = str(td)
        _LOGGER.debug("Delay derived: %s, departure_rt: %s", delay_rt_derived, departure_rt)
    else:
        #depart_time_corrected_time = (dt_util.parse_datetime(f"{base_date} {self._departure_time}")).replace(tzinfo=timezone_stop)
        depart_time_corrected_time = dt_util.parse_datetime(base_datetime).replace(tzinfo=timezone_stop)
    #_LOGGER.debug("Departure time corrected based on realtime-time: %s", depart_time_corrected_time)

    if delay_rt != "-" and delay_rt != 0:
        #depart_time_corrected_delay = (dt_util.parse_datetime(f"{base_date} {self._departure_time}") + datetime.timedelta(seconds=delay_rt)).replace(tzinfo=timezone_stop)
        depart_time_corrected_delay = (dt_util.parse_datetime(base_datetime) + datetime.timedelta(seconds=delay_rt)).replace(tzinfo=timezone_stop)
    else:
        delay_rt = "-"
        #depart_time_corrected_delay = dt_util.parse_datetime(f"{base_date} {self._departure_time}").replace(tzinfo=timezone_stop)
        depart_time_corrected_delay = dt_util.parse_datetime(base_datetime).replace(tzinfo=timezone_stop)
    #_LOGGER.debug("Departure time corrected based on realtime-delay: %s", depart_time_corrected_delay)

    if depart_time_corrected_delay > depart_time_corrected_time:
        depart_time_corrected = depart_time_corrected_delay
    else:
        depart_time_corrected = depart_time_corrected_time
    #_LOGGER.debug("Departure time corrected: %s", depart_time_corrected)

    if apply_now_filter and not (depart_time_corrected > now_tz):
        _LOGGER.debug("Departure time corrected: %s, NOT after now in tz with offset: %s", depart_time_corrected, now_tz)
        return None

    return {
        "departure": self._departure_time,
        "departure_datetime": self._departure_datetime_utc,
        "departure_realtime": departure_rt,
        "departure_realtime_datetime": departure_rt_datetime,
        "delay_realtime_derived": delay_rt_derived,
        "delay_realtime": delay_rt,
        "date": datetime.datetime.strptime(base_datetime, "%Y-%m-%d %H:%M:%S").date().isoformat(),
        "stop_name": row["stop_name"],
        "stop_id": row["stop_id"],
        "route": row["route_short_name"],
        "route_long": row["route_long_name"],
        "headsign": row["trip_headsign"],
        "trip_id": row["trip_id"],
        "direction_id": row["direction_id"],
        "icon": self._icon,
    }                

def _fetch_local_stop_rows(schedule, latitude, longitude, radius,
                            time_range, time_range_history, now):
    """Run the local-stop SQL query and return plain dicts. """
    ## QUERY candidate_stops and candidate_dates are used to construct a list of valid_dates, i.e a list where services run
    ## valid_dates is then used in the main query
    sql_query = f"""    
        WITH
          -- the stops within the radius first, then their calls: on an
          -- interned database stop_times is a view whose stop_id index the
          -- planner cannot see, and it read every call of the network to
          -- keep those of a few stops (4 to 8 s on the Orleans feed)
          nearby AS MATERIALIZED (
            SELECT stop_id FROM stops
            WHERE abs(stop_lat - :latitude) < :radius AND abs(stop_lon - :longitude) < :radius
          ),
          candidate_stops AS MATERIALIZED (
            SELECT stop.stop_id, stop.stop_name, stop.stop_lat AS latitude, stop.stop_lon AS longitude,
                   stop.stop_timezone AS stop_timezone, agency.agency_timezone AS agency_timezone,
                   trip.trip_id, trip.trip_headsign, trip.direction_id, trip.trip_short_name,
                   trip.service_id,
                   st.departure_time AS departure_time_raw,
                   st.stop_sequence AS stop_sequence,
                   route.route_long_name, route.route_short_name, route.route_type, route.route_id
            FROM nearby
            CROSS JOIN stop_times st ON st.stop_id = nearby.stop_id
            INNER JOIN stops stop ON stop.stop_id = st.stop_id
            INNER JOIN trips trip ON trip.trip_id = st.trip_id
            INNER JOIN routes route ON route.route_id = trip.route_id
            INNER JOIN agency agency ON route.agency_id = agency.agency_id
            WHERE {_boards("st")}
          ),
          -- from as many days back as the latest call around here asks for
          -- (a call at 48:10 leaves two days after its service day), at
          -- least yesterday, to tomorrow
          candidate_dates(date) AS (
            SELECT date(:now_offset, '-' || max(1, (
                SELECT coalesce(max(CAST(julianday(date(departure_time_raw)) - julianday('1970-01-01') AS INTEGER)), 0)
                FROM candidate_stops)) || ' days')
            UNION ALL
            SELECT date(date, '+1 day') FROM candidate_dates
            WHERE date < date(:now_offset, '+1 day')
          ),
          valid_dates AS MATERIALIZED (
            SELECT cal.service_id, cd.date
            FROM calendar cal
            CROSS JOIN candidate_dates cd
            WHERE cal.service_id IN (SELECT service_id FROM candidate_stops)
              AND cd.date BETWEEN cal.start_date AND cal.end_date
              AND (
                (CAST(strftime('%w', cd.date) AS INTEGER) = 0 AND cal.sunday    = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 1 AND cal.monday   = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 2 AND cal.tuesday  = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 3 AND cal.wednesday = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 4 AND cal.thursday = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 5 AND cal.friday   = 1) OR
                (CAST(strftime('%w', cd.date) AS INTEGER) = 6 AND cal.saturday = 1)
              )
              AND NOT EXISTS (
                SELECT 1 FROM calendar_dates ex
                WHERE ex.service_id = cal.service_id AND ex.date = cd.date AND ex.exception_type = 2
              )
            UNION
            SELECT cd2.service_id, cd2.date
            FROM calendar_dates cd2
            INNER JOIN candidate_dates cd ON cd.date = cd2.date
            WHERE cd2.service_id IN (SELECT service_id FROM candidate_stops)
              AND cd2.exception_type = 1
          )
        SELECT cs.stop_id, cs.stop_name, cs.latitude, cs.longitude, cs.stop_timezone, cs.agency_timezone,
               cs.trip_id, cs.trip_headsign, cs.direction_id, cs.trip_short_name,
               datetime(
                 vd.date || ' ' || time(cs.departure_time_raw),
                 '+' || CAST(julianday(date(cs.departure_time_raw)) - julianday('1970-01-01') AS INTEGER) || ' days'
               ) AS departure_dt,
               cs.stop_sequence, cs.route_long_name, cs.route_short_name, cs.route_type,
               cs.route_id
        FROM candidate_stops cs
        INNER JOIN valid_dates vd ON vd.service_id = cs.service_id
        WHERE datetime(
                vd.date || ' ' || time(cs.departure_time_raw),
                '+' || CAST(julianday(date(cs.departure_time_raw)) - julianday('1970-01-01') AS INTEGER) || ' days'
              ) BETWEEN datetime(:now_offset, :timerange_history) AND datetime(:now_offset, :timerange)
        ORDER BY cs.stop_id, vd.date, cs.departure_time_raw;
    """  # noqa: S608        
    
    query_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timerange": time_range,
        "timerange_history": time_range_history,
        "radius": radius,
        "now_offset": now,
    }

    _LOGGER.debug("SQL statement:\n%s", sql_query)
    _LOGGER.debug("SQL parameters:\n%s", query_params)

    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), query_params).fetchall()

    data_returned = [row_cursor._asdict() for row_cursor in rows]
    _LOGGER.debug("Local stop rows returned: %s", data_returned)
    return data_returned


def _interpret_local_stop_rows(self, rows):
    """Turn raw SQL-shaped rows into the local-stops departures list.

    No database: `rows` only needs to be a list of plain dicts
    """
    offset = self._data["offset"]
    timetable = []
    local_stops_list = []
    prev_stop_id = ""
    prev_entry = entry = {}

    # Define timezone
    if self.hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration, using UTC")
        timezone_local = dt_util.get_time_zone("UTC")
    else:
        timezone_local = dt_util.get_time_zone(self.hass.config.time_zone)
    _LOGGER.debug("Local timezone: %s",timezone_local)
    
    now_tz = dt_util.now().replace(tzinfo=timezone_local) + datetime.timedelta(minutes=offset)
    _LOGGER.debug("Default 'now' on local timezone, incl. offset (if configured): %s",now_tz)

	
    # Set elements for realtime retrieval via local file.
    if self._realtime:
        self._rt_group = "trip"
        rt_key = dict(getattr(self, "_rt_key", None) or {})
        if rt_key.get(CONF_API_KEY_LOCATION) == "query_string":
            # the coordinator already put this key in the url: handed on,
            # get_gtfs_rt appended it a second time (?key=K&key=K)
            rt_key.pop(CONF_API_KEY_LOCATION)
        self._rt_data = {
            "url": self._trip_update_url,
            CONF_API_KEY : rt_key.get(CONF_API_KEY,None),
            CONF_API_KEY_NAME : rt_key.get(CONF_API_KEY_NAME, None),
            CONF_API_KEY_LOCATION : rt_key.get(CONF_API_KEY_LOCATION,None),
            CONF_ACCEPT_HEADER_PB :rt_key.get(CONF_ACCEPT_HEADER_PB,None),
            "file": self._data["name"] + "_localstop",
            }
        _LOGGER.debug("self rt_data: %s, self headers: %s, self data: %s", self._rt_data, self._headers, self._data)

        check = get_gtfs_rt(self.hass,DEFAULT_PATH_RT,self._rt_data)

        # check if local file created
        if check != "ok":
            # the timetable still stands: the departures are listed without
            # their delays this cycle, where they all went with the feed
            _LOGGER.warning("Could not download RT data from %s, listing the "
                            "timetable alone", self._trip_update_url)
        else:
            # use local file created as new url
            self._trip_update_url = "file://" + DEFAULT_PATH_RT + "/" + self._data["name"] + "_localstop.rt"

    # Fetch + parse the RT feed once for this refresh cycle.
    feed_entities = None
    if self._realtime and not self._trip_update_url.startswith("file://"):
        # the download failed: an empty feed, so the lines below do not
        # each go and ask the host again
        feed_entities = []
    elif self._realtime:
        feed_entities = get_gtfs_feed_entities(
            url=self._trip_update_url, headers=self._headers, label="trip_data"
        ) or []

    for row in rows:  
        #_LOGGER.debug("Row from query: %s", row)
        #defining TZ for row
        #_LOGGER.debug("Configured Agency timezone: %s", row['agency_timezone'])
        #_LOGGER.debug("Configured Stop timezone: %s", row['stop_timezone'])
        if row['agency_timezone'] is not None:
            timezone_agency = dt_util.get_time_zone(row['agency_timezone'])
        elif row['stop_timezone'] is not None:
            timezone_agency = dt_util.get_time_zone(row['stop_timezone'])
        else:
            timezone_agency = timezone_local
        if row['stop_timezone'] is not None:
            timezone_stop = dt_util.get_time_zone(row['stop_timezone'])
        else:
            timezone_stop = timezone_local
        _LOGGER.debug("Using Agency timezone: %s", timezone_agency)
        _LOGGER.debug("Using Stop timezone: %s", timezone_stop)

        if row["stop_id"] != prev_stop_id and prev_stop_id != "":
            local_stops_list.append(prev_entry)
            timetable = []

        entry = {"stop_id": row['stop_id'], "stop_name": row['stop_name'], "stop_sequence": row['stop_sequence'], "latitude": row['latitude'], "longitude": row['longitude'], "departure": timetable, "offset": offset}
        self._icon = ICONS.get(row['route_type'], ICON)
       
        element = _build_local_stop_element(
            self, row, row["departure_dt"], 
            timezone_agency, timezone_stop, now_tz,
            apply_now_filter=True, feed_entities=feed_entities)
            
        if element is not None:
            if element not in timetable:
                timetable.append(element)
            _LOGGER.debug("Timetable: %s", timetable)

        prev_entry = entry.copy()
        prev_stop_id = str(row["stop_id"])
        entry["departure"] = timetable


    if entry:
        local_stops_list.append(entry)

    for stop in local_stops_list:
        stop["departure"].sort(key=lambda d: d["departure_datetime"])

    data_returned = local_stops_list
    _LOGGER.debug("Interpreted local stop rows returned: %s", data_returned)
    return data_returned

def get_local_stops_next_departures(self):
    _LOGGER.debug("Get local stop departure with data: %s", self._data)
    if check_extracting(self.hass, self._data['gtfs_dir'],self._data['file']):
        _LOGGER.debug("Cannot get next departures on this datasource as still unpacking: %s", self._data["file"])
        return []
    """Get next departures from data."""
    schedule = self._data["schedule"]
    # same contract as get_next_departure: a sentinel or None instead of a
    # schedule means nothing to offer, not a traceback
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no local stops", self._data["file"], schedule or "empty")
        return []
    offset = self._data["offset"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    latitude, longitude= _tracker_position(self.hass, self._data['device_tracker_id'])
    time_range= str('+' + str(self._data.get("timerange", DEFAULT_LOCAL_STOP_TIMERANGE)) + ' minute')
    time_range_history = str('-' + str(self._data.get("timerange_history", DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY)) + ' minute')
    radius = self._data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    if not latitude or not longitude:
        _LOGGER.error("No latitude and/or longitude for : %s", self._data['device_tracker_id'])
        return []

    rows = _fetch_local_stop_rows(
        schedule, latitude, longitude, radius, time_range, time_range_history, now
    )
    return _interpret_local_stop_rows(self, rows)


async def update_gtfs_local_stops(hass, data): 
    _LOGGER.debug("Update service for local stops with data: %s", data)
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("device_tracker_id") == data["entity_id"] :
            entries.append(entry.entry_id)
    for cf_entry in entries:
        _LOGGER.debug("Reloading local stops for config_entry_id: %s", cf_entry) 
        await hass.config_entries.async_reload(cf_entry)
    return
    
def _route_departures_between(data, first, last, limit=5000, at="origin_depart_dt"):
    """Every departure of an entry over two service days, as UTC instants.

    The window read the timetable export makes, rather than the sensor's
    next ten: a busy line has more than ten departures left today, and
    the ten the sensor lists all fell on today, so "tomorrow" came back
    empty. Each row is laid in its network's zone before it becomes an
    instant, and a trip reached from two quays of one stop is listed once.

    at is the time of the ride read: origin_depart_dt, its departure from
    the origin, or dest_arrival_dt, its arrival at the destination (the
    arrivals service), laid in the destination's zone when the agency
    names none.
    """
    rows, _origin = _fetch_departure_rows(
        data["route_type"], data["origin"], data["destination"], data["schedule"],
        window=(first, last), limit=limit, **departure_query_args(data))
    instants, seen = [], set()
    stop_zone = "dest_stop_timezone" if at == "dest_arrival_dt" else "origin_stop_timezone"
    for row in rows:
        key = (row.get(at), row.get("trip_id"))
        if key in seen or not row.get(at):
            continue
        seen.add(key)
        zone_name = row.get("agency_timezone") or row.get(stop_zone)
        zone = (dt_util.get_time_zone(zone_name) if zone_name else None) or dt_util.DEFAULT_TIME_ZONE
        try:
            local = datetime.datetime.strptime(row[at], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        instants.append(dt_util.as_utc(local.replace(tzinfo=zone)))
    return sorted(instants)


def _route_departure_from(data, first_day, at="origin_depart_dt"):
    """The entry's first departure on the service day first_day or after,
    as a UTC instant, or None when the calendar has none in its horizon;
    with at="dest_arrival_dt", that ride's arrival."""
    args = departure_query_args(data)
    day = get_next_service_date(
        data["schedule"], data["origin"].split(": ")[0], data["destination"].split(": ")[0],
        first_day, data["route_type"], line=args["line"],
        origin_names=data.get("origin_stations"), dest_names=data.get("destination_stations"),
        route=args["route"], direction=args["direction"])
    if not day:
        return None
    # the rows come in time order: the first is the one
    instants = _route_departures_between(data, day, day, limit=1, at=at)
    return instants[0] if instants else None


async def get_route_departures(hass, data):
    """The entry's departures today and tomorrow, from from_time on, and
    what lies past them (_route_times)."""
    return await _route_times(hass, data, "origin_depart_dt")


async def get_route_arrivals(hass, data):
    """The arrivals at the destination of the entry's rides still to leave,
    today and tomorrow, from from_time on, and what lies past them: the
    departures service read at the other end of the ride. Every arrival
    of the two days, not the sensor's next ten nor a hundred."""
    return await _route_times(hass, data, "dest_arrival_dt")


async def _route_times(hass, data, at):
    """The entry's rides today and tomorrow, from from_time on, and what
    lies past them, each ride read at `at` (_route_departures_between).

    Two empty lists said the same for a line that resumes on Thursday, a
    line suspended and a feed that ran out. As the timetable file does,
    next is the first one after the two days, None when the calendar has
    none, and until the last service day the feed publishes: an empty
    answer with no next reads "nothing published until then".
    """
    _LOGGER.debug("Getting route %s with data: %s", at, data)
    config_entry = hass.config_entries.async_get_entry(data.get("config_entry",""))
    empty = {"today": [], "tomorrow": [], "next": None, "until": None}
    if config_entry is None:
        # a service call naming an entry that is not there, or not gtfs2's
        _LOGGER.error("No gtfs2 entry %s to read the departures of", data.get("config_entry"))
        return empty
    cf_data = config_entry.data
    cf_options = config_entry.options
    _LOGGER.debug("config entry data: %s, options: %s", cf_data, cf_options)
    if not (cf_data.get("origin") and cf_data.get("destination")):
        # the entry picker offers every gtfs2 entry, a source or a local
        # stops one among them: neither is a journey with two ends
        _LOGGER.error("Entry %s is not a journey, it has no departures to list",
                      data.get("config_entry"))
        return empty

    # the day and the cut-off are the rider's, in Home Assistant's zone
    now = dt_util.now()
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    tomorrow_date = (now + datetime.timedelta(days=1)).strftime(dt_util.DATE_STR_FORMAT)
    # yesterday's service runs past midnight: its 24:40 is today's 00:40,
    # which a window starting today left out
    yesterday_date = (now - datetime.timedelta(days=1)).strftime(dt_util.DATE_STR_FORMAT)
    from_time = data.get('from_time', '00:00:00')
    cutoff_today = datetime.datetime.strptime(now_date + ' ' + from_time, "%Y-%m-%d %H:%M:%S")
    cutoff_tomorrow = datetime.datetime.strptime(tomorrow_date + ' ' + from_time, "%Y-%m-%d %H:%M:%S")
    _LOGGER.debug("Cutoff today: %s, cutoff tomorrow: %s", cutoff_today, cutoff_tomorrow)

    _pygtfs = await hass.async_add_executor_job(
        get_gtfs, hass, DEFAULT_PATH, cf_data, False
    )
    if _pygtfs is None or isinstance(_pygtfs, str):
        # a sentinel: no zip, no database, or a feed all in the future
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no departures",
                        cf_data.get("file"), _pygtfs or "empty")
        return empty

    # what the sensor of this entry is asked with, line and ends included,
    # so the service answers for the same journey
    _data = {
            "schedule": _pygtfs,
            "origin": cf_data["origin"],
            "destination": cf_data["destination"],
            **{key: cf_data[key] for key in ("origin_stations", "destination_stations")
               if cf_data.get(key)},
            "offset": cf_options["offset"] if "offset" in cf_options else 0,
            "gtfs_dir": DEFAULT_PATH,
            "name": cf_data["name"],
            "file": cf_data["file"],
            "route_type": cf_data["route_type"],
            "route": cf_data["route"],
            "loop_direction": cf_data.get("loop_direction"),
            "line": cf_data.get("line"),
        }
    day_after = (now + datetime.timedelta(days=2)).strftime(dt_util.DATE_STR_FORMAT)
    try:
        # one service day more than the two listed: the query costs about
        # the same (TAO tram A, 1.8 s for a day or three), and the next
        # departure is usually in it, a run of tomorrow's service after
        # midnight or the day after's first
        instants = await hass.async_add_executor_job(
            _route_departures_between, _data, yesterday_date, day_after, 5000, at)
        later = [i for i in instants
                 if dt_util.as_local(i).strftime(dt_util.DATE_STR_FORMAT) > tomorrow_date]
        next_instant = later[0] if later else await hass.async_add_executor_job(
            _route_departure_from, _data,
            (now + datetime.timedelta(days=3)).strftime(dt_util.DATE_STR_FORMAT), at)
        until = await hass.async_add_executor_job(
            last_service_day, os.path.join(hass.config.path(DEFAULT_PATH), cf_data["file"] + ".zip"))
    finally:
        # released whatever happens: this schedule was opened for the call
        try:
            _pygtfs.engine.dispose()
        except Exception:  # pylint: disable=broad-except
            pass

    today_departures = []
    tomorrow_departures = []
    for instant in instants:
        local = dt_util.as_local(instant).replace(tzinfo=None)
        day = local.strftime(dt_util.DATE_STR_FORMAT)
        # from_time is where the list starts: a departure at that very
        # second is in it, the midnight one of the default 00:00:00 above all
        if day == now_date and cutoff_today <= local:
            today_departures.append(instant.isoformat())
        elif day == tomorrow_date and cutoff_tomorrow <= local:
            tomorrow_departures.append(instant.isoformat())

    _departures = {"today": today_departures, "tomorrow": tomorrow_departures,
                   "next": next_instant.isoformat() if next_instant else None,
                   "until": until}
    _LOGGER.debug("Route %s returned: %s", at, _departures)
    return _departures
    
def _trip_stops(schedule, trips, origin_ids):
    """The stops each trip calls at from the origin on, "name - HH:MM:SS".

    Read in stop_sequence order, one bound parameter per trip. The origin
    is found by its stop_id, compared whole: the list was a text search
    through "trip: name - time (stop_id)" lines, where an id inside
    another one, or a name holding ": ", threw the match off.
    """
    if not trips:
        return {}
    marks = ", ".join(f":t{i}" for i in range(len(trips)))
    sql_stops = f"""
    SELECT st.trip_id, s.stop_name, time(st.departure_time), s.stop_id
    from stop_times st
    inner join stops s on s.stop_id = st.stop_id
    where st.trip_id in ({marks})
    order by st.trip_id, st.stop_sequence
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_stops), {f"t{i}": trip for i, trip in enumerate(trips)}).fetchall()
    calls = {}
    for trip_id, name, time_of_day, stop_id in rows:
        calls.setdefault(str(trip_id), []).append((str(stop_id), f"{name} - {time_of_day}"))
    origins = {str(stop_id) for stop_id in origin_ids}
    stopslist = {}
    for trip in trips:
        listed, reached = [], False
        for stop_id, shown in calls.get(str(trip), []):
            reached = reached or stop_id in origins
            if reached:
                listed.append(shown)
        stopslist[trip] = listed
    return stopslist


async def get_trip_stops(hass, data):
    _LOGGER.debug("Getting stoptimes for trip with: %s", data)
    entity_id = data.get("entity_id", "")
    state = hass.states.get(entity_id)
    entry = er.async_get(hass).async_get(entity_id)
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id) if entry else None
    nothing = {"entity": entity_id or "entity-not-found", "origin_station_id": "",
               "origin_station_name": "", "trip_stops": {}}
    if state is None or config_entry is None:
        # a service call naming an entity that is not a gtfs2 sensor
        _LOGGER.error("No gtfs2 sensor %s to read the trip stops of", entity_id)
        return nothing
    cf_data = config_entry.data
    origin_station_ids=[]
    origin_station_names=[]
    trips=[]
    if 'device_tracker_id' in state.attributes:
        for trip in state.attributes.get("next_departures_lines",{}):
            trips.append(trip.get("trip_id",""))
            if trip.get("stop_id","") not in origin_station_ids:
                origin_station_ids.append(trip.get("stop_id",""))
            if trip.get("stop_name","") not in origin_station_names:
                origin_station_names.append(trip.get("stop_name",""))
    else:
        trips = list(state.attributes.get("next_departures_trips") or [])
        origin_station_ids.append(state.attributes.get("origin_station_stop_id", ""))
        origin_station_names.append(state.attributes.get("origin_station_stop_name", ""))

    schedule = await hass.async_add_executor_job(
        get_gtfs, hass, DEFAULT_PATH, cf_data, False
    )
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no trip stops",
                        cf_data.get("file"), schedule or "empty")
        return nothing
    try:
        # off the event loop: the query reads every call of every trip listed
        stopslist = await hass.async_add_executor_job(
            _trip_stops, schedule, trips, origin_station_ids)
    finally:
        schedule.engine.dispose()

    _tripstops = {
        "entity": entity_id or "entity-not-found",
        "origin_station_id": origin_station_ids[0] if origin_station_ids else "",
        "origin_station_name": origin_station_names[0] if origin_station_names else "",
        "trip_stops": stopslist,
    }

    _LOGGER.debug("Tripstops returned: %s", _tripstops)
    return _tripstops
