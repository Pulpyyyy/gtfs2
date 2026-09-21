"""Data Update coordinator for the GTFS integration."""
from __future__ import annotations

import datetime
from datetime import timedelta
import logging
import os
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util

from .const import (
    DEFAULT_PATH,
    DEFAULT_REFRESH_INTERVAL, 
    DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_RADIUS,
    DEFAULT_API_KEY_NAME,
    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_API_KEY_LOCATION,
    CONF_ACCEPT_HEADER_PB,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_ALERTS_URL,
    ATTR_DUE_IN,
    ATTR_NEXT_RT,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_RT_UPDATED_AT,
    ICON,
    ICONS
)    
from .gtfs_helper import get_gtfs, get_next_departure, check_datasource_index, check_extracting, get_local_stops_next_departures
from .geojson import clear_vehicle_file, vehicle_positions_name
from .gtfs_rt_helper import get_next_services, get_rt_alerts, merge_struck, struck_trips
from .rt_source import rt_feed_config, rt_headers, with_query_key
from .rt_window import rt_window_gate
from .refresh_steps import drop_struck_trips, next_service_date_for
from .departure_attributes import departure_records
from .exports import export_leg, export_route_shape, export_timetable

_LOGGER = logging.getLogger(__name__)


def close_schedule(schedule) -> None:
    """Let a schedule go: its session, then its engine's connections."""
    if schedule and hasattr(schedule, "session"):
        try:
            schedule.session.close()
            schedule.engine.dispose()
        except Exception:  # pylint: disable=broad-except
            pass


def _database_edition(hass, file):
    """The source's database as far as reopening it goes: which file, its size, its last write.

    The file first: a refresh swaps another one in under the same name, and
    a schedule opened on the old one goes on reading it.
    """
    try:
        stat = os.stat(os.path.join(hass.config.path(DEFAULT_PATH), file + ".sqlite"))
    except (OSError, TypeError):
        return None
    return f"{stat.st_ino}:{int(stat.st_mtime)}:{stat.st_size}"


async def schedule_for(coordinator, data):
    """The source's schedule, reopened only when its database changed.

    Opening one is an engine, a create_all over every table and a query of
    the feeds, and every coordinator did it every minute, closing the one
    before. The database only changes when a refresh swaps a new one in or
    a writer adds to it, which its size and last write tell: until then the
    schedule opened is the one used. get_gtfs still decides whenever there
    is no schedule, or the file is not there, which is where it downloads.
    """
    hass = coordinator.hass
    edition = await hass.async_add_executor_job(_database_edition, hass, data["file"])
    current = coordinator._pygtfs
    if (edition is not None and edition == getattr(coordinator, "_pygtfs_edition", None)
            and hasattr(current, "session")):
        return current
    await hass.async_add_executor_job(close_schedule, current)
    # get_gtfs opens the sqlite file and, when it is missing, downloads
    # and unpacks the feed: blocking work that has no place on the loop
    schedule = await hass.async_add_executor_job(get_gtfs, hass, DEFAULT_PATH, data, False)
    coordinator._pygtfs_edition = edition if hasattr(schedule, "session") else None
    return schedule


def shown_departure_left(previous, now) -> bool:
    """Whether the departure the sensor shows has left, and nothing says otherwise.

    The departures are read again at the static refresh interval, 15
    minutes by default, and until then the sensor kept its state on a bus
    already gone. A departure whose time is past is read again at once,
    unless the realtime still has one coming: a late bus stays on the
    board as long as the feed says it has not left.
    """
    shown = (previous.get("next_departure") or {}).get("departure_time")
    if not (hasattr(shown, "tzinfo") and shown.tzinfo is not None) or shown > now:
        return False
    coming = (previous.get("next_departure_realtime_attr") or {}).get(ATTR_NEXT_RT) or []
    return not any(hasattr(moment, "tzinfo") and moment.tzinfo is not None and moment > now
                   for moment in coming)


class GTFSUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the GTFS integration."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=entry.entry_id,
            update_interval=timedelta(minutes=1),
        )
        self.config_entry = entry
        self.hass = hass

        self._pygtfs = ""
        self._data: dict[str, str] = {}
        # the trip whose stops are already exported, so the geojson is
        # rewritten when the journey changes and not on every refresh
        self._route_export_trip = None
        # the writing of the route file under way, if any (see _export_route_shape)
        self._route_task = None
        # the service day and zip edition the timetable file was written for
        self._timetable_export = None
        # the writing of it under way, if any (see _export_timetable)
        self._timetable_task = None
        self._stale_markers_cleaned = False

    async def _async_update_data(self) -> dict[str, str]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)  

        # the same schedule as long as the database is the same one
        self._pygtfs = await schedule_for(self, data)

        self._data = {
            "schedule": self._pygtfs,
            "origin": data["origin"],
            "destination": data["destination"],
            # a train entry's every station at each end, only on the entries
            # that ticked them: the others keep the shape they always had
            **{key: data[key] for key in ("origin_stations", "destination_stations")
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
            "extracting": False,
            "next_departure": {},
            "next_departure_realtime_attr": {},
            "alert": {}
        }           
        
        if check_extracting(self.hass, self.hass.config.path(self._data['gtfs_dir']), self._data['file']):   
            _LOGGER.debug("Cannot update this sensor as still unpacking: %s", self._data["file"])
            self._data.update(previous_data)
            self._data["extracting"] = True
            return self._data
        

        # determine static + rt or only static (refresh schedule depending)
        #1. sensor exists with data but refresh interval not yet reached, use existing data
        if "gtfs_updated_at" in previous_data and (
            datetime.datetime.strptime(previous_data["gtfs_updated_at"], '%Y-%m-%dT%H:%M:%S.%f%z')
            + timedelta(minutes=options.get("refresh_interval", DEFAULT_REFRESH_INTERVAL))
        ) > dt_util.utcnow() + timedelta(seconds=1):
            run_static = False
            _LOGGER.debug("No run static refresh: sensor exists but not yet refresh for name: %s", data["name"])
            if shown_departure_left(previous_data, dt_util.utcnow()):
                run_static = True
                _LOGGER.debug("Run static refresh: the departure shown for %s has left", data["name"])
        else:
            run_static = True
            _LOGGER.debug("Run static refresh: sensor without gtfs data OR refresh for name: %s", data["name"])
        
        # the trip updates of this refresh, when realtime reads them below
        rt_feed = None
        if not run_static:
            # do nothing awaiting refresh interval and use existing data
            self._data = previous_data
            # reaching this point means check_extracting said no, so clear the flag
            # rather than carrying over the one previous_data was left with
            self._data["extracting"] = False
        else:
            check_index = await self.hass.async_add_executor_job(
                    check_datasource_index, self.hass, self._pygtfs, self.hass.config.path(DEFAULT_PATH), data["file"]
                )

            try:
                self._data["next_departure"] = await self.hass.async_add_executor_job(
                    get_next_departure, self.hass, self._data
                )
                self._data["gtfs_updated_at"] = dt_util.utcnow().isoformat()
            except Exception as ex:  # pylint: disable=broad-except
                raise UpdateFailed(f"Error in getting gtfs data: {ex}") from ex
            _LOGGER.debug("GTFS coordinator data from helper: %s", self._data["next_departure"])

            # The route shape comes from the schedule alone: export it here,
            # outside the realtime block, so a map card can draw the journey
            # of an entry that has no vehicle feed at all.
            await export_route_shape(self, data)
            await export_timetable(self, data)

            if not self._data["next_departure"]:
                # Nothing left to show. Look ahead for the next day this journey
                # runs at all, in a key of its own: next_departure has to stay
                # empty, the sensor reads its fields as a real departure.
                #
                # The search starts today, not tomorrow. A line can run today
                # with every departure already behind us, and that is not the
                # same thing as a line resting for days: the sensor tells the
                # two apart by whether the date it gets back is today's.
                self._data["next_service_date"] = await next_service_date_for(
                    self.hass, self._pygtfs, data, self._data.get("offset", 0))
        
        # collect and return rt attributes
        # STILL REQUIRES A SOLUTION IF CONNECTION TIMING OUT
        # the feeds come from the source's datasource entry when it exists,
        # from this entry's own options otherwise: one configuration per
        # source, every sensor of the source follows it
        rt_cfg, rt_active = rt_feed_config(self.hass, self.config_entry)
        rt_paused = None
        if rt_active:
            # the polling window is derived from the timetable: outside it
            # the feeds are left alone and the static screen carries on
            rt_paused = await self.hass.async_add_executor_job(
                rt_window_gate, self.hass, self._data["file"], self._pygtfs,
                with_query_key(rt_cfg.get(CONF_TRIP_UPDATE_URL), rt_cfg))
            if rt_paused:
                _LOGGER.debug("GTFS RT: %s is outside its service window (%s), feeds not read",
                              self._data["file"], rt_paused)
                rt_active = False
                if rt_cfg.get(CONF_VEHICLE_POSITION_URL):
                    # nothing will refresh the positions until the window
                    # opens again, so the map is told rather than left on
                    # the last vehicles seen
                    departure = self._data.get("next_departure") or {}
                    await self.hass.async_add_executor_job(
                        clear_vehicle_file, self.hass,
                        str(departure.get("route_id")
                            or (data.get("route") or "").split(": ")[0]),
                        str(departure.get("trip_direction_id", data.get("direction"))))
        if rt_active:
            # No next_departure does NOT mean no bus: the last scheduled
            # departure of the day can still be on its way, late, and the
            # realtime feed is the only one who knows. Skipping the whole
            # block here (the first fix for the origin_stop_sequence
            # KeyError) made that bus vanish from the board while the map
            # still showed it rolling. The block now runs with fallbacks
            # taken from the config entry instead; every read below is a
            # .get, which is what the KeyError actually required.
            if not self._data.get("next_departure"):
                _LOGGER.debug("GTFS RT: no scheduled departure left, realtime runs on config-entry fallbacks")
            self._get_next_service = {}
            """Initialize the info object."""
            self._route_delimiter = None
            self._trip_update_url = with_query_key(rt_cfg.get(CONF_TRIP_UPDATE_URL), rt_cfg)
            self._vehicle_position_url = with_query_key(rt_cfg.get(CONF_VEHICLE_POSITION_URL), rt_cfg)
            self._alerts_url = with_query_key(rt_cfg.get(CONF_ALERTS_URL), rt_cfg)
            self._headers = rt_headers(rt_cfg)
            self._icon = ICONS.get(int(self._data["route_type"]), ICON)
            self.info = {}
            self._route_id = self._data["next_departure"].get("route_id", None)
            if self._route_id == None:
                _LOGGER.debug("GTFS RT: no route_id in sensor data, using route_id from config_entry")
                self._route_id = data["route"].split(": ")[0]
            self._stop_id = self._data["next_departure"].get("origin_stop_id", data["origin"]).split(": ")[0]
            self._stop_sequence = self._data["next_departure"].get("origin_stop_sequence", None)
            self._destination_id = data["destination"].split(": ")[0]
            self._trip_id = self._data.get('next_departure', {}).get('trip_id', None) or "no_trip_information"
            self._trip_short_name = self._data.get('next_departure', {}).get('trip_short_name', None)
            self._direction = str(self._data.get('next_departure', {}).get('trip_direction_id', data["direction"]))
            self._trip_list = self._data["next_departure"].get("next_departures_trip_id", [])[:10]
            self._relative = False
            # the alerts first and on their own: they are a feed of their
            # own, often a different host, and read together with the trip
            # updates one bad answer there took the departure times down
            # with it, leaving the sensor on last cycle's
            try:
                self._get_rt_alerts = await self.hass.async_add_executor_job(get_rt_alerts, self)
                self._data["alert"] = self._get_rt_alerts
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Error getting gtfs realtime alerts, for origin: %s with error: %s", data["origin"], ex)
            try:
                self._get_next_service = await self.hass.async_add_executor_job(get_next_services, self)
                self._data["next_departure_realtime_attr"] = self._get_next_service
                self._data["next_departure_realtime_attr"]["gtfs_rt_updated_at"] = dt_util.utcnow()
                await drop_struck_trips(self, data, run_static)
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Error getting gtfs realtime data, for origin: %s with error: %s", data["origin"], ex)
                await self._read_records()
                return self._data
            # the trip updates just read, kept for the leg file below:
            # they carry the realtime of every stop, the sensor reads one
            rt_feed = getattr(self, "_feed_entities", None)
            if self._vehicle_position_url:
                # let map cards locate the geojson written by get_rt_vehicle_positions
                self._data["vehicle_positions_file"] = vehicle_positions_name(self._route_id, self._direction)
            if self._vehicle_position_url and not self._stale_markers_cleaned:
                self._cleanup_stale_vehicle_markers()
                self._stale_markers_cleaned = True
        else:
            # paused by the window, switched off, or never configured: the
            # delays and alerts read before are the ones of another moment,
            # and carried over from the previous cycle they were served as
            # if they still stood. The timetable alone speaks from here
            self._data["next_departure_realtime_attr"] = {}
            self._data["alert"] = {}
            if rt_paused is None:
                _LOGGER.debug("GTFS RT: realtime not active for this entry, neither on its source nor in its options")

        # the leg file follows every clock that can move: the list of
        # departures on a static refresh, their realtime on a realtime one
        if run_static or rt_feed is not None:
            await export_leg(self, data, rt_feed)

        await self._read_records()
        return self._data

    async def _read_records(self) -> None:
        """Read, off the loop, the rows the sensor describes the departure with.

        Read again when the departure shown names other ones, or at each
        static refresh: the stops, trip and route of one departure do not
        move from a minute to the next, and five lookups a sensor a minute
        add up. The records outlive the schedule they came from, which is
        reopened every cycle: they are plain rows, read in full.
        """
        departure = self._data.get("next_departure") or {}
        key = (departure.get("origin_stop_id"), departure.get("destination_stop_id"),
               departure.get("trip_id"), departure.get("route_id"),
               self._data.get("route"), self._data.get("gtfs_updated_at"))
        if key == getattr(self, "_records_key", None) and self._data.get("records"):
            return
        try:
            self._data["records"] = await self.hass.async_add_executor_job(
                departure_records, self._pygtfs, self._data)
            self._records_key = key
        except Exception as ex:  # pylint: disable=broad-except
            # the attributes that read them go without for a cycle
            _LOGGER.debug("Could not read the departure's records: %s", ex)

    def _remember_struck(self) -> None:
        """Fold what the last realtime reading struck out into what this
        static period has seen: {trip_id: start_date or None}, cancelled
        and skipping the origin kept apart."""
        self._struck_cancelled = merge_struck(getattr(self, "_struck_cancelled", None),
                                              getattr(self, "_rt_cancelled", None))
        self._struck_skipped = merge_struck(getattr(self, "_struck_skipped", None),
                                            getattr(self, "_rt_skipped", None))

    def _cleanup_stale_vehicle_markers(self) -> None:
        """One-shot removal of the stale vehicle markers of this route.

        geo_json_events registers every marker of the positions file as a
        geo_location entity and drops its state when the vehicle leaves the
        feed, but the registry entry stays behind. As the marker id embeds the
        trip, every run leaves a new entry and the registry grows without
        bound. Drop the entries of this route that no longer have a state; a
        trip that runs again is simply registered afresh.
        """
        registry = er.async_get(self.hass)
        pattern = re.compile(re.escape(str(self._route_id)) + r"\(\d+\)\d{1,3}$")
        for entry in list(registry.entities.values()):
            if (
                entry.domain == "geo_location"
                and entry.platform == "geo_json_events"
                and pattern.search(entry.unique_id)
                and self.hass.states.get(entry.entity_id) is None
            ):
                _LOGGER.info(
                    "Removing stale vehicle marker %s (unique_id: %s)",
                    entry.entity_id,
                    entry.unique_id,
                )
                registry.async_remove(entry.entity_id)

class GTFSLocalStopUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for getting local stops."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=entry.entry_id,
            update_interval=timedelta(minutes=entry.options.get("local_stop_refresh_interval", DEFAULT_LOCAL_STOP_REFRESH_INTERVAL)),
        )
        self.config_entry = entry
        self.hass = hass
        
        self._pygtfs = ""
        self._data: dict[str, str] = {}

    async def _async_update_data(self) -> dict[str, str]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""      
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)

        # the same schedule as long as the database is the same one
        self._pygtfs = await schedule_for(self, data)

        self._data = {
            "schedule": self._pygtfs,
            "gtfs_dir": DEFAULT_PATH,
            "name": data["name"],
            "file": data["file"],
            "offset": options["offset"] if "offset" in options else 0,
            "timerange": options.get("timerange", DEFAULT_LOCAL_STOP_TIMERANGE),
            "radius": options.get("radius", DEFAULT_LOCAL_STOP_RADIUS),
            "device_tracker_id": data["device_tracker_id"],
            "extracting": False,
        }           
        self._data["gtfs_updated_at"] = dt_util.utcnow().isoformat()

        
        if check_extracting(self.hass, self.hass.config.path(self._data['gtfs_dir']), self._data['file']):   
            _LOGGER.debug("Cannot update this sensor as still unpacking: %s", self._data["file"])
            self._data.update(previous_data)
            self._data["extracting"] = True
            return self._data

        check_index = await self.hass.async_add_executor_job(
                check_datasource_index, self.hass, self._pygtfs, self.hass.config.path(DEFAULT_PATH), data["file"]
            )
            
        self._realtime = False
        # same resolution as the generic coordinator: the datasource entry
        # of the source first, this entry's own options as the fallback
        rt_cfg, rt_active = rt_feed_config(self.hass, self.config_entry)
        if rt_active:
            self._realtime = True
            self._get_next_service = {}
            """Initialize the info object."""
            self._route_delimiter = None
            self._headers = {}
            self._rt_group = "trip"
            self._trip_update_url = with_query_key(rt_cfg.get(CONF_TRIP_UPDATE_URL), rt_cfg)
            # a local stops sensor lists departures of every line around a
            # position, so it owns no route to draw: reading the vehicle
            # feed here would fetch it once per listed line and write the
            # map file of a route this entry does not speak for
            self._vehicle_position_url = None
            self._alerts_url = rt_cfg.get(CONF_ALERTS_URL, None)
            if not self._trip_update_url:
                # local stops read nothing but trip updates: a source living on
                # alerts or vehicle positions alone has nothing for them, and
                # get_local_stops_next_departures would otherwise try to
                # download the missing feed and drop every departure with it
                self._realtime = False
            # what the key is and where it goes, for the download
            # get_local_stops_next_departures runs; kept apart from the
            # headers, which are sent to the host as they are and take
            # nothing but strings
            self._rt_key = {
                CONF_API_KEY: rt_cfg.get(CONF_API_KEY),
                CONF_API_KEY_NAME: rt_cfg.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME),
                CONF_API_KEY_LOCATION: rt_cfg.get(CONF_API_KEY_LOCATION),
                CONF_ACCEPT_HEADER_PB: rt_cfg.get(CONF_ACCEPT_HEADER_PB, False),
            }
            if rt_cfg.get(CONF_API_KEY_LOCATION, None) == "header":
                self._headers = rt_headers(rt_cfg)
                

        if self._realtime:
            # same automatic window as the generic coordinator; the url the
            # fetches use is the one already carrying its query key
            rt_paused = await self.hass.async_add_executor_job(
                rt_window_gate, self.hass, data["file"], self._pygtfs,
                self._trip_update_url)
            if rt_paused:
                _LOGGER.debug("GTFS RT: %s is outside its service window (%s), feeds not read",
                              data["file"], rt_paused)
                self._realtime = False
        try:
            self._data["local_stops_next_departures"] = await self.hass.async_add_executor_job(
                    get_local_stops_next_departures, self
                )
        except Exception as ex:
            _LOGGER.error("Error getting local stops data: %s", ex)
            raise UpdateFailed(f"Error in getting local stops data: {ex}")
        #_LOGGER.debug("Data from coordinator: %s", self._data)              
        return self._data
