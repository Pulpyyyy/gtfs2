"""The stops around a person: the departures of every line near where they
are, timetable and realtime (get_local_stops_next_departures), read by the
local stops coordinator, and the service that refreshes them on demand
(update_gtfs_local_stops).
"""
from __future__ import annotations

from collections.abc import Mapping
import datetime
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .const import (
    DEFAULT_LOCAL_STOP_RADIUS,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY,
    DOMAIN,
    ICON,
    ICONS,
    TIME_STR_FORMAT,
)
from .clocks import _day_offset, _on_service_day, _removed_on, _runs_on, zone_of
from .datasource import check_extracting
from .gtfs_helper import _feed_now, _row_instant
from .gtfs_rt_helper import delay_of, get_rt_route_trip_statuses, struck_trips
from .rt_feed import FeedEntities, get_gtfs_feed_entities, on_service_day
from .stop_rules import _boards

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

    from .coordinator import GTFSLocalStopUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def _tracker_position(hass: HomeAssistant, entity_id: str) -> tuple[float | None, float | None]:
    """Where a person or zone is, (latitude, longitude), or (None, None).

    The entity may be gone, renamed or not loaded yet at start: its state
    is then None, and reading its attributes raised on every refresh and
    in the options screen alike.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None, None
    return state.attributes.get("latitude", None), state.attributes.get("longitude", None)


def get_local_stop_list(hass: HomeAssistant, schedule: Schedule, data: Mapping[str, Any]) -> int:
    _LOGGER.debug("Getting local stops list with data: %s", data)
    latitude, longitude = _tracker_position(hass, data['device_tracker_id'])
    if not latitude or not longitude:
        # nowhere to look around: no stop is near
        return 0
    radius= data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    sql_query = """
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


def local_stops_nearby(hass: HomeAssistant, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The stops a local stops entry follows: every stop within its radius
    a trip calls at, whether or not a departure falls in the window now.
    The sensors were made from the stops with a departure in the window:
    an entry made in the evening, after the last bus, got none."""
    schedule = data.get("schedule")
    if schedule is None or isinstance(schedule, str):
        return []
    latitude, longitude = _tracker_position(hass, data["device_tracker_id"])
    if not latitude or not longitude:
        return []
    # the stops within the radius first, then their calls, as the
    # departures are read (_fetch_local_stop_rows)
    sql_query = """
        WITH nearby AS MATERIALIZED (
            SELECT stop_id, stop_name FROM stops
            WHERE abs(stop_lat - :latitude) < :radius AND abs(stop_lon - :longitude) < :radius
        )
        SELECT DISTINCT nearby.stop_id, nearby.stop_name
        FROM nearby
        CROSS JOIN stop_times st ON st.stop_id = nearby.stop_id
        ORDER BY nearby.stop_id
        """
    radius = data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(text(sql_query), {"latitude": latitude, "longitude": longitude,
                                                  "radius": radius}).fetchall()
    except SQLAlchemyError as ex:
        # the stops with a departure now are still there to make sensors of
        _LOGGER.warning("Could not read the stops around %s: %s", data["device_tracker_id"], ex)
        return []
    return [{"stop_id": row[0], "stop_name": row[1]} for row in rows]


def local_departure_leaves(scheduled: datetime.datetime, realtime: datetime.datetime | str,
                           delay: int | str) -> datetime.datetime:
    """When a local stop departure leaves: the time the realtime feed gives
    (realtime, else the timetable's), and never before the timetable's
    plus the delay the feed announces. delay is seconds, or "-" when the
    feed gives none."""
    leaves = realtime if isinstance(realtime, datetime.datetime) else scheduled
    if isinstance(delay, int) and delay:
        leaves = max(leaves, scheduled + datetime.timedelta(seconds=delay))
    return leaves


def drop_gone_local_departures(stops: list[dict[str, Any]], now: datetime.datetime) -> list[dict[str, Any]]:
    """The local stops list without the departures gone by now (an aware
    datetime, the entry's offset included), by the rule the reading
    itself applies (local_departure_leaves); the list as it is when none
    has gone.

    The list is read again at the entry's own pace, 15 minutes by default,
    and a departure gone stayed on it until then: the coordinator takes
    them out each minute in between, without reading anything.
    """
    kept: list[dict[str, Any]]
    kept, gone = [], False
    for stop in stops:
        left = [d for d in stop["departure"]
                if local_departure_leaves(d["departure_datetime"], d["departure_realtime_datetime"],
                                          d["delay_realtime"]) > now]
        gone = gone or len(left) != len(stop["departure"])
        kept.append({**stop, "departure": left})
    return kept if gone else stops


def _build_local_stop_element(self: GTFSLocalStopUpdateCoordinator, row: Mapping[str, Any], base_datetime: str,
                              timezone_agency: datetime.tzinfo | None, timezone_stop: datetime.tzinfo | None,
                              now_tz: datetime.datetime, apply_now_filter: bool,
                              feed_entities: FeedEntities | None = None) -> dict[str, Any] | None:
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
    scheduled = _row_instant(base_datetime, timezone_agency)
    self._departure_datetime = scheduled.astimezone(tz=timezone_stop)
    self._departure_datetime_utc = dt_util.as_utc(self._departure_datetime)
    #_LOGGER.debug("Self._departure datetime in agency_tz: %s", self._departure_datetime)
    self._departure_time = self._departure_datetime.replace(tzinfo=None).strftime(TIME_STR_FORMAT)
    #_LOGGER.debug("Self._departure time in stop tz: %s", self._departure_time)

    departure_rt: datetime.datetime | str = "-"
    departure_rt_datetime: datetime.datetime | str = "-"
    # seconds, or "-" when the feed gives none
    delay_rt: Any = "-"
    delay_rt_derived = "-"
    departures: list[datetime.datetime] = []

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
            svc = next_service.get(self._route, {}).get(self._direction, {}).get(self._stop_id, {})
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
        # base_datetime is the agency's wall clock, as the departure above
        # reads it: labelled with the stop's zone, a stop west of the agency
        # (Amtrak, Los Angeles against New York) kept departures already gone
        depart_time_corrected_time = scheduled
    #_LOGGER.debug("Departure time corrected based on realtime-time: %s", depart_time_corrected_time)

    if departure_rt != "-":
        # the same rule as the line sensors' (delay_of)
        delay_rt = delay_of(0 if delay_rt == "-" else delay_rt,
                            int(depart_time_corrected_time.timestamp()),
                            int(self._departure_datetime.timestamp()))
    if delay_rt == 0:
        delay_rt = "-"
    depart_time_corrected = local_departure_leaves(
        scheduled, depart_time_corrected_time, delay_rt)
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
        "date": scheduled.date().isoformat(),
        "stop_name": row["stop_name"],
        "stop_id": row["stop_id"],
        # a line named by its long name only (TriMet's MAX), a destination
        # given on each call rather than on the trip (TriMet): the card
        # showed neither the line nor where it goes
        "route": row["route_short_name"] or row["route_long_name"],
        "route_long": row["route_long_name"],
        "headsign": row["trip_headsign"] or row["stop_headsign"],
        "trip_id": row["trip_id"],
        "direction_id": row["direction_id"],
        "icon": self._icon,
    }                

def _fetch_local_stop_rows(schedule: Schedule, latitude: float, longitude: float, radius: float,
                            time_range: str, time_range_history: str,
                            now: datetime.datetime) -> list[dict[str, Any]]:
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
                   st.stop_sequence AS stop_sequence, st.stop_headsign AS stop_headsign,
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
                SELECT coalesce(max({_day_offset("departure_time_raw")}), 0)
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
              AND {_runs_on("cd.date", "cal")}
              AND NOT {_removed_on("cal.service_id", "cd.date")}
            UNION
            SELECT cd2.service_id, cd2.date
            FROM calendar_dates cd2
            INNER JOIN candidate_dates cd ON cd.date = cd2.date
            WHERE cd2.service_id IN (SELECT service_id FROM candidate_stops)
              AND cd2.exception_type = 1
          )
        SELECT cs.stop_id, cs.stop_name, cs.latitude, cs.longitude, cs.stop_timezone, cs.agency_timezone,
               cs.trip_id, cs.trip_headsign, cs.stop_headsign, cs.direction_id, cs.trip_short_name,
               {_on_service_day("vd.date", "cs.departure_time_raw")} AS departure_dt,
               cs.stop_sequence, cs.route_long_name, cs.route_short_name, cs.route_type,
               cs.route_id
        FROM candidate_stops cs
        INNER JOIN valid_dates vd ON vd.service_id = cs.service_id
        WHERE {_on_service_day("vd.date", "cs.departure_time_raw")} BETWEEN datetime(:now_offset, :timerange_history) AND datetime(:now_offset, :timerange)
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


def _local_stop_feed(self: GTFSLocalStopUpdateCoordinator) -> FeedEntities | None:
    """The trip updates a local stops refresh lays on its departures, read
    once for all of them from the source's feed, the download the source's
    other sensors share: None without realtime, an empty list when the
    feed could not be read."""
    if not self._realtime or not self._trip_update_url:
        return None
    self._rt_group = "trip"
    feed_entities = get_gtfs_feed_entities(
        url=self._trip_update_url, headers=self._headers, label="trip_data",
        owner=self._data["file"])
    if feed_entities is None:
        # the timetable still stands: the departures are listed without
        # their delays this cycle, where they all went with the feed
        _LOGGER.warning("Could not download RT data from %s, listing the "
                        "timetable alone", self._trip_update_url)
        return []
    return feed_entities


def _feed_by_trip(feed_entities: FeedEntities | None) -> dict[tuple[str, str], list[int]]:
    """{("trip", trip_id) or ("id", entity id): [positions]} of the trip
    updates of a feed: the two ways a local stop departure matches one,
    by trip (_follows_trip in trip mode)."""
    index: dict[tuple[str, str], list[int]] = {}
    for position, entity in enumerate(feed_entities or ()):
        if not entity.get("trip_update", False):
            continue
        index.setdefault(("trip", entity["trip_update"]["trip"].get("trip_id") or ""), []).append(position)
        index.setdefault(("id", entity.get("id") or ""), []).append(position)
    return index


def _trip_entities(self: GTFSLocalStopUpdateCoordinator, feed_entities: FeedEntities | None,
                   index: dict[tuple[str, str], list[int]], row: Mapping[str, Any]) -> FeedEntities | None:
    """The trip updates a local stop departure can take its realtime from,
    in feed order: those naming its trip, or whose id is its trip's short
    name. Handed the whole feed, every departure walked all of it for its
    own trip: 300 departures on a 20000 trip feed took 14 s a refresh."""
    if feed_entities is None or getattr(self, "_trip_list", None):
        return feed_entities
    positions = set(index.get(("trip", row["trip_id"]), ()))
    positions.update(index.get(("id", row["trip_short_name"]), ()))
    return [feed_entities[position] for position in sorted(positions)]


def _local_row_zones(row: Mapping[str, Any], timezone_local: datetime.tzinfo | None
                     ) -> tuple[datetime.tzinfo | None, datetime.tzinfo | None]:
    """(agency zone, stop zone) a local stop row is read in: the agency's,
    else the stop's, for the first; the stop's for the second; Home
    Assistant's when the feed names none."""
    return (zone_of(row["agency_timezone"], row["stop_timezone"]) or timezone_local,
            zone_of(row["stop_timezone"]) or timezone_local)


def _interpret_local_stop_rows(self: GTFSLocalStopUpdateCoordinator,
                               rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn raw SQL-shaped rows into the local-stops departures list.

    No database: `rows` only needs to be a list of plain dicts, ordered
    by stop as the query orders them.
    """
    offset = self._data["offset"]
    if self.hass.config.time_zone is None:
        _LOGGER.error("Timezone is not set in Home Assistant configuration, using UTC")
    timezone_local = dt_util.get_time_zone(self.hass.config.time_zone or "UTC")
    _LOGGER.debug("Local timezone: %s",timezone_local)
    now_tz = dt_util.now().replace(tzinfo=timezone_local) + datetime.timedelta(minutes=offset)
    _LOGGER.debug("Default 'now' on local timezone, incl. offset (if configured): %s",now_tz)

    feed_entities = _local_stop_feed(self)
    feed_index = _feed_by_trip(feed_entities)

    # {stop_id: its entry}, the entry read from the stop's last row
    stops: dict[str, dict[str, Any]] = {}
    for row in rows:
        timezone_agency, timezone_stop = _local_row_zones(row, timezone_local)
        _LOGGER.debug("Using Agency timezone: %s, Stop timezone: %s", timezone_agency, timezone_stop)
        timetable = stops[row["stop_id"]]["departure"] if row["stop_id"] in stops else []
        stops[row["stop_id"]] = {"stop_id": row['stop_id'], "stop_name": row['stop_name'], "stop_sequence": row['stop_sequence'], "latitude": row['latitude'], "longitude": row['longitude'], "departure": timetable, "offset": offset}
        self._icon = ICONS.get(row['route_type'], ICON)

        element = _build_local_stop_element(
            self, row, row["departure_dt"],
            timezone_agency, timezone_stop, now_tz,
            apply_now_filter=True,
            feed_entities=_trip_entities(self, feed_entities, feed_index, row))
        if element is not None and element not in timetable:
            timetable.append(element)

    local_stops_list = list(stops.values())
    for stop in local_stops_list:
        stop["departure"].sort(key=lambda d: d["departure_datetime"])
    _LOGGER.debug("Interpreted local stop rows returned: %s", local_stops_list)
    return local_stops_list

def get_local_stops_next_departures(self: GTFSLocalStopUpdateCoordinator) -> list[dict[str, Any]]:
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
    latitude, longitude= _tracker_position(self.hass, self._data['device_tracker_id'])
    time_range= str('+' + str(self._data.get("timerange", DEFAULT_LOCAL_STOP_TIMERANGE)) + ' minute')
    time_range_history = str('-' + str(self._data.get("timerange_history", DEFAULT_LOCAL_STOP_TIMERANGE_HISTORY)) + ' minute')
    radius = self._data.get("radius", DEFAULT_LOCAL_STOP_RADIUS) / 111111
    if not latitude or not longitude:
        _LOGGER.error("No latitude and/or longitude for : %s", self._data['device_tracker_id'])
        return []

    # the stored stop times are the network's wall clock: now is read on
    # it, as the journey sensor reads it, not on Home Assistant's
    now = datetime.datetime.fromisoformat(_feed_now(schedule, None, offset))
    rows = _fetch_local_stop_rows(
        schedule, latitude, longitude, radius, time_range, time_range_history, now
    )
    return _interpret_local_stop_rows(self, rows)


async def update_gtfs_local_stops(hass: HomeAssistant, data: Mapping[str, Any]) -> None:
    _LOGGER.debug("Update service for local stops with data: %s", data)
    entries: list[str] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("device_tracker_id") == data["entity_id"] :
            entries.append(entry.entry_id)
    for cf_entry in entries:
        _LOGGER.debug("Reloading local stops for config_entry_id: %s", cf_entry) 
        await hass.config_entries.async_reload(cf_entry)
    return
