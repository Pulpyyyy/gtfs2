"""The services that read a line's timetable on demand: the departures of a
route from a stop (get_route_departures), its arrivals (get_route_arrivals)
and the calls of one trip (get_trip_stops), each answered to the
automation that asked. Registered by __init__.setup.
"""
from __future__ import annotations

from collections.abc import Mapping
import datetime
import json
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.sql import text
import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DEFAULT_PATH, id_of
from .feed_window import last_service_day
from .gtfs_db import close_schedule, feed_zip
from .clocks import _row_instant, zone_of
from .datasource import get_gtfs
from .gtfs_helper import (_fetch_departure_rows, departure_query_args,
                          get_next_service_date, journey_data)
from .timetable import TIMETABLE_ROWS_MAX

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


def _route_departures_between(data: Mapping[str, Any], first: str, last: str, limit: int = TIMETABLE_ROWS_MAX,
                              at: str = "origin_depart_dt") -> list[datetime.datetime]:
    """Every departure of an entry over two service days, as UTC instants.

    The window read the timetable export makes, rather than the sensor's
    next ten: a busy line has more than ten departures left today, and
    the ten the sensor lists all fell on today, so "tomorrow" came back
    empty. Each row is laid in its network's zone before it becomes an
    instant, and a trip reached from two quays of one stop is listed once.

    at is the time of the ride read: origin_depart_dt, its departure from
    the origin, or dest_arrival_dt, its arrival at the destination (the
    arrivals service), laid in the destination's zone when the agency
    names none, in the origin's when the destination names none either.
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
        # an arrival with no zone of its end is read in the origin's, as
        # the sensor reads it (_departure_zones); Home Assistant's else
        zone = (zone_of(row.get("agency_timezone"), row.get(stop_zone), row.get("origin_stop_timezone"))
                or dt_util.DEFAULT_TIME_ZONE)
        try:
            instants.append(dt_util.as_utc(_row_instant(row[at], zone)))
        except ValueError:
            continue
    return sorted(instants)


def _route_departure_from(data: Mapping[str, Any], first_day: str,
                          at: str = "origin_depart_dt") -> datetime.datetime | None:
    """The entry's first departure on the service day first_day or after,
    as a UTC instant, or None when the calendar has none in its horizon;
    with at="dest_arrival_dt", that ride's arrival."""
    day = get_next_service_date(
        data["schedule"], id_of(data["origin"]), id_of(data["destination"]),
        first_day, data["route_type"], **departure_query_args(data))
    if not day:
        return None
    # the rows come in time order: the first is the one
    instants = _route_departures_between(data, day, day, limit=1, at=at)
    return instants[0] if instants else None


async def get_route_departures(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    """The entry's departures today and tomorrow, from from_time on, and
    what lies past them (_route_times)."""
    return await _route_times(hass, data, "origin_depart_dt")


async def get_route_arrivals(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    """The arrivals at the destination of the entry's rides still to leave,
    today and tomorrow, from from_time on, and what lies past them: the
    departures service read at the other end of the ride. Every arrival
    of the two days, not the sensor's next ten nor a hundred."""
    return await _route_times(hass, data, "dest_arrival_dt")


async def _route_times(hass: HomeAssistant, data: Mapping[str, Any], at: str) -> dict[str, Any]:
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
    empty: dict[str, Any] = {"today": [], "tomorrow": [], "next": None, "until": None}
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
        get_gtfs, hass, DEFAULT_PATH, cf_data
    )
    if _pygtfs is None or isinstance(_pygtfs, str):
        # a sentinel: no zip, no database, or a feed all in the future
        _LOGGER.warning("Datasource %s has no usable schedule (%s), no departures",
                        cf_data.get("file"), _pygtfs or "empty")
        return empty

    # what the sensor of this entry is asked with, line and ends included,
    # so the service answers for the same journey
    _data = journey_data(_pygtfs, cf_data, cf_options)
    day_after = (now + datetime.timedelta(days=2)).strftime(dt_util.DATE_STR_FORMAT)
    try:
        # one service day more than the two listed: the query costs about
        # the same (TAO tram A, 1.8 s for a day or three), and the next
        # departure is usually in it, a run of tomorrow's service after
        # midnight or the day after's first
        instants = await hass.async_add_executor_job(
            _route_departures_between, _data, yesterday_date, day_after, TIMETABLE_ROWS_MAX, at)
        later = [i for i in instants
                 if dt_util.as_local(i).strftime(dt_util.DATE_STR_FORMAT) > tomorrow_date]
        next_instant = later[0] if later else await hass.async_add_executor_job(
            _route_departure_from, _data,
            (now + datetime.timedelta(days=3)).strftime(dt_util.DATE_STR_FORMAT), at)
        until = await hass.async_add_executor_job(
            last_service_day, feed_zip(hass.config.path(DEFAULT_PATH), cf_data["file"]))
    finally:
        # released whatever happens: this schedule was opened for the call
        close_schedule(_pygtfs)

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
    
def _trip_stops(schedule: Schedule, trips: list[str],
                origin_ids: list[str]) -> dict[str, list[str]]:
    """The stops each trip calls at from the origin on, "name - HH:MM:SS".

    Read in stop_sequence order, one bound parameter per trip. The origin
    is found by its stop_id, compared whole: the list was a text search
    through "trip: name - time (stop_id)" lines, where an id inside
    another one, or a name holding ": ", threw the match off.
    """
    if not trips:
        return {}
    sql_stops = """
    SELECT st.trip_id, s.stop_name, time(st.departure_time), s.stop_id
    from stop_times st
    inner join stops s on s.stop_id = st.stop_id
    where st.trip_id in (select value from json_each(:trips))
    order by st.trip_id, st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql_stops), {"trips": json.dumps(trips)}).fetchall()
    calls: dict[str, list[tuple[str, str]]] = {}
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


async def get_trip_stops(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, Any]:
    _LOGGER.debug("Getting stoptimes for trip with: %s", data)
    entity_id = data.get("entity_id", "")
    state = hass.states.get(entity_id)
    entry = er.async_get(hass).async_get(entity_id)
    config_entry = hass.config_entries.async_get_entry(entry.config_entry_id) if entry else None
    nothing: dict[str, Any] = {"entity": entity_id or "entity-not-found", "origin_station_id": "",
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
        # each listed trip leaves from its own record of the place, a
        # terminus's quays in turn: the first departure's alone left the
        # others with no stop at all
        for stop_id in state.attributes.get("next_departures_origin_stop_id") or []:
            if stop_id not in origin_station_ids:
                origin_station_ids.append(stop_id)

    schedule = await hass.async_add_executor_job(
        get_gtfs, hass, DEFAULT_PATH, cf_data
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
        close_schedule(schedule)

    _tripstops = {
        "entity": entity_id or "entity-not-found",
        "origin_station_id": origin_station_ids[0] if origin_station_ids else "",
        "origin_station_name": origin_station_names[0] if origin_station_names else "",
        "trip_stops": stopslist,
    }

    _LOGGER.debug("Tripstops returned: %s", _tripstops)
    return _tripstops
