"""Support for GTFS Integration."""
from __future__ import annotations

import asyncio
import datetime
import gc
import time
import logging
import os
import glob
import re
import sqlite3
import hashlib
import json
import csv
import io
import requests
import pygtfs
from sqlalchemy.sql import text
import multiprocessing
from multiprocessing import Process
from . import zip_file as zipfile
from pathlib import Path


import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant.components import persistent_notification
from homeassistant.helpers.translation import async_get_translations
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import entity_registry as er

from .direction_repair import repair_trip_directions
from .const import (
    DEFAULT_PATH_GEOJSON,
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    CONF_ACCEPT_HEADER_PB,
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
from .gtfs_rt_helper import get_rt_route_trip_statuses, get_gtfs_rt, safe_file_part, get_gtfs_feed_entities
from .gtfs_db import import_routes, optimise_datasource, real_path, routes_in
from .gtfs_filter import (
    filter_gtfs_zip,
    read_zip_agencies,
    read_zip_routes,
    zip_only_future_dates,
)

_LOGGER = logging.getLogger(__name__)


# How far ahead get_next_service_date is allowed to look. A route that has not
# run for three months is not "resuming later", it is out of the feed, and an
# unbounded scan would walk the whole calendar to say so.
NEXT_SERVICE_HORIZON_DAYS = 90


def get_next_service_date(schedule, origin_id, dest_id, from_date, route_type="3",
                          horizon=NEXT_SERVICE_HORIZON_DAYS):
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
    """
    # the coordinator calls this with whatever get_gtfs returned, which is a
    # sentinel string or None when the datasource is unusable. Matched by
    # shape, not by class: anything schedule-shaped may query
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("No usable schedule to look up the next service date (%s)", schedule or "empty")
        return None
    if route_type == "2":
        # trains match on the exact stop_name, like get_next_departure does
        origin_where = ("o.stop_id in (select stop_id from stops "
                        "where stop_name = :origin)")
        dest_where = ("x.stop_id in (select stop_id from stops "
                      "where stop_name = :dest)")
        origin_id = str(origin_id)
        dest_id = str(dest_id)
    else:
        origin_where = "o.stop_id = :origin"
        dest_where = "x.stop_id = :dest"

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
            where {origin_where} and {dest_where}
              and o.stop_sequence < x.stop_sequence
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
                "origin": origin_id,
                "dest": dest_id,
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


def _fetch_departure_rows(route_type, origin, destination, include_tomorrow,
                           now, now_date, yesterday, tomorrow, tomorrow_date, schedule,
                           direction=None, route=None, line=None):
    """Run the static-GTFS SQL query and return matching rows as plain dicts.
                                                                                                            
                 

                                        

    This is the only part of get_next_departure that touches the database.
    Split out so its output (`rows`) can be handed in directly by a test,
    without a real schedule/database, instead of always coming from here.
    """
    if route_type == "2":
        route_type_where = f"route_type in (2,100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117)"
        # The station is matched on the exact name the flow offered. A prefix
        # match also boarded the rider at any station whose name extends the
        # asked one (Champagnole, Champagnole Paul-Emile Victor), whichever
        # departed first.
        start_station_id = str(origin)
        end_station_id = str(destination)
        start_station_where = f"AND start_station.stop_id in (select stop_id from stops where stop_name = :origin_station_id)"
        end_station_where = f"AND end_station.stop_id in (select stop_id from stops where stop_name = :end_station_id)"
        # the train flow does not ask for a direction, it stores 0 as a placeholder
        direction_where = ""
        direction = None
        route = None
        route_where = ""
        # the train flow stores the picked line's code instead of a route id
        line_where = "AND route.route_short_name = :line" if line else ""
        _LOGGER.debug("Setting up TRAIN Route for start/end : %s / %s ", start_station_id, end_station_id)
    else:
        route_type_where = "1=1"
        start_station_id = origin.split(': ')[0]
        end_station_id = destination.split(': ')[0]
        # A feed often writes one physical stop as several records, one per
        # platform, and a run uses whichever of them its turn takes. On the
        # Orleans tram, of the trips leaving the hospital for Jules Verne on
        # one day, 40 end on Jules Verne's first record and 34 on its second,
        # two metres apart under the same name; and 55 more leave from the
        # hospital's own second record. Asked for one record at each end, the
        # sensor showed 40 of those 129 runs, and following the line took
        # four entries instead of one. Both ends are therefore matched on the
        # whole stop: the record itself and the records the feed groups with
        # it under the same parent station. Nothing rides backwards for it:
        # the query still keeps the origin before the destination on the same
        # trip, and the entry's direction still holds. Matching on the name
        # rather than on the declared parent was tried and dropped, it
        # answered on a different stop that happened to share a name.
        stop_group = """(
            SELECT sibling.stop_id
            FROM stops chosen, stops sibling
            WHERE chosen.stop_id = :%s
              AND (sibling.stop_id = chosen.stop_id
                   OR (chosen.parent_station IS NOT NULL
                       AND chosen.parent_station <> ''
                       AND sibling.parent_station = chosen.parent_station)))"""
        start_station_where = ("AND start_station.stop_id IN "
                               + stop_group % "origin_station_id")
        end_station_where = ("AND end_station.stop_id IN "
                             + stop_group % "end_station_id")
        # A circular line runs both directions through the same stops in the
        # same order, so the stop pair alone does not tell them apart: also
        # hold the direction the entry was set up with. Trips without a
        # direction_id keep matching, as in get_stop_list.
        if direction in (0, 1):
            direction_where = "AND (trip.direction_id = :direction OR trip.direction_id IS NULL)"
        else:
            direction = None
            direction_where = ""
        # The same goes for the line: the flow picked one route, so the
        # sensor holds it, and stops shared by several lines no longer mix
        # their departures. Entries without a stored route stay as they were.
        route_where = "AND trip.route_id = :route" if route else ""
        line = None
        line_where = ""
        _LOGGER.debug("Setting up Route for start/end : %s / %s, direction: %s, route: %s", start_station_id, end_station_id, direction, route)
                            
                                                
                                                                                 
                                                                     
                                                    
                                                                      
                                            
                                                
                                                                
                                               
                                                                                                        
                                                              
                                                                                

                                                                        
                                                                             
           
    limit = 24 * 60 * 60 * 2
    tomorrow_select = tomorrow_select2 = tomorrow_where = tomorrow_order = ""
    tomorrow_calendar_date_where = f"AND (calendar_date_today.date = date('{now_date}'))"
    if include_tomorrow:
        _LOGGER.debug("Includes Tomorrow")
        limit = int(limit / 2 * 3)
        tomorrow_name = tomorrow.strftime("%A").lower()
        tomorrow_select = f"( select calendar.{tomorrow_name} - ( select case when (select 1 from calendar_dates where service_id=trip.service_id and date = '{tomorrow_date}' and exception_type = 2 ) == 1 then 1 else 0 end) ) as tomorrow,"
        tomorrow_where = f"OR calendar.{tomorrow_name} = 1"
        tomorrow_order = f"calendar.{tomorrow_name} DESC,"
        tomorrow_calendar_date_where = f"AND (calendar_date_today.date = date('{now_date}') or calendar_date_today.date = date('{now_date}','+1 day') )"
        tomorrow_select2 = f"CASE WHEN date('{now_date}') < calendar_date_today.date or date(origin_stop_time.departure_time) = '1970-01-02' THEN 1 else 0 END as tomorrow,"
    sql_query = f"""
        SELECT trip.trip_id, trip.route_id,trip.trip_headsign, trip.direction_id,trip.trip_short_name,
               route.route_long_name,route.route_short_name,
        	   start_station.stop_id as origin_stop_id,
               start_station.stop_name as origin_stop_name,
               start_station.stop_timezone as origin_stop_timezone,
               agency.agency_timezone as agency_timezone,
               time(origin_stop_time.arrival_time) AS origin_arrival_time,
               time(origin_stop_time.departure_time) AS origin_depart_time,
               date(origin_stop_time.departure_time) AS origin_depart_date,
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
               time(destination_stop_time.departure_time) AS dest_depart_time,
               destination_stop_time.drop_off_type AS dest_drop_off_type,
               destination_stop_time.pickup_type AS dest_pickup_type,
               destination_stop_time.shape_dist_traveled AS dest_dist_traveled,
               destination_stop_time.stop_headsign AS dest_stop_headsign,
               destination_stop_time.stop_sequence AS dest_stop_sequence,
               destination_stop_time.timepoint AS dest_stop_timepoint,
               calendar.{yesterday.strftime("%A").lower()} AS yesterday,
               ( select calendar.{now.strftime("%A").lower()} - (  select case when (select 1 from calendar_dates where service_id=trip.service_id and date = date('{now_date}') and exception_type = 2 ) == 1 then 1 else 0 end  ) ) as today,
               {tomorrow_select}
               calendar.start_date AS start_date,
               calendar.end_date AS end_date,
               "" as calendar_date,
               0 as today_cd
        FROM trips trip
        INNER JOIN calendar calendar
                   ON trip.service_id = calendar.service_id
        INNER JOIN stop_times origin_stop_time
                   ON trip.trip_id = origin_stop_time.trip_id
        INNER JOIN stops start_station
                   ON origin_stop_time.stop_id = start_station.stop_id
        INNER JOIN stop_times destination_stop_time
                   ON trip.trip_id = destination_stop_time.trip_id
        INNER JOIN stops end_station
                   ON destination_stop_time.stop_id = end_station.stop_id
        INNER JOIN routes route
                   ON route.route_id = trip.route_id 
        INNER JOIN agency agency
                   ON route.agency_id = agency.agency_id                 
		WHERE {route_type_where}
        {start_station_where}
        {end_station_where}
        {direction_where}
        {route_where}
        {line_where}
        AND origin_stop_sequence < dest_stop_sequence
        AND calendar.start_date <= date('{now_date}')
        AND calendar.end_date >= date('{now_date}')
		UNION ALL
	    SELECT trip.trip_id, trip.route_id,trip.trip_headsign, trip.direction_id,trip.trip_short_name,
               route.route_long_name,route.route_short_name,
               start_station.stop_id as origin_stop_id,
               start_station.stop_name as origin_stop_name,
               start_station.stop_timezone as origin_stop_timezone,
               agency.agency_timezone as agency_timezone,
               time(origin_stop_time.arrival_time) AS origin_arrival_time,
               time(origin_stop_time.departure_time) AS origin_depart_time,
               date(origin_stop_time.departure_time) AS origin_depart_date,
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
               time(destination_stop_time.departure_time) AS dest_depart_time,
               destination_stop_time.drop_off_type AS dest_drop_off_type,
               destination_stop_time.pickup_type AS dest_pickup_type,
               destination_stop_time.shape_dist_traveled AS dest_dist_traveled,
               destination_stop_time.stop_headsign AS dest_stop_headsign,
               destination_stop_time.stop_sequence AS dest_stop_sequence,
               destination_stop_time.timepoint AS dest_stop_timepoint,
               0 AS yesterday,
               0 AS today,
               {tomorrow_select2}
               date('{now_date}') AS start_date,
               date('{now_date}') AS end_date,
               calendar_date_today.date as calendar_date,
               calendar_date_today.exception_type as today_cd
        FROM trips trip
        INNER JOIN stop_times origin_stop_time
                   ON trip.trip_id = origin_stop_time.trip_id
        INNER JOIN stops start_station
                   ON origin_stop_time.stop_id = start_station.stop_id
        INNER JOIN stop_times destination_stop_time
                   ON trip.trip_id = destination_stop_time.trip_id
        INNER JOIN stops end_station
                   ON destination_stop_time.stop_id = end_station.stop_id
        INNER JOIN routes route
                   ON route.route_id = trip.route_id 
        INNER JOIN calendar_dates calendar_date_today
				   ON trip.service_id = calendar_date_today.service_id
        INNER JOIN agency agency
                   ON route.agency_id = agency.agency_id                    
		WHERE {route_type_where}
        {start_station_where}
        {end_station_where}
        {direction_where}
        {route_where}
        {line_where}
		AND origin_stop_sequence < dest_stop_sequence
        AND today_cd = 1
		{tomorrow_calendar_date_where}
        -- a loop calls at one stop twice, so a trip can reach the asked
        -- stop on two of its records: the earlier arrival is kept below
        ORDER BY calendar_date,origin_depart_date, today_cd, origin_depart_time, dest_stop_sequence
        """  # noqa: S608
    # Create lookup timetable for today and possibly tomorrow, taking into
    # account any departures from yesterday scheduled after midnight,
    # as long as all departures are within the calendar date range.
    query_params = {
        "tomorrow_select": tomorrow_select,
        "route_type_where": route_type_where,
        "start_station_where": start_station_where,
        "end_station_where": end_station_where,
        "direction_where": direction_where,
        "route_where": route_where,
        "line_where": line_where,
        "tomorrow_select2": tomorrow_select2,
        "tomorrow_calendar_date_where": tomorrow_calendar_date_where,
        "origin_station_id": start_station_id,
        "end_station_id": end_station_id,
        "direction": direction,
        "route": route,
        "line": line,
        "limit": limit,
        "route_type": route_type,
        "now_date": now_date,
    }

    log_params = {
        **query_params,
    }

    #_LOGGER.debug("SQL statement:\n%s", sql_query)
    #_LOGGER.debug("SQL parameters:\n%s", log_params)      
                  
                                                         
                                            
    with schedule.engine.connect() as conn:
        result = conn.execute(
            text(sql_query),
            {
                "origin_station_id": start_station_id,
                "end_station_id": end_station_id,
                "direction": direction,
                "route": route,
                "line": line,
                "limit": limit,
                "route_type": route_type,
            },
        )
        rows = result.fetchall()

    return [row_cursor._asdict() for row_cursor in rows], start_station_id


def _interpret_departure_rows(hass, rows, start_station_id, now, now_local_tz,
                               now_date_local_tz, now_time, yesterday_date,
                               tomorrow, tomorrow_date, tomorrow_date_local_tz):
    """Turn raw SQL-shaped rows into the `next_departure` dict.

    No database, no schedule object: `rows` only needs to be a list of
    plain dicts shaped like `_fetch_departure_rows`' output. This is what
    a test builds by hand to simulate a specific condition (a midnight
    crossing, a yesterday-late departure, ...) without a real GTFS feed.
    """
    timetable = {}
    yesterday_start = today_start = tomorrow_start = None
    yesterday_last = today_last = ""        
    for row in rows:
        #_LOGGER.debug("Row in cursor: %s", row)
        if row["yesterday"] == 1 and yesterday_date >= row["start_date"]:
            _LOGGER.debug("Row in cursor added to yesterday")
            extras = {"day": "yesterday", "first": None, "last": False}
            if yesterday_start is None:
                yesterday_start = row["origin_depart_date"]
            if yesterday_start != row["origin_depart_date"]:
                idx = (
                    f"{now_date_local_tz} {row['origin_depart_time']}",
                    str(row["trip_id"]),
                )
                if idx in timetable:
                    _LOGGER.warning("Duplicate timetable key for yesterday: %s, and trip_id: %s", idx, row['trip_id'])
                else:
                    timetable[idx] = {**row, **extras}
                    yesterday_last = idx
        if (
            (
                (row["today"] == 1 or row["today_cd"] == 1)
                and ("tomorrow" not in row or row["tomorrow"] == 0)
            )
            or (
                row["today"] == 1
                and row["calendar_date"] == ""
            )
            ):
            _LOGGER.debug("Row in cursor added to today")
            extras = {"day": "today", "first": False, "last": False}
            if today_start is None:
                today_start = row["origin_depart_date"]
                extras["first"] = True
            if today_start == row["origin_depart_date"]:
                idx_prefix = now_date_local_tz
            else:
                idx_prefix = tomorrow_date_local_tz
            idx = (
                f"{idx_prefix} {row['origin_depart_time']}",
                str(row["trip_id"]),
            )
            if idx in timetable:
                _LOGGER.warning(
                    "Duplicate timetable key for today: %s, and trip_id: %s",
                    idx,
                    row["trip_id"],
                )
            else:
                timetable[idx] = {**row, **extras}
                today_last = idx      
        if (
            "tomorrow" in row
            and row["tomorrow"] == 1
            and ( tomorrow_date <= row["end_date"] or tomorrow_date == row["calendar_date"] or row["origin_depart_date"]=="1970-01-02")
        ):
            _LOGGER.debug("Row in cursor added to tomorrow")
            extras = {"day": "tomorrow", "first": False, "last": None}
            if tomorrow_start is None:
                tomorrow_start = row["origin_depart_date"]
                extras["first"] = True
            if tomorrow_start == row["origin_depart_date"]:
                idx_prefix = tomorrow_date_local_tz
            idx = (
                f"{idx_prefix} {row['origin_depart_time']}",
                str(row["trip_id"]),
            )
            if idx in timetable:
                _LOGGER.warning(
                    "Duplicate timetable key for tomorrow: %s, and trip_id: %s",
                    idx,
                    row["trip_id"],
                )
            else:
                timetable[idx] = {**row, **extras}
    # Flag last departures.
    for idx in filter(None, [yesterday_last, today_last]):
        timetable[idx]["last"] = True
    item = {}
    for key in sorted(timetable.keys()):
        if datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S") > now:
            item = timetable[key]
            _LOGGER.info(
                "Departure(s) found for station %s @ %s -> %s", start_station_id, key, item
            )
            break
    _LOGGER.debug("Item(s) from SQL: %s", item)
    
    if item == {}:
        # No departure to show. Keep returning an empty dict: callers test this
        # value for truth and then read the fields of a real departure, so a
        # non-empty "there is nothing" would be read as a departure and crash.
        # The date of the next service is published separately, by the
        # coordinator, through get_next_service_date.
        _LOGGER.info("No items found in gtfs")
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
        timezone_dest = dt_util.get_time_zone(item["orig_stop_timezone"]) 
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
    item={}
    for key in sorted(timetable.keys()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        #_LOGGER.debug ("Upcoming_departure_in_defined_timezone: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, ix: %s", upcoming, now_local_tz, key, ix)
        if upcoming > now_local_tz:
            if ix == 0 :
                _LOGGER.debug("Resetting item")
                item = timetable[key]
                ix = ix + 1
            _LOGGER.debug("Adding departure in defined timezone: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, ix: %s", upcoming, now_local_tz, key, ix)
            timetable_remaining.append(dt_util.as_utc(upcoming).isoformat())   
    _LOGGER.debug("Timetable Remaining Departures on this Start/Stop: %s", timetable_remaining)
    if item == {}:
        # No departure to show. Keep returning an empty dict: callers test this
        # value for truth and then read the fields of a real departure, so a
        # non-empty "there is nothing" would be read as a departure and crash.
        # The date of the next service is published separately, by the
        # coordinator, through get_next_service_date.
        _LOGGER.info("No items found in gtfs")
        return {}
    
    # create upcoming timetable with line info, headsign and trips
    timetable_remaining_line = []
    timetable_remaining_headsign = []
    timetable_upcoming_trips = []
    timetable_upcoming_arrivals = []
    timetable_upcoming_durations = []
    for key, value in sorted(timetable.items()):
        upcoming = datetime.datetime.strptime(key[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone)
        upcoming_arrival = datetime.datetime.combine(
            upcoming.date(),
            datetime.datetime.strptime(value["dest_arrival_time"],"%H:%M:%S").time()).replace(tzinfo=timezone_dest)
        # Arrival after midnight -> next calendar day
        if upcoming_arrival.time() < upcoming.time():
            upcoming_arrival += datetime.timedelta(days=1)
        #_LOGGER.debug ("Upcoming list values for departure in defined tz: %s, Now_in_defined_timezone_plus_offset: %s, key: %s, value %s", upcoming, now_local_tz, key, value)
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
            
    #_LOGGER.debug(
    #    "Timetable Remaining Departures on this Start/Stop, per line: %s",
    #    timetable_remaining_line,
    #)
    #_LOGGER.debug(
    #    "Timetable Remaining Departures on this Start/Stop, with headsign: %s",
    #    timetable_remaining_headsign,
    #)
    #_LOGGER.debug(
    #    "Timetable Remaining Trips on this Start/Stop: %s",
    #    timetable_upcoming_trips,
    #)
    #_LOGGER.debug(
    #    "Timetable arrival times on this Start/Stop: %s",
    #    timetable_upcoming_arrivals,
    #)


    # Format arrival and departure dates and times, accounting for the
    # possibility of times crossing over midnight.
    _tomorrow = False
    if item.get("tomorrow") == 1 or item.get("calendar_date") > now_date_local_tz or item.get("origin_depart_date") != '1970-01-01' :
        _tomorrow = True
    _LOGGER.debug("Time is 'tomorrow': %s ,based on -> tomorrow_val: %s, calendar_date val: %s, now_date_local_tz val: %s", _tomorrow, item.get("tomorrow"),item.get("calendar_date"), now_date_local_tz)        
    origin_arrival = now
    dest_arrival = now
    origin_depart_time = f"{now_date_local_tz} {item['origin_depart_time']}"
    if _tomorrow and now_time > item['origin_depart_time']:
        origin_arrival = tomorrow
        dest_arrival = tomorrow
        origin_depart_time = f"{tomorrow_date} {item['origin_depart_time']}"
    
    if item["origin_arrival_time"] > item["origin_depart_time"]:
        origin_arrival -= datetime.timedelta(days=1)
    origin_arrival_time = (
        f"{origin_arrival.strftime(dt_util.DATE_STR_FORMAT)} "
        f"{item['origin_arrival_time']}"
    )

    if item["dest_arrival_time"] < item["origin_depart_time"]:
        dest_arrival += datetime.timedelta(days=1)   
    dest_arrival_time = (
        f"{dest_arrival.strftime(dt_util.DATE_STR_FORMAT)} {item['dest_arrival_time']}"
    )

    dest_depart = dest_arrival
    if item["dest_depart_time"] < item["dest_arrival_time"]:
        dest_depart += datetime.timedelta(days=1)
    dest_depart_time = (
        f"{dest_depart.strftime(dt_util.DATE_STR_FORMAT)} {item['dest_depart_time']}"
    )
 
    _LOGGER.debug("Orig depart time: %s", origin_depart_time)
    
    depart_time = dt_util.parse_datetime(origin_depart_time).replace(tzinfo=timezone)
    arrival_time = dt_util.parse_datetime(dest_arrival_time).replace(tzinfo=timezone_dest)
    origin_arrival_time = dt_util.as_utc(datetime.datetime.strptime(origin_arrival_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    origin_depart_time = dt_util.as_utc(datetime.datetime.strptime(origin_depart_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    dest_arrival_time = dt_util.as_utc(datetime.datetime.strptime(dest_arrival_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    dest_depart_time = dt_util.as_utc(datetime.datetime.strptime(dest_depart_time, "%Y-%m-%d %H:%M:%S")).isoformat()
    
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
    }
    
    return data_returned



def get_next_departure(hass, _data):
    _LOGGER.debug("Get next departure with data: %s", _data)
    if check_extracting(hass, _data['gtfs_dir'],_data['file']):
        _LOGGER.debug("Cannot get next departures on this datasource as still unpacking: %s", _data["file"])
        return {}

    """Get next departures from data."""

    schedule = _data["schedule"]
    # get_gtfs hands back a sentinel string or None when the datasource is
    # unusable (zip or sqlite missing, dates all in the future): querying
    # that raises in SQLAlchemy, far from the cause, on every update.
    # Matched by shape, not by class: anything schedule-shaped may query
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no departures", _data["file"], schedule or "empty")
        return {}
    route_type = _data["route_type"]

    # What the entry was set up with, beyond the stop pair: the direction it
    # runs in, the route it was picked on, and, for trains, the line code.
    # Read here so the query stays a pure function of its arguments.
    direction = str(_data.get("direction", "") or "")
    direction = int(direction) if direction in ("0", "1") else None
    route = str(_data.get("route", "") or "").split(": ")[0].strip()
    route = route if route and route != "train" else None
    line = str(_data.get("line", "") or "").strip() or None

    offset = _data["offset"]
    include_tomorrow = _data["include_tomorrow"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_local_tz = dt_util.now() + datetime.timedelta(minutes=offset)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    now_date_local_tz = now_local_tz.strftime(dt_util.DATE_STR_FORMAT)
    now_time = now.strftime(TIME_STR_FORMAT)
    yesterday = now - datetime.timedelta(days=1)
    yesterday_date = yesterday.strftime(dt_util.DATE_STR_FORMAT)
    tomorrow = now + datetime.timedelta(days=1)
    tomorrow_local_tz = dt_util.now() + datetime.timedelta(minutes=offset) + datetime.timedelta(days=1) 
    tomorrow_date = tomorrow.strftime(dt_util.DATE_STR_FORMAT)
    tomorrow_date_local_tz = tomorrow_local_tz.strftime(dt_util.DATE_STR_FORMAT)

    # Fetch all departures for yesterday, today and optionally tomorrow,
    # up to an overkill maximum in case of a departure every minute for those
    # days.
    rows, start_station_id = _fetch_departure_rows(
        route_type, _data["origin"], _data["destination"], include_tomorrow,
        now, now_date, yesterday, tomorrow, tomorrow_date, schedule,
        direction=direction, route=route, line=line,
    )

    return _interpret_departure_rows(
        hass, rows, start_station_id, now, now_local_tz,
        now_date_local_tz, now_time, yesterday_date,
        tomorrow, tomorrow_date, tomorrow_date_local_tz,
    )


def source_meta_path(zip_path):
    """Where the sidecar of a source zip lives: right beside it."""
    return zip_path + ".meta.json"


def source_meta(zip_path):
    """What the sidecar remembers of the last successful download, or {}.

    The sidecar is a cache of derived facts, never primary data: deleting
    it costs at most one refresh that could have been skipped, so a missing
    or unreadable file is an empty answer, not an error.
    """
    try:
        with open(source_meta_path(zip_path), encoding="utf-8") as meta_file:
            meta = json.load(meta_file)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def stage_zip(response, zip_path):
    """Write a downloaded feed beside its target and verify it is a zip.

    A moved or renumbered url often keeps answering HTTP 200 with whatever
    now lives there: an error page, a stray protobuf, fifteen bytes of
    nothing. The kept zip is the only full record of the feed, so nothing
    replaces it before proving to be a zip. Returns the staged path, or
    None when the payload is not one.
    """
    staged = zip_path + ".new"
    with open(staged, "wb") as out:
        out.write(response.content)
    if not zipfile.is_zipfile(staged):
        _LOGGER.error(
            "The download from %s is not a zip file (%s bytes), "
            "keeping the current data", response.url, len(response.content))
        try:
            os.remove(staged)
        except OSError:
            pass
        return None
    return staged


def adopt_zip(response, staged, zip_path):
    """Swap the verified download in and record what it was.

    The sidecar keeps the validators the host sent, so the next check can
    ask "did this change" for the price of one conditional request, and
    the hash, for the hosts that send no validators at all.
    """
    os.replace(staged, zip_path)
    meta = {
        "url": str(response.url),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "size": len(response.content),
        "downloaded_at": dt_util.utcnow().isoformat(),
    }
    try:
        with open(source_meta_path(zip_path), "w", encoding="utf-8") as out:
            json.dump(meta, out, indent=1)
    except OSError as ex:
        _LOGGER.warning("Could not record the download of %s: %s", zip_path, ex)


def get_gtfs(hass, path, data, update=False):
    _LOGGER.debug("Getting gtfs with data: %s", data)
    _headers = None
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    url = data["url"]
    if data.get(CONF_API_KEY_LOCATION, None) == "query_string":
      if data.get(CONF_API_KEY, None):
        url = url + "?" + data.get(CONF_API_KEY_NAME, "api_key") + "=" + data[CONF_API_KEY]
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
    if update and data["extract_from"] == "zip" and os.path.exists(os.path.join(gtfs_dir, file)) and os.path.exists(os.path.join(gtfs_dir, sqlite)):
        os.remove(os.path.join(gtfs_dir, sqlite))      
    if data["extract_from"] == "zip":
        if not os.path.exists(os.path.join(gtfs_dir, file)):
            _LOGGER.error("The given GTFS zipfile was not found")
            return "no_zip_file"
    if data["extract_from"] == "url":
        if update or not os.path.exists(os.path.join(gtfs_dir, file)):
            try:
                # some providers answer 403 to the default requests user agent;
                # _headers is None unless an api key is used in a header
                _get_headers = dict(_headers or {})
                _get_headers.setdefault("User-Agent", "home-assistant-gtfs2")
                r = requests.get(url,headers=_get_headers, allow_redirects=True,timeout=15)
                r.raise_for_status()
                # verify before removing anything: a download that turns out
                # not to be a zip must leave the datasource as it was
                staged = stage_zip(r, os.path.join(gtfs_dir, file))
                if staged is None:
                    return "no_data_file"
                if _pending_remove:
                    remove_datasource(hass, path, filename, True)
                adopt_zip(r, staged, os.path.join(gtfs_dir, file))
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("The given URL or GTFS data file/folder was not found: %s", ex)
                return "no_data_file"
    
    # if update (servicecall) then check if new file does not only have future dates
    if check_source_dates:
        if update and not check_calendar_dates_from_zip(gtfs_dir, file):
            _LOGGER.info('New file contains only dates in the future, extracting terminated')
            return
    
    (gtfs_root, _) = os.path.splitext(file)    
    sqlite_file = f"{gtfs_root}.sqlite?check_same_thread=False&timeout=60"
    joined_path = os.path.join(gtfs_dir, sqlite_file)  

    gtfs = pygtfs.Schedule(joined_path)
   
    if not gtfs.feeds: 
        if data.get("clean_feed_info", False):
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
    clean = remove_from_zip(remove_file,gtfs_dir, file[:-4])
    if os.fork() != 0:
        return
    pygtfs.append_feed(gtfs, os.path.join(gtfs_dir, file))
    check_datasource_index(hass, gtfs, gtfs_dir, file[:-4])
    repair_trip_directions(gtfs)
    
def build_scratch_database(gtfs_dir, file, scratch_file, clean_feed_info=False,
                           only_routes=None):
    """Unpack a zip into the scratch database, synchronously.

    The counterpart of extract_from_zip, minus the fork: the caller is already
    off the event loop, and an import has to be finished before its routes can
    be copied out. Nothing here touches the real database.

    only_routes cuts the feed down to those routes before pygtfs sees it.
    pygtfs pays per row, so this is what makes a national feed usable:
    measured on gtfs-nl.zip, 15.1 M stop_times filtered in 40 s down to a
    feed pygtfs imports in half a second, where the full import built a
    2.6 GB scratch file. When the filter cannot run, the whole feed is
    imported as before: only slower, never wrong. The filtered path also
    leaves the source zip untouched, where the historic path strips tables
    out of it in place.

    Returns True when the scratch file holds a feed.
    """
    feed_file = os.path.join(gtfs_dir, file)
    filtered = None
    if only_routes:
        candidate = scratch_file + ".zip"
        if filter_gtfs_zip(feed_file, candidate, only_routes,
                           drop_feed_info=clean_feed_info) is not None:
            filtered = candidate
            feed_file = candidate
        else:
            _LOGGER.warning(
                "Could not filter %s, importing the whole feed instead", file)
    if filtered is None:
        drop = ['shapes.txt', 'transfers.txt', 'fare_attributes.txt',
                'levels.txt', 'pathways.txt', 'translations.txt']
        if clean_feed_info:
            drop.append('feed_info.txt')
        remove_from_zip(drop, gtfs_dir, file[:-4])

    # same connection arguments as the real database, so the scratch one
    # behaves identically under a timeout
    conn = f"{scratch_file}?check_same_thread=False&timeout=60"
    try:
        scratch = pygtfs.Schedule(conn)
        pygtfs.append_feed(scratch, feed_file)
        ok = bool(scratch.feeds)
        if ok:
            # routes are copied out of this file into the real database, so
            # directions have to be right before the copy, not after
            repair_trip_directions(scratch)
        # the session holds a connection the pool does not know about, so
        # closing only the engine leaves the file open until garbage
        # collection - long enough for the cleanup below to fail on Windows
        scratch.session.close()
        scratch.engine.dispose()
        del scratch
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Could not unpack %s into the import database: %s", file, ex)
        return False
    finally:
        if filtered and os.path.exists(filtered):
            # pygtfs read the filtered zip through a handle it only drops on
            # collection, and Windows refuses to delete a file still open
            gc.collect()
            try:
                os.remove(filtered)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", filtered, ex)
    if not ok:
        _LOGGER.error("The import database holds no feed after unpacking %s", file)
    return ok


def _source_request(data):
    """The url and headers a source's zip is fetched with, api key included."""
    url = data["url"]
    headers = {"User-Agent": "home-assistant-gtfs2"}
    key = data.get(CONF_API_KEY)
    if key and data.get(CONF_API_KEY_LOCATION) == "query_string":
        url = url + "?" + (data.get(CONF_API_KEY_NAME) or "api_key") + "=" + key
    if key and data.get(CONF_API_KEY_LOCATION) == "header":
        headers[(data.get(CONF_API_KEY_NAME) or "api_key")] = key
    return url, headers


def ensure_source_zip(hass, path, data):
    """Make sure the source zip is in place, without starting any import.

    The front half of get_gtfs: same checks, same download, same error codes,
    minus the part that unpacks the feed into a database. The config flow
    calls this when a source is submitted, so the lines can be chosen from
    the zip alone and the import can wait until it knows which routes to
    keep - on a national feed, the difference between a flow that continues
    and one that ends on a progress notification.

    Returns None when the zip is ready, else the code the flow already
    words: "extracting", "no_zip_file", "no_data_file".
    """
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    filename = data["file"]
    zip_path = os.path.join(gtfs_dir, filename + ".zip")
    if check_extracting(hass, path, filename):
        return "extracting"
    if data["extract_from"] == "zip":
        return None if os.path.exists(zip_path) else "no_zip_file"
    if not os.path.exists(zip_path):
        try:
            url, headers = _source_request(data)
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
            r.raise_for_status()
            staged = stage_zip(r, zip_path)
            if staged is None:
                return "no_data_file"
            adopt_zip(r, staged, zip_path)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("The given URL or GTFS data file/folder was not found: %s", ex)
            return "no_data_file"
    return None


def open_datasource(gtfs_dir, filename):
    """Open a datasource that is known to exist, with no extracting gate.

    get_gtfs refuses to answer while anything writes to the file, because a
    journal used to mean the legacy fork was still building it in place. In
    the two database model the real file receives short legitimate writes
    while the sensors live - an index being added, an intern - so a journal
    can exist for milliseconds and means nothing. Callers that just proved
    the datasource exists, like the step after a finished import, open it
    here instead of walking into that gate.

    Returns the schedule, or None when the file is not there.
    """
    sqlite_file = os.path.join(gtfs_dir, filename + ".sqlite")
    if not os.path.exists(sqlite_file):
        _LOGGER.error("No datasource to open: %s", sqlite_file)
        return None
    return pygtfs.Schedule(f"{sqlite_file}?check_same_thread=False&timeout=60")


def refresh_datasource(hass, path, data):
    """Refresh a datasource from its source, keeping the sensors served.

    The legacy update rebuilt the real database in place: on a large feed
    the sensors read a half-built file for as long as the import took. Here
    the fresh feed is filtered down to the routes the database actually
    follows, unpacked into the scratch database, copied into a new real
    file built beside the old one, and the two are swapped in one rename.
    The coordinators reopen the file on their next cycle, so they only ever
    see the old complete data or the new complete data.

    Falls back to the legacy full extract when there is nothing to refresh
    from: no database yet, or one that follows no route.

    Returns {route_id: stop_times} on success, False on failure, and
    whatever get_gtfs returns when it falls back.
    """
    gtfs_dir = hass.config.path(path)
    filename = data["file"]
    real = real_path(gtfs_dir, filename)
    routes = sorted(routes_in(real))
    if not routes:
        _LOGGER.info("Datasource %s follows no route yet, extracting it whole",
                     filename)
        return get_gtfs(hass, path, data, True)

    zip_name = filename + ".zip"
    zip_path = os.path.join(gtfs_dir, zip_name)
    if data.get("extract_from", "url") == "url":
        # download beside the current zip and swap only once complete and
        # proven to be a zip: the zip is the only full record of the feed
        # and must survive a failed or hijacked download
        try:
            url, headers = _source_request(data)
            r = requests.get(url, headers=headers, allow_redirects=True, timeout=15)
            r.raise_for_status()
            staged = stage_zip(r, zip_path)
            if staged is None:
                return False
            adopt_zip(r, staged, zip_path)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Could not download %s: %s", data.get("url"), ex)
            fresh = zip_path + ".new"
            if os.path.exists(fresh):
                try:
                    os.remove(fresh)
                except OSError:
                    pass
            return False
    if not os.path.exists(zip_path):
        _LOGGER.error("No source zip to refresh %s from", filename)
        return False
    if data.get("check_source_dates", False) and zip_only_future_dates(zip_path):
        _LOGGER.info("New file contains only dates in the future, "
                     "keeping the current data")
        return False

    # the new real database is built under its own datasource name, so every
    # existing helper works on it unchanged and nothing it does can touch the
    # file the sensors are reading
    staging = filename + ".refresh"
    new_real = real_path(gtfs_dir, staging)

    def _build(scratch_file):
        return build_scratch_database(
            gtfs_dir, zip_name, scratch_file,
            data.get("clean_feed_info", False), only_routes=routes)

    try:
        if os.path.exists(new_real):
            os.remove(new_real)
        added = import_routes(gtfs_dir, staging, routes, _build)
        if added is None or len(added) < len(routes):
            _LOGGER.error("Refresh of %s aborted, the current data stays: %s",
                          filename, added)
            return False
        # intern only: everything in this file was just copied on purpose
        optimise_datasource(gtfs_dir, staging)
        os.replace(new_real, real)
    finally:
        for leftover in (new_real, new_real + "-journal"):
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError as ex:
                    _LOGGER.warning("Could not remove %s: %s", leftover, ex)
    _LOGGER.info("Refreshed datasource %s from its source: %s stop_times "
                 "per route", filename, added)
    return added

def check_calendar_dates_from_zip(gtfs_dir,file):
    _LOGGER.debug("Checking if file contains only future data: %s ", file)
    filename = os.path.join(gtfs_dir, file)
    # Rename existing sqlite if existing (i.e. in case of a fresh install)
    if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
        os.rename (os.path.join(gtfs_dir, file[:-4] + '.sqlite'), os.path.join(gtfs_dir, file[:-4] + '.sqlite_current'))
    #Load the ZIP archive
    zin = zipfile.ZipFile (f"{os.path.join(gtfs_dir, filename)}", 'r')
    check_list=[]
    try:
        for item in zin.infolist():
            if item.filename[0:8] == 'calendar' :
                if item.filename == 'calendar.txt':
                    column = 'start_date'
                else:
                    column = 'date'
                with open(zin.extract(item.filename)) as f:
                    header = f.readline().strip('\n')   #
                    data = f.readlines() 
                    index =header.replace('"','').split(',').index(column)           
                    list = []
                    for line in data:
                        list.append(line.split(',')[index])
                    check_list.append(min(list))
        min_date = datetime.datetime.strptime(min(check_list),"%Y%m%d")
        _LOGGER.debug("Youngest calender date from new files: %s, is: %s", check_list, min_date)
        if min_date > datetime.datetime.now()  :
            _LOGGER.info("New file contains only dates in the future, keeping current")
            if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
                os.remove(os.path.join(gtfs_dir, file[:-4] + ".sqlite"))
            os.rename (os.path.join(gtfs_dir, file[:-4] + '.sqlite_current'), os.path.join(gtfs_dir, file[:-4] + '.sqlite'))
            return False
    except Exception as ex:
        _LOGGER.error("Error getting earliest dates from zip, continuing with extract, error: %s", ex)
        _LOGGER.debug(f"Removing/restoring sqlite after error")
        if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
            os.remove(os.path.join(gtfs_dir, file[:-4] + ".sqlite"))
        if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite")):
            os.rename (os.path.join(gtfs_dir, file[:-4] + '.sqlite'), os.path.join(gtfs_dir, file[:-4] + '.sqlite_current'))
        return False
    _LOGGER.debug(f"New file is not containing only newer dates, removing current/copied sqlite")    
    if os.path.exists(os.path.join(gtfs_dir, file[:-4] + ".sqlite_current")):
        os.remove(os.path.join(gtfs_dir, file[:-4] + ".sqlite_current"))
    return True

def remove_from_zip(delmelist,gtfs_dir,file):
    _LOGGER.debug("Removing data: %s , from zipfile: %s", delmelist, file)
    tempfile = file + "_temp.zip"
    tempfile_out = file + "_temp_out.zip"
    filename = file + ".zip"
    os.rename (os.path.join(gtfs_dir, filename), os.path.join(gtfs_dir, tempfile))
    # Load the ZIP archive
    try: 
        zin = zipfile.ZipFile (f"{os.path.join(gtfs_dir, tempfile)}", 'r')
        zout = zipfile.ZipFile (f"{os.path.join(gtfs_dir, tempfile_out)}", 'w')
        for item in zin.infolist():
            buffer = zin.read(item.filename)
            if (item.filename not in delmelist):
                zout.writestr(item, buffer)
        zout.close()
        zin.close()
        os.rename(os.path.join(gtfs_dir, tempfile_out), os.path.join(gtfs_dir, filename))
        os.remove(os.path.join(gtfs_dir, tempfile)) 
    except Exception as ex:  # pylint: disable=broad-except
        print('Something went wrong with the zipfile... : ', ex)
        return     


def get_routes_in_zip(gtfs_dir, filename):
    """The route_ids the source zip declares, read without unpacking it.

    The zip kept beside a datasource is the only complete record of the feed:
    a prune leaves routes and stops in place but empties everything that links
    them, so asking the database which lines exist can only ever return the
    lines it already knows. Reading the source answers for the whole network.

    routes.txt is small - tens to a few thousand lines - and only one column is
    needed, so the member is streamed and decoded on the fly rather than
    extracted to disk.

    Returns an empty set when the zip is gone or unreadable, which the caller
    treats as "cannot tell", not as "no routes".
    """
    path = os.path.join(gtfs_dir, filename + ".zip")
    if not os.path.exists(path):
        _LOGGER.debug("No source zip beside datasource %s", filename)
        return set()
    try:
        with zipfile.ZipFile(path) as zin:
            member = next((n for n in zin.namelist()
                           if n.rsplit("/", 1)[-1] == "routes.txt"), None)
            if member is None:
                _LOGGER.warning("No routes.txt in %s", path)
                return set()
            with zin.open(member) as fh:
                # utf-8-sig: GTFS files routinely carry a byte order mark, and
                # it would otherwise end up glued to the first column name
                reader = csv.DictReader(io.TextIOWrapper(fh, "utf-8-sig"))
                if not reader.fieldnames or "route_id" not in reader.fieldnames:
                    _LOGGER.warning("No route_id column in %s", member)
                    return set()
                return {row["route_id"] for row in reader if row.get("route_id")}
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read routes from %s: %s", path, ex)
        return set()


def get_agencies_in_zip(gtfs_dir, filename):
    """The agencies of the source zip, shaped like get_agency_list's rows.

    What the agency step shows when no database exists yet: the feed is the
    only thing there is to read, and nothing may start importing before the
    lines are chosen.
    """
    rows = read_zip_agencies(os.path.join(gtfs_dir, filename + ".zip"))
    rows.sort(key=lambda row: str(row.get("agency_name")))
    return [f"{row.get('agency_id') or '0'}: {row['agency_name']}"
            for row in rows]


def get_route_options_from_zip(gtfs_dir, filename, agency=None):
    """The route selector options, read from the source zip.

    Same "route_type##route_id##label" values get_route_list builds from the
    database, and every one carries the "##pruned" flag: no timetable is
    loaded yet, and that flag is exactly what routes the flow through the
    screen that imports one. agency narrows to one agency_id; "0" and None
    mean the whole feed.
    """
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    if agency and agency != "0":
        rows = [row for row in rows if (row.get("agency_id") or "0") == agency]
    options = []
    for row in rows:
        label = _route_label(row.get("route_short_name"),
                             row.get("route_long_name"),
                             route_id=row["route_id"])
        options.append(
            f"{row.get('route_type') or '99'}##{row['route_id']}##{label}##pruned")
    return sorted(options, key=lambda value: _natural(value.split("##")[2]))


def get_route_labels_from_zip(gtfs_dir, filename, route_ids):
    """get_route_labels when there is no database to ask: names from the zip."""
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    known = {row["route_id"]: _route_label(row.get("route_short_name"),
                                           row.get("route_long_name"),
                                           route_id=row["route_id"])
             for row in rows}
    return {r: known.get(r, r) for r in route_ids}


def routes_in_zip_for_agency(gtfs_dir, filename, route_ids, agency=None):
    """Cut a list of route_ids down to one agency's, as routes.txt records it.

    The also-import list offers what the feed declares minus what is loaded;
    on a national feed that is thousands of lines from dozens of operators,
    when the operator was already named on the agency screen. "0" and None
    mean no cut.
    """
    if not agency or agency == "0":
        return route_ids
    rows = read_zip_routes(os.path.join(gtfs_dir, filename + ".zip"))
    owned = {row["route_id"] for row in rows
             if (row.get("agency_id") or "0") == agency}
    return [r for r in route_ids if r in owned]


def _says_something(part):
    """Whether a name part carries anything a reader can use.

    A line number is often nothing but digits, so digits count. What does not
    count is punctuation on its own: SNCF publishes 54 lines whose long name is
    the string " -", the two ends of a route it did not fill in, and showing
    that to the user is worse than showing nothing.
    """
    return any(character.isalnum() for character in str(part or ""))


def _route_endpoints(schedule, route_ids):
    """Where each of these lines starts and ends, as "A > B".

    Read from one trip per line, which is what the direction step already does
    on the screen after this one. It is only asked for the lines whose name is
    unusable, so the query stays small even on a national feed.
    """
    if not route_ids:
        return {}
    route_ids = sorted(route_ids)
    placeholders = ", ".join(f":e{i}" for i in range(len(route_ids)))
    sql = f"""
    with picked as (
        select route_id, min(trip_id) as trip_id
        from trips where route_id in ({placeholders}) group by route_id
    )
    select p.route_id, st.stop_sequence, s.stop_name
    from picked p
    inner join stop_times st on st.trip_id = p.trip_id
    inner join stops s on s.stop_id = st.stop_id
    """  # noqa: S608
    ends = {}
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {f"e{i}": r for i, r in enumerate(route_ids)}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # without this the label falls back to the route_id, which is what it
        # did before: ugly, but never empty
        _LOGGER.warning("Could not read the ends of %s routes: %s", len(route_ids), ex)
        return {}
    for route_id, sequence, name in rows:
        if not name:
            continue
        first, last = ends.get(route_id, (None, None))
        if first is None or sequence < first[0]:
            first = (sequence, name)
        if last is None or sequence > last[0]:
            last = (sequence, name)
        ends[route_id] = (first, last)
    return {route_id: f"{first[1]} > {last[1]}"
            for route_id, (first, last) in ends.items()
            if first and last and first[1] != last[1]}


def _route_label(short, long_name, endpoints=None, route_id=None):
    """What the user reads for one line: its number, then where it goes.

    The two parts are kept only if they say something, so a line named
    "INCONNU" against a long name of " -" no longer reads "INCONNU :  -". When
    the long name is the one missing, the two ends of the route take its place,
    which is the thing the user was looking for in the first place.
    """
    parts = [str(p) for p in (short, long_name)
             if p and str(p) != "None" and _says_something(p)]
    if len(parts) < 2 and endpoints:
        parts = parts[:1] + [endpoints]
    if parts:
        return " : ".join(parts)
    return str(route_id or "")


def _natural(label):
    """Sort key that reads 2 before 10, the way a line number is read."""
    out = []
    for chunk in re.split(r"(\d+)", str(label)):
        out.append((1, int(chunk)) if chunk.isdigit() else (0, chunk.lower()))
    return out


def get_route_labels(schedule, route_ids):
    """Readable names for route_ids, as {route_id: "41 : GARE - ESAT RODIN"}.

    routes survives a prune even when its trips do not, so these names are
    available for lines the datasource no longer carries any timetable for -
    which is exactly when they need to be offered back.
    """
    if not route_ids:
        return {}
    out = {}
    placeholders = ", ".join(f":r{i}" for i in range(len(route_ids)))
    sql = ("select route_id, route_short_name, route_long_name from routes "
           f"where route_id in ({placeholders})")  # noqa: S608
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                text(sql), {f"r{i}": r for i, r in enumerate(route_ids)}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not read route names: %s", ex)
        return {r: r for r in route_ids}
    rows = list(rows)
    needs_ends = [r[0] for r in rows if not _says_something(r[2])]
    endpoints = _route_endpoints(schedule, needs_ends)
    for route_id, short, long in rows:
        out[route_id] = _route_label(short, long, endpoints.get(route_id), route_id)
    # a route the feed declares but routes does not: keep it selectable
    for r in route_ids:
        out.setdefault(r, r)
    return {r: out[r] for r in route_ids}


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
    if data["agency"].split(': ')[0] != "0":
        agency_where = f"and r.agency_id = '{data['agency'].split(': ')[0]}'"
    if data["route_type"] != "99":
        route_type_where = f"and route_type = {data['route_type']}"
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
        params = {"q": "q"}
        params.update({f"pr{i}": r for i, r in enumerate(sorted(pruned))})
        rows = conn.execute(text(sql_routes), params).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        routes_list.append(list(row_cursor))
    # the lines whose long name says nothing get the two ends of the route
    # instead, read in one go rather than one query per line
    endpoints = _route_endpoints(
        schedule, [str(x[1]) for x in routes_list if not _says_something(x[3])])
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
    routes.sort(key=lambda value: _natural(value.split("##")[2]))
    _LOGGER.debug(f"routes: {routes}")
    return routes

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
    """
    _LOGGER.debug("Getting direction labels for route: %s", route_id)
    sql = """
    SELECT t.direction_id, s.stop_name, st.stop_sequence
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    inner join stops s on s.stop_id = st.stop_id
    where t.trip_id in (
        select trip_id from (
            select st2.trip_id as trip_id, t2.direction_id as d,
                   count(*) as n
            from trips t2
            inner join stop_times st2 on st2.trip_id = t2.trip_id
            where t2.route_id = :route_id
            group by st2.trip_id
        )
        group by d
        having n = max(n)
    )
    order by t.direction_id, st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"route_id": route_id}).fetchall()
    stops = {}
    for direction, name, _seq in rows:
        key = str(direction if direction is not None else 0)
        stops.setdefault(key, []).append(name)
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
      and o.stop_id = :origin_id
      and d.stop_id = :destination_id
      and o.stop_sequence < d.stop_sequence
      {direction_where}
    limit 1
    """
    with schedule.engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()
    _LOGGER.debug("Trip between %s and %s on %s (direction %s): %s",
                  origin_id, destination_id, route_id, direction, bool(row))
    return bool(row)


def has_train_trip_between(schedule, origin_name, destination_name, line=None):
    """Whether any rail trip serves both stations, in this order.

    The train path works with station names rather than stop ids, matched
    the way get_next_departure does: on the name's prefix, since a feed
    splits a station into several platform stops sharing it. Held to one
    line when the flow picked one, like the departures themselves.
    """
    line_where = "and r.route_short_name = :line" if line else ""
    sql = f"""
    SELECT 1
    from trips t
    inner join routes r on r.route_id = t.route_id
    inner join stop_times o on o.trip_id = t.trip_id
    inner join stops so on so.stop_id = o.stop_id
    inner join stop_times d on d.trip_id = t.trip_id
    inner join stops sd on sd.stop_id = d.stop_id
    where r.route_type in (2,100,101,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117)
      and so.stop_name like :origin_name
      and sd.stop_name like :destination_name
      and o.stop_sequence < d.stop_sequence
      {line_where}
    limit 1
    """
    with schedule.engine.connect() as conn:
        row = conn.execute(text(sql), {
            "origin_name": origin_name + "%",
            "destination_name": destination_name + "%",
            "line": line,
        }).fetchone()
    _LOGGER.debug("Train trip between %s and %s (line %s): %s",
                  origin_name, destination_name, line, bool(row))
    return bool(row)


def get_station_list(schedule, route_id=None):
    """List the distinct stop names, for feeds where stop ids are unusable.

    A station shows up in GTFS as several stops, one per platform or mode, so
    the ids cannot be offered as they are. The names repeat instead: on a
    regional rail feed, 925 stops come down to 379 names.
    """
    _LOGGER.debug("Getting station list for route: %s", route_id)
    where = ""
    if route_id:
        where = f"""
        where exists (
            select 1 from stop_times st
            inner join trips t on t.trip_id = st.trip_id
            where st.stop_id = s.stop_id and t.route_id = '{route_id}'
        )"""
    sql = f"""
    SELECT distinct s.stop_name
    from stops s
    {where}
    order by s.stop_name
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"q": "q"}).fetchall()
    stations = [r[0] for r in rows if r[0]]
    _LOGGER.debug("Stations returned: %s", len(stations))
    return stations


def get_stop_list(schedule, route_id, direction):
    _LOGGER.debug("Getting stops list for route: %s", route_id)
    sql_stops = f"""
    SELECT st.trip_id, s.stop_id, s.stop_name, st.stop_sequence, s.parent_station
    from trips t
    inner join stop_times st on st.trip_id = t.trip_id
    inner join stops s on s.stop_id = st.stop_id
    where  t.route_id = '{route_id}'
    and (t.direction_id = {direction} or t.direction_id is null)
    order by st.trip_id, st.stop_sequence
    """  # noqa: S608
    stops = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_stops), {"q": "q"}).fetchall()
    trips = {}
    names = {}
    stations = {}
    for trip_id, stop_id, stop_name, stop_sequence, parent_station in rows:
        trips.setdefault(trip_id, []).append((stop_id, stop_sequence))
        names[stop_id] = stop_name
        stations[stop_id] = parent_station
    # Trips of one direction do not all run the full length, and a partial
    # trip renumbers stop_sequence from 0, so sorting the union of all trips
    # by stop_sequence interleaves the start of the route with its middle
    # (TAO tram B: 110 short runs from mid-route). Walk the fullest trip
    # first instead, it is the route as the rider rides it, then slot each
    # stop it skips right after the stop preceding it on a trip that does
    # serve it.
    order = []
    kept_seq = {}
    for _trip_id, trip_stops in sorted(
        trips.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        prev = -1
        for stop_id, stop_sequence in trip_stops:
            if stop_id in kept_seq:
                prev = order.index(stop_id)
                continue
            prev += 1
            order.insert(prev, stop_id)
            kept_seq[stop_id] = stop_sequence
    kept = [[stop_id, names[stop_id], kept_seq[stop_id]] for stop_id in order]
    # A stop the feed writes once per platform is one place to the rider, who
    # cannot tell which platform a given run will use and should not have to:
    # offering both leaves two lines reading the same name, and picking either
    # one hides the runs that use the other. A station is therefore offered
    # once, by the first of its records the line calls at, and the departures
    # of the others are reached through it.
    # Only records the line calls at one after the other are folded together,
    # because that is what two platforms of one place look like along a ride.
    # Records of one station far apart in the list are not platforms of a
    # stop, they are the two sides of a line that passes twice, a loop or a
    # direction the feed fills with both ways round, and they stay apart:
    # the numbering below is what tells those apart.
    one_per_station = []
    previous_station = None
    for x in kept:
        station = stations.get(x[0])
        if station and station == previous_station:
            continue
        previous_station = station
        one_per_station.append(x)
    kept = one_per_station
    # A circular line calls at its terminus twice, under ids of its own: TAO
    # line 22 runs Zenith to Zenith and offers three stops all reading
    # "Zenith", which the user cannot tell apart. Number the repeats in the
    # order the line calls at them, so the choice is about the journey rather
    # than about a raw id. The value keeps the id untouched, only the readable
    # part changes.
    by_name = {}
    for x in kept:
        by_name.setdefault(x[1], []).append(x)
    rank = {}
    for name, group in by_name.items():
        if len(group) > 1:
            for n, x in enumerate(group, 1):
                rank[x[0]] = n
    for x in kept:
        shown = x[1]
        if x[0] in rank:
            shown = f"{shown} #{rank[x[0]]}"
        val = x[0] + ": " + shown + ' (' + str(x[2]) + ')'
        stops.append(val)
    _LOGGER.debug(f"Route stops: {stops}")
    return stops
    
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
        row = row_cursor._asdict()
        agencies_list.append(list(row_cursor))
    for x in agencies_list:
        val = str(x[0]) + ": " + str(x[1])
        agencies.append(val)
    _LOGGER.debug(f"agencies: {agencies}")
    return agencies

async def get_datasources(hass, path) -> dict[str]:
    _LOGGER.debug(f"Getting datasources for path: {path}")
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    files = await hass.async_add_executor_job(
            os.listdir, gtfs_dir)
    datasources = []
    for file in files:
        if file.endswith(".sqlite"):
            datasources.append(file.split(".")[0])        
    _LOGGER.debug(f"Datasources in folder: {datasources}")
    return datasources


async def get_zipfiles(hass, path) -> list[str]:
    """List the zip files sitting in the gtfs2 folder, without their extension.

    get_datasources lists datasources that were already extracted (.sqlite);
    this lists the archives still waiting to be extracted, so the user can pick
    one instead of typing its name.
    """
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    files = await hass.async_add_executor_job(os.listdir, gtfs_dir)
    zipfiles = sorted(
        f[:-4] for f in files
        if f.endswith(".zip") and not f.endswith("_temp.zip")
        and not f.endswith("_temp_out.zip")
    )
    _LOGGER.debug(f"Zip files in folder: {zipfiles}")
    return zipfiles


def remove_datasource(hass, path, filename, include_sqlite):
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


def check_extraction_result(gtfs_dir, filename):
    """Whether an extraction actually produced a usable datasource.

    check_extracting only says that nothing is writing any more, which a
    process killed halfway satisfies just as well as one that succeeded: both
    leave no journal behind. So finishing has to be told apart from working.

    pygtfs writes the _feed row last, once every table is loaded, which makes
    it the honest marker. The counts confirm the tables a journey needs are
    populated, since a feed with no routes or no stop_times cannot answer any
    query the integration makes.

    Returns (ok, detail). On success detail holds the row counts; on failure
    it is a reason code, so the notification can word it in the user's
    language rather than repeat an English sentence built here.
    """
    sqlite_file = os.path.join(gtfs_dir, filename + ".sqlite")
    if not os.path.exists(sqlite_file):
        return False, "no_database"
    try:
        conn = sqlite3.connect(sqlite_file, timeout=10)
    except sqlite3.Error as ex:
        _LOGGER.error("Cannot open %s: %s", sqlite_file, ex)
        return False, "cannot_open"
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "select name from sqlite_master where type in ('table', 'view')")}
        missing = {"_feed", "routes", "trips", "stops", "stop_times"} - tables
        if missing:
            _LOGGER.error("Missing tables in %s: %s", filename, sorted(missing))
            return False, "tables_missing"
        # the _feed row is written once everything else is in
        if not cur.execute("select count(*) from _feed").fetchone()[0]:
            return False, "unfinished"
        counts = {t: cur.execute(f"select count(*) from {t}").fetchone()[0]  # noqa: S608
                  for t in ("routes", "trips", "stops", "stop_times")}
        empty = [t for t, n in counts.items() if not n]
        if empty:
            _LOGGER.error("Empty tables in %s: %s", filename, empty)
            return False, "tables_empty"
    except sqlite3.Error as ex:
        _LOGGER.error("Cannot read %s: %s", sqlite_file, ex)
        return False, "unreadable"
    finally:
        conn.close()
    _LOGGER.debug("Extraction of %s looks complete: %s", filename, counts)
    # the counts themselves, so the notification can word them in the user's
    # language: "43 routes, 679988 stop_times" is table names, untranslatable
    # and of no use to whoever reads it
    return True, counts


async def async_watch_extraction(hass: HomeAssistant, filename: str):
    """Wait for an extraction to end, then say how it went.

    The unpacking runs in a detached process, which has its own copy of hass
    and no event loop: it cannot raise a notification itself, and anything it
    creates dies with it. So the watching is done here, and the only thing that
    crosses the boundary is the state of the files on disk.

    This exists because the config flow is not a reliable witness. Closing its
    window abandons the flow while the extraction carries on, leaving no way to
    learn that it finished, or whether it worked.
    """
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    # every source goes through the progress step now, including one already
    # unpacked. Nothing happened there, so there is nothing to announce.
    ok, _ = await hass.async_add_executor_job(
        check_extraction_result, gtfs_dir, filename)
    if ok and not await hass.async_add_executor_job(
        check_extracting, hass, DEFAULT_PATH, filename
    ):
        _LOGGER.debug("Nothing to watch for %s, it is already built", filename)
        return
    # the fork needs a moment before it creates the journal, so a datasource
    # that does not look busy yet is not necessarily done
    await asyncio.sleep(10)
    while await hass.async_add_executor_job(
        check_extracting, hass, DEFAULT_PATH, filename
    ):
        await asyncio.sleep(15)

    ok, detail = await hass.async_add_executor_job(
        check_extraction_result, gtfs_dir, filename)
    # Whoever reads this closed the progress window: they left before knowing
    # how it went, and have to pick the flow back up by hand. So say where to
    # go, not just what happened.
    if ok:
        _LOGGER.info("Extraction of %s finished: %s", filename, detail)
        await _async_notify(hass, "extract_ready", f"gtfs2_extract_{filename}",
                            file=filename, routes=detail.get("routes", 0),
                            stops=detail.get("stops", 0))
    else:
        _LOGGER.error("Extraction of %s failed: %s", filename, detail)
        # the reason is a key of its own, so the whole message is translated
        # rather than a translated frame around an English sentence
        reason = await _async_text(hass, f"reason_{detail}", detail)
        await _async_notify(hass, "extract_failed", f"gtfs2_extract_{filename}",
                            file=filename, detail=reason)


async def async_notify_import(hass, filename, routes, added):
    """Report how an import went, for a user who closed the progress window.

    The import runs in the executor and reaches its end whatever happens to the
    flow, but an abandoned flow means nobody is left to say so. Called from a
    background task, which outlives it.
    """
    if not added:
        _LOGGER.error("Import into %s failed for %s", filename, routes)
        await _async_notify(hass, "import_failed", f"gtfs2_import_{filename}",
                            file=filename)
        return
    lines = ", ".join(r.split(":")[-1] for r in added)
    _LOGGER.info("Import into %s added %s", filename, added)
    await _async_notify(hass, "import_done", f"gtfs2_import_{filename}",
                        file=filename, lines=lines)


async def async_notify_line_orphaned(hass, filename, line):
    """Say that a line's last sensor is gone while its timetable remains.

    Raised by the entry removal hook. Deliberately not a prune: the user may
    be reshuffling sensors and want the line right back, so the notification
    names what is now dead weight and the service that drops it, and the
    choice stays theirs.
    """
    _LOGGER.info("No sensor reads line %s of %s any more", line, filename)
    await _async_notify(hass, "line_orphaned", f"gtfs2_prune_{filename}",
                        file=filename, line=line)


async def _async_notify(hass, key, notification_id, **values):
    """Raise a notification in the user's language.

    A notification is read outside any config flow, so it cannot lean on the
    placeholders Home Assistant fills there: the strings are fetched and
    formatted here. They live under a "notification" section of strings.json,
    alongside the flow's own, so translating the integration covers them too.

    Falls back to the key itself when a translation is missing, which is
    visible without being fatal.
    """
    persistent_notification.async_create(
        hass,
        await _async_text(hass, key, key, **values),
        title=await _async_text(hass, f"{key}_title", "GTFS", **values),
        notification_id=notification_id)


async def _async_text(hass, name, default, **values):
    """One notification string, in the user's language, placeholders filled."""
    try:
        strings = await async_get_translations(
            hass, hass.config.language, "notification", {DOMAIN})
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not load notification strings: %s", ex)
        strings = {}
    raw = strings.get(f"component.{DOMAIN}.notification.{name}", default)
    try:
        return raw.format(**values)
    except (KeyError, IndexError):
        return raw


def check_datasource_index(hass, schedule, gtfs_dir, file):
    _LOGGER.debug("Check datasource index for file: %s", file)
    if check_extracting(hass, gtfs_dir,file):
        _LOGGER.warning("Cannot check indexes on this datasource as still unpacking: %s", file)
        return
    # runs before get_next_departure on every refresh, so it meets the same
    # sentinels get_gtfs leaves in place of a schedule
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Cannot check indexes: datasource %s has no usable schedule (%s)", file, schedule or "empty")
        return
    sql_index_1 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    (type= 'index' and tbl_name = 'stop_times' and name like '%trip_id%')
    -- an interned datasource exposes stop_times as a view: its indexes
    -- live on gtfs2_stop_times and must not be recreated here
    or (type = 'view' and name = 'stop_times');
    """
    sql_index_2 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    (type= 'index' and tbl_name = 'stop_times' and name like '%stop_id%')
    -- an interned datasource exposes stop_times as a view: its indexes
    -- live on gtfs2_stop_times and must not be recreated here
    or (type = 'view' and name = 'stop_times');
    """
    sql_index_3 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'shapes' and name like '%shape_id%';
    """
    sql_index_4 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'stops' and name like '%stop_name%';
    """
    sql_index_5 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'routes' and name like '%route_type%';
    """
    sql_index_6 = f"""
    SELECT count(*) as checkidx
    FROM sqlite_master
    WHERE
    type= 'index' and tbl_name = 'trips' and name like '%route_id%';
    """
    sql_add_index_1 = f"""
    create index gtfs2_stop_times_trip_id on stop_times(trip_id)
    """
    sql_add_index_2 = f"""
    create index gtfs2_stop_times_stop_id on stop_times(stop_id)
    """
    sql_add_index_3 = f"""
    create index gtfs2_shapes_shape_id on shapes(shape_id)
    """
    sql_add_index_4 = f"""
    create index gtfs2_stops_stop_name on stops(stop_name)
    """    
    sql_add_index_5 = f"""
    create index gtfs2_routes_route_type on routes(route_type)
    """
    sql_add_index_6 = f"""
    create index gtfs2_trips_route_id on trips(route_id)
    """
    sql_check_route_agency = f"""
    SELECT count(*) as check_agency
    FROM routes where agency_id='None'
    """
    sql_fix_route_agency = f"""
    update routes set agency_id = (select agency_id from agency limit 1)
        where agency_id='None'
    """
    
    with schedule.engine.connect() as conn:
        rows_1a = conn.execute(text(sql_index_1), {"q": "q"}).fetchall()
    for row_cursor in rows_1a:
        _LOGGER.debug("IDX result1: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 1 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_1), {"q": "q"})       
        
    with schedule.engine.connect() as conn:
        rows_2a = conn.execute(text(sql_index_2), {"q": "q"}).fetchall()
    for row_cursor in rows_2a:
        _LOGGER.debug("IDX result2: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 2 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_2), {"q": "q"})
                
    with schedule.engine.connect() as conn:
        rows_3a = conn.execute(text(sql_index_3), {"q": "q"}).fetchall()
    for row_cursor in rows_3a:
        _LOGGER.debug("IDX result3: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 3 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_3), {"q": "q"})
                
    with schedule.engine.connect() as conn:
        rows_4a = conn.execute(text(sql_index_4), {"q": "q"}).fetchall()
    for row_cursor in rows_4a:
        _LOGGER.debug("IDX result4: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 4 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_4), {"q": "q"})
                
    with schedule.engine.connect() as conn:
        rows_5a = conn.execute(text(sql_index_5), {"q": "q"}).fetchall()
    for row_cursor in rows_5a:
        _LOGGER.debug("IDX result5: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 5 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_5), {"q": "q"})

    with schedule.engine.connect() as conn:
        rows_6a = conn.execute(text(sql_index_6), {"q": "q"}).fetchall()
    for row_cursor in rows_6a:
        _LOGGER.debug("IDX result6: %s", row_cursor._asdict())
        if row_cursor._asdict()['checkidx'] == 0:
            _LOGGER.warning("Adding index 6 to improve performance")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_add_index_6), {"q": "q"})

    with schedule.engine.connect() as conn:
        rows_8a = conn.execute(text(sql_check_route_agency), {"q": "q"}).fetchall()
    for row_cursor in rows_8a:
        _LOGGER.debug("Agency 'None' in routes: %s", row_cursor._asdict())
        if row_cursor._asdict()['check_agency'] > 0:
            _LOGGER.warning("Fix missing agency_id in routes table")
            with schedule.engine.connect() as conn:
                conn.execute(text(sql_fix_route_agency), {"q": "q"})
                conn.commit()

    
            
def create_trip_geojson(self):
    # not in use, awaiting geojson in HA-core to cover this type of geometry
    _LOGGER.debug("Create geojson with data: %s", self._data)
    schedule = self._data["schedule"]
    self._trip_id = self._data["next_departure"]["trip_id"]
    sql_shape = f"""
    SELECT t.trip_id, s.shape_pt_lat, s.shape_pt_lon
    FROM trips t, shapes s
    WHERE
    t.shape_id = s.shape_id
    and t.trip_id = '{self._trip_id}'
    order by s.shape_pt_sequence
    """
    shapes_list = []
    coordinates = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_shape), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        shapes_list.append(list(row_cursor))
    for x in shapes_list:
        coordinate = []
        coordinate.append(x[2])
        coordinate.append(x[1])
        coordinates.append(coordinate)
    self.geojson = {"features": [{"geometry": {"coordinates": coordinates, "type": "LineString"}, "properties": {"id": self._trip_id, "title": self._trip_id}, "type": "Feature"}], "type": "FeatureCollection"}    
    _LOGGER.debug("Geojson output: %s", json.dumps(self.geojson))
    return None


def _fmt_gtfs_time(value):
    """Render a pygtfs departure_time (seconds since midnight, may exceed 24h) as HH:MM:SS."""
    try:
        s = int(value)
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    except (TypeError, ValueError):
        return str(value) if value is not None else None


def route_geojson_name(route_id, direction):
    """File name of the route export, in one place because three callers need
    the same answer: the writer, the sensor attribute and the removal on entry
    deletion. A file nobody can name again is a file nobody can delete."""
    return f"{safe_file_part(route_id)}_{safe_file_part(direction)}_route.json"


def vehicle_positions_name(route_id, direction):
    """Same, for the realtime positions file written by get_rt_vehicle_positions."""
    return f"{safe_file_part(route_id)}_{safe_file_part(direction)}.json"


def get_representative_trip(schedule, route_id, direction):
    """The fullest trip of a route and direction, to stand in for a real one.

    Used to draw a line that has no departure to point at: which trip is
    picked matters little for a shape, as long as it is not a short turn, so
    the one with a shape and the most stops wins. Deterministic on ties, so a
    restart does not silently swap the drawn path.
    """
    if not route_id:
        return None
    where = "t.route_id = :route_id"
    params = {"route_id": str(route_id)}
    # direction_id is optional in GTFS and gtfs2 stringifies a missing one
    if direction not in (None, "", "None"):
        where += " AND CAST(t.direction_id AS TEXT) = :direction"
        params["direction"] = str(direction)
    # ONE pass over stop_times, filtered by a subquery on trips, and never a
    # join. pygtfs creates no index on stop_times at all, so joining it to a
    # filtered trips set makes SQLite scan the whole table once per candidate
    # trip: measured on a mid-sized city feed (680k stop_times, 1818 trips on
    # the line) that was 17.3 SECONDS against 45 ms this way, for the same
    # answer. Preferring a trip that has a shape is a separate, indexed
    # question, asked first and dropped if it excludes everything.
    sql = """
    SELECT st.trip_id, COUNT(*) AS stops
    FROM stop_times st
    WHERE st.trip_id IN (SELECT t.trip_id FROM trips t WHERE {where}{shaped})
    GROUP BY st.trip_id
    ORDER BY stops DESC, st.trip_id
    LIMIT 1
    """
    try:
        with schedule.engine.connect() as conn:
            row = conn.execute(text(sql.format(where=where, shaped=" AND t.shape_id IS NOT NULL")), params).fetchone()
            if not row:
                row = conn.execute(text(sql.format(where=where, shaped="")), params).fetchone()
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not find a trip to draw route %s direction %s: %s", route_id, direction, ex)
        return None
    if not row:
        _LOGGER.debug("No trip at all for route %s direction %s", route_id, direction)
        return None
    _LOGGER.debug("Drawing route %s direction %s from trip: %s", route_id, direction, row[0])
    return row[0]


def update_route_geojson(self):
    """Write the journey's ordered stops to www/gtfs2/<route>_<direction>_route.json.

    Companion file to the vehicle-positions geojson. Points only: the geojson
    integration reads nothing else, and since the import strips shapes.txt a
    LineString could only duplicate the stops; a map card rebuilds the path by
    joining the points in stop_sequence order. Each point carries an id and a
    title the way the geojson integration expects, plus the trip_id; what
    describes the whole journey sits on the FeatureCollection.
    Rewritten only when the drawn trip changes (see coordinator).
    """
    schedule = self._data["schedule"]
    trip_id = (self._data.get("next_departure") or {}).get("trip_id", None)
    if not trip_id:
        # No departure left today is not the same as no line: a weekday route
        # read on a Sunday, or a seasonal one out of season, still has a path
        # worth drawing. Take the fullest trip of this route and direction.
        trip_id = get_representative_trip(schedule, self._route_id, self._direction)
    if not trip_id:
        return
    sql_stops = """
    SELECT st.stop_id, s.stop_name, s.stop_lat, s.stop_lon, st.stop_sequence, st.departure_time
    FROM stop_times st
    JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.trip_id = :trip_id
    ORDER BY st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        stop_rows = conn.execute(text(sql_stops), {"trip_id": trip_id}).fetchall()
    if not stop_rows:
        _LOGGER.debug("No stops found for trip: %s", trip_id)
        return
    features = []
    for row in stop_rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row[3], row[2]]},
            "properties": {
                "id": str(self._route_id) + "_" + str(self._direction) + "_" + str(row[4]),
                # the _stop suffix is what a customize_glob rule matches on to
                # give the stop entity a picture, see upstream c666cb7
                "title": row[1] + "_stop",
                "trip_id": trip_id,
                "stop_id": row[0],
                "stop_name": row[1],
                "stop_sequence": row[4],
                "departure_time": _fmt_gtfs_time(row[5]),
            },
        })
    geojson_dir = self.hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    # the ids come out of the datasource, so they are not file names until
    # they are made ones: see safe_file_part
    file = os.path.join(geojson_dir, route_geojson_name(self._route_id, self._direction))
    _LOGGER.debug("Creating route geojson file: %s", file)
    with open(file, "w") as outfile:
        json.dump({
            "type": "FeatureCollection",
            "properties": {
                "trip_id": trip_id,
                "route_id": str(self._route_id),
                "direction_id": str(self._direction),
            },
            "features": features,
        }, outfile)

def get_local_stop_list(hass, schedule, data):
    _LOGGER.debug("Getting local stops list with data: %s", data)
    device_tracker = hass.states.get(data['device_tracker_id'])
    latitude = device_tracker.attributes.get("latitude", None)
    longitude = device_tracker.attributes.get("longitude", None) 
    radius = data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
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
        

def _build_local_stop_element(self, row, base_date, date_label,
                              timezone_agency, timezone_stop, now_tz,
                              apply_now_filter, feed_entities=None):
    """Build one departure element incl. realtime, for a given service date.

    base_date / date_label: 'now_date' for today, 'tomorrow_date' for tomorrow.
    apply_now_filter: True for today (drop already-passed), False for tomorrow.
    feed_entities: already-fetched/parsed RT feed for this refresh cycle, if any
    (avoids re-fetching + re-parsing the same feed once per row/stop).
    Relies on self._icon being set by the caller for this row.
    Returns the element dict, or None if filtered out.
    """
    self._trip_id = row["trip_id"]
    self._direction = str(row["direction_id"])
    self._trip_short_name = row["trip_short_name"]
    self._route = row["route_id"]
    self._route_id = row["route_id"]
    self._stop_id = row["stop_id"]
    self._stop_sequence = row["stop_sequence"]
    #_LOGGER.debug("Row departure_time: %s", row["departure_time"])
    #_LOGGER.debug("Base_date / date_label: %s", base_date)

    # collect departure time from row, using agency timezone as basis, then transforming it to the stop-specific timezone (based on Amtrak)
    self._departure_datetime = datetime.datetime.strptime(
        base_date + " " + row["departure_time"], "%Y-%m-%d %H:%M:%S"
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
        depart_time_corrected_time = (dt_util.parse_datetime(f"{base_date} {self._departure_time}")).replace(tzinfo=timezone_stop)
    #_LOGGER.debug("Departure time corrected based on realtime-time: %s", depart_time_corrected_time)

    if delay_rt != "-" and delay_rt != 0:
        depart_time_corrected_delay = (dt_util.parse_datetime(f"{base_date} {self._departure_time}") + datetime.timedelta(seconds=delay_rt)).replace(tzinfo=timezone_stop)
    else:
        delay_rt = "-"
        depart_time_corrected_delay = dt_util.parse_datetime(f"{base_date} {self._departure_time}").replace(tzinfo=timezone_stop)
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
        "date": date_label,
        "stop_name": row["stop_name"],
        "stop_id": row["stop_id"],
        "route": row["route_short_name"],
        "route_long": row["route_long_name"],
        "headsign": row["trip_headsign"],
        "trip_id": row["trip_id"],
        "direction_id": row["direction_id"],
        "icon": self._icon,
    }


def get_local_stops_next_departures(self):
    # 20260803 Note: this procedure is not using an option to in/exclude 'tomorrow'
    _LOGGER.debug("Get local stop departure with data: %s", self._data)
    if check_extracting(self.hass, self._data['gtfs_dir'],self._data['file']):
        _LOGGER.warning("Cannot get next depurtures on this datasource as still unpacking: %s", self._data["file"])
        return {}
    """Get next departures from data."""
    schedule = self._data["schedule"]
    # same contract as get_next_departure: a sentinel or None instead of a
    # schedule means nothing to offer, not a traceback
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no local stops", self._data["file"], schedule or "empty")
        return {}
    offset = self._data["offset"]
    now = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset)
    now_hist_corrected = dt_util.now().replace(tzinfo=None) + datetime.timedelta(minutes=offset) - datetime.timedelta(minutes=DEFAULT_LOCAL_STOP_TIMERANGE)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    now_time_hist_corrected = now_hist_corrected.strftime(TIME_STR_FORMAT)
    tomorrow = now + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime(dt_util.DATE_STR_FORMAT)
    device_tracker = self.hass.states.get(self._data['device_tracker_id'])
    tomorrow_name = tomorrow.strftime("%A").lower()
    latitude = device_tracker.attributes.get("latitude", None)
    longitude = device_tracker.attributes.get("longitude", None)
    time_range = str('+' + str(self._data.get("timerange", DEFAULT_LOCAL_STOP_TIMERANGE)) + ' minute')
    time_range_history = str('-' + str(self._data.get("timerange_history", DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY)) + ' minute')
    radius = self._data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    if not latitude or not longitude:
        _LOGGER.error("No latitude and/or longitude for : %s", self._data['device_tracker_id'])
        return []

    sql_query = f"""
        SELECT * FROM (
        SELECT stop.stop_id, stop.stop_name,stop.stop_lat as latitude, stop.stop_lon as longitude, stop.stop_timezone as stop_timezone, agency.agency_timezone as agency_timezone, trip.trip_id, trip.trip_headsign, trip.direction_id, trip.trip_short_name, time(st.departure_time) as departure_time,st.stop_sequence as stop_sequence,
               route.route_long_name,route.route_short_name,route.route_type,
               calendar.{now.strftime("%A").lower()} AS today,
               calendar.{tomorrow_name} AS tomorrow,
               calendar.start_date AS start_date,
               calendar.end_date AS end_date,
               date(:now_offset) as calendar_date,
               0 as today_cd, 
               route.route_id
        FROM trips trip
        INNER JOIN calendar calendar
                   ON trip.service_id = calendar.service_id
        INNER JOIN stop_times st
                   ON trip.trip_id = st.trip_id
        INNER JOIN stops stop
                   on stop.stop_id = st.stop_id and abs(stop.stop_lat - :latitude) < :radius and abs(stop.stop_lon - :longitude) < :radius
        INNER JOIN routes route
                   ON route.route_id = trip.route_id 
        INNER JOIN agency agency
                   ON route.agency_id = agency.agency_id
        WHERE
        (
            (
                calendar.{now.strftime("%A").lower()} = 1
                AND trip.service_id NOT IN (
                    SELECT service_id
                    FROM calendar_dates
                    WHERE date = date(:now_offset)
                      AND exception_type = 2
                )
                AND datetime(
                    date(:now_offset) || ' ' || time(st.departure_time)
                ) BETWEEN
                    datetime(:now_offset, :timerange_history)
                    AND datetime(:now_offset, :timerange)
            )
            OR
            (
                calendar.{tomorrow_name} = 1
                AND trip.service_id NOT IN (
                    SELECT service_id
                    FROM calendar_dates
                    WHERE date = date(:now_offset, '+1 day')
                      AND exception_type = 2
                )
                AND datetime(
                    date(:now_offset,'+1 day') || ' ' || time(st.departure_time)
                ) BETWEEN
                    datetime(:now_offset,:timerange_history)
                    AND datetime(:now_offset,:timerange)
            )
        )
        AND calendar.start_date <= date(:now_offset)
        AND calendar.end_date >= date(:now_offset)
        )
		UNION ALL
        SELECT * FROM (
	    SELECT stop.stop_id, stop.stop_name,stop.stop_lat as latitude, stop.stop_lon as longitude, stop.stop_timezone as stop_timezone, agency.agency_timezone as agency_timezone, trip.trip_id, trip.trip_headsign, trip.direction_id,trip.trip_short_name, time(st.departure_time) as departure_time,st.stop_sequence as stop_sequence,
               route.route_long_name,route.route_short_name,route.route_type,
               0 AS today,
               CASE WHEN date(:now_offset) < calendar_date_today.date THEN 1 else 0 END as tomorrow,
               date(:now_offset) AS start_date,
               date(:now_offset) AS end_date,
               calendar_date_today.date as calendar_date,
               calendar_date_today.exception_type as today_cd,
               route.route_id
        FROM trips trip
        INNER JOIN stop_times st
                   ON trip.trip_id = st.trip_id
        INNER JOIN stops stop
                   on stop.stop_id = st.stop_id and abs(stop.stop_lat - :latitude) < :radius and abs(stop.stop_lon - :longitude) < :radius
        INNER JOIN routes route
                   ON route.route_id = trip.route_id 
        INNER JOIN calendar_dates calendar_date_today
				   ON trip.service_id = calendar_date_today.service_id
        INNER JOIN agency agency
                   ON route.agency_id = agency.agency_id
                 
		WHERE 
        today_cd = 1
        AND 
        (
            (
                calendar_date_today.date = date(:now_offset)
                AND datetime(
                    date(:now_offset) || ' ' || time(st.departure_time)
                ) BETWEEN
                    datetime(:now_offset, :timerange_history)
                    AND datetime(:now_offset, :timerange)
            )
            OR
            (
                calendar_date_today.date = date(:now_offset, '+1 day')
                AND datetime(
                    date(:now_offset, '+1 day') || ' ' || time(st.departure_time)
                ) BETWEEN
                    datetime(:now_offset, :timerange_history)
                    AND datetime(:now_offset, :timerange)
            )
        )                         
        )
        order by stop_id, calendar_date asc, departure_time asc;
        """  # noqa: S608
    query_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timerange": time_range,
        "timerange_history": time_range_history,
        "radius": radius,
        "now_offset": now,
    }

    #_LOGGER.debug("SQL statement:\n%s", sql_query)
    #_LOGGER.debug("SQL parameters:\n%s", query_params)        

    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_query), {"latitude": latitude, "longitude": longitude, "timerange": time_range, "timerange_history": time_range_history, "radius": radius, "now_offset": now}).fetchall()

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
        self._rt_data = {
            "url": self._trip_update_url,
            CONF_API_KEY : self._headers.get(CONF_API_KEY,None),
            CONF_API_KEY_NAME : self._headers.get(CONF_API_KEY_NAME, None),
            CONF_API_KEY_LOCATION : self._headers.get(CONF_API_KEY_LOCATION,None),
            CONF_ACCEPT_HEADER_PB :self._headers.get(CONF_ACCEPT_HEADER_PB,None),
            "file": self._data["name"] + "_localstop",
            }
        _LOGGER.debug("self rt_data: %s, self headers: %s, self data: %s", self._rt_data, self._headers, self._data)

        check = get_gtfs_rt(self.hass,DEFAULT_PATH_RT,self._rt_data)

        # check if local file created
        if check != "ok":
            _LOGGER.error("Could not download RT data from: %s", self._trip_update_url)
            return {}
        else:
            # use local file created as new url
            self._trip_update_url = "file://" + DEFAULT_PATH_RT + "/" + self._data["name"] + "_localstop.rt"

    # Fetch + parse the RT feed once for this refresh cycle. Previously this
    # happened inside get_rt_route_trip_statuses on every row/stop match,
    # which re-fetched and re-parsed the same feed once per trip - expensive
    # when a stop has many routes/trips. The feed itself doesn't change
    # between rows within a single refresh, only which row is being matched
    # against it, so fetching it once and passing it into each match call is
    # equivalent and avoids the redundant work.
    feed_entities = None
    if self._realtime:

        feed_entities = get_gtfs_feed_entities(
            url=self._trip_update_url, headers=self._headers, label="trip_data"
        ) or []

    for row_cursor in rows:
        row = row_cursor._asdict()
        #_LOGGER.debug("Row from query: %s", row)

        #defining TZ for row
        #_LOGGER.debug("Configured Agency timezone: %s", row['agency_timezone'])
        #_LOGGER.debug("Configured Stop timezone: %s", row['stop_timezone'])
        _LOGGER.debug("Now hist corrected: %s", now_hist_corrected)
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

        if row["today"] == 1 or (row["today_cd"] == 1 and row["start_date"] == row["calendar_date"]):
            if row["today"] == 1:
                _LOGGER.debug("Adding row from calendar for today=1")
            if row["today_cd"] == 1 and row["start_date"] == row["calendar_date"]:
                _LOGGER.debug("Adding row from calendar_dates for today_cd=1 and start_date = calendar_date")
            #_t_elem_start = time.monotonic()
            element = _build_local_stop_element(
                self, row, now_date, now_date, timezone_agency, timezone_stop, now_tz,
                apply_now_filter=True, feed_entities=feed_entities)
            if element is not None:					  
                if element not in timetable:
                    timetable.append(element)
                _LOGGER.debug("Timetable: %s", timetable)

        if (row["tomorrow"] == 1 and datetime.datetime.strptime(now_time_hist_corrected,"%H:%M") > datetime.datetime.strptime(row["departure_time"],"%H:%M:%S")):
            _LOGGER.debug("Tomorrow: adding row for tomorrow_date: %s", tomorrow_date)

            element = _build_local_stop_element(
                self, row, tomorrow_date, tomorrow_date, timezone_agency, timezone_stop, now_tz,
                apply_now_filter=False, feed_entities=feed_entities)

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
    _LOGGER.debug("Stop data returned: %s", data_returned)
    return data_returned
	   
async def update_gtfs_local_stops(hass, data): 
    _LOGGER.debug("Update service for local stops with data: %s", data)
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("device_tracker_id") == data["entity_id"] :
            entries.append(entry.entry_id)
    for cf_entry in entries:
        _LOGGER.debug("Reloading local stops for config_entry_id: %s", cf_entry) 
        reload = await hass.config_entries.async_reload(cf_entry)    
    return
    
async def get_route_departures(hass, data):
    _LOGGER.debug("Getting route departures with data: %s", data)
    config_entry = hass.config_entries.async_get_entry(data.get("config_entry",""))
    cf_data = config_entry.data
    cf_options = config_entry.options
    _LOGGER.debug("config entry data: %s, options: %s", cf_data, cf_options)
    
    now = dt_util.now().replace(tzinfo=None)
    now_date = now.strftime(dt_util.DATE_STR_FORMAT)
    cutoff_today = datetime.datetime.strptime(now_date + ' ' + data.get('from_time','00:00:00'), "%Y-%m-%d %H:%M:%S")
    tomorrow = now + datetime.timedelta(days=1)
    tomorrow_date = tomorrow.strftime(dt_util.DATE_STR_FORMAT)
    cutoff_tomorrow = datetime.datetime.strptime(tomorrow_date + ' ' + data.get('from_time','00:00:00'), "%Y-%m-%d %H:%M:%S")
    _LOGGER.debug("Cutoff today: %s, cutoff tomorrow: %s", cutoff_today, cutoff_tomorrow)

    _pygtfs = get_gtfs(
            hass, DEFAULT_PATH, cf_data, False
        ) 
    
    _data = {
            "schedule": _pygtfs,
            "origin": cf_data["origin"],
            "destination": cf_data["destination"],
            "offset": cf_options["offset"] if "offset" in cf_options else 0,
            "include_tomorrow": True,
            "gtfs_dir": DEFAULT_PATH,
            "name": cf_data["name"],
            "file": cf_data["file"],
            "route_type": cf_data["route_type"],
            "route": cf_data["route"],
            "extracting": False,
            "next_departure": {},
            "next_departure_realtime_attr": {},
            "alert": {}
        }
        
    departures = await hass.async_add_executor_job(
                    get_next_departure, hass, _data
                ) 
                
    _LOGGER.debug("Departures received: %s", departures["next_departures"])

    today_departures = []
    tomorrow_departures = []
    for dt_string in departures["next_departures"]:
        dt = datetime.datetime.fromisoformat(dt_string).replace(tzinfo=None)
        dt_date = dt.strftime(dt_util.DATE_STR_FORMAT)
        if dt_date == now_date and cutoff_today < dt:
            today_departures.append(dt_string)
        if dt_date == tomorrow_date and cutoff_tomorrow < dt:
            tomorrow_departures.append(dt_string)
     
    _departures = {
        "today": today_departures if len(today_departures) > 0 else [],
        "tomorrow": tomorrow_departures if len(tomorrow_departures) > 0 else []
    } 
     
    _LOGGER.debug("Departures returned: %s", _departures)   
    _pygtfs.engine.dispose()
    return _departures
    
async def get_trip_stops(hass, data):
    _LOGGER.debug("Getting stoptimes for trip with: %s", data)
    state = hass.states.get(data.get("entity_id",""))
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get(data.get("entity_id",""))
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
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
        trips = state.attributes.get("next_departures_trips", "[]")
        origin_station_ids.append(state.attributes.get("origin_station_stop_id", ""))
        origin_station_names.append(state.attributes.get("origin_station_stop_name", ""))
    
    trip_list = str(trips).replace("[","(").replace("]",")")

    schedule = get_gtfs(
            hass, DEFAULT_PATH, cf_data, False
        ) 
       
    sql_stops = f"""
    SELECT st.trip_id, s.stop_name, time(st.departure_time), s.stop_id
    from stop_times st 
    inner join stops s on s.stop_id = st.stop_id
    where  st.trip_id in {trip_list}
    order by st.trip_id, st.departure_time, st.stop_sequence
    """  # noqa: S608
    stops_list = []
    stops = []
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_stops), {"q": "q"}).fetchall()
    for row_cursor in rows:
        row = row_cursor._asdict()
        stops_list.append(list(row_cursor))
    for x in stops_list:
        val = x[0] + ": " + x[1] + ' - ' + str(x[2]) + ' (' + str(x[3]) + ')'
        stops.append(val)

    stopslist = {}
    for trip in trips:
        s = []
        stop_hit = 0
        for tripstop in stops:
            for origin_station_id in origin_station_ids:
                if origin_station_id in tripstop and trip in tripstop:
                    stop_hit = 1
                if trip in tripstop and stop_hit == 1:
                    if tripstop.split(": ")[1] not in s:
                        s.append(tripstop.split(": ")[1].split(" (")[0])
                stopslist[trip] = s
    
    _tripstops = {
        "entity": data.get("entity_id","entity-not-found"),
        "origin_station_id": origin_station_ids[0],
        "origin_station_name": origin_station_names[0],
        "trip_stops": stopslist,
    }
    
    _LOGGER.debug("Tripstops returned: %s", _tripstops)
    schedule.engine.dispose()
    return _tripstops
