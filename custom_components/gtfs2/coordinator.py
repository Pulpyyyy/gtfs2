"""Data Update coordinator for the GTFS integration."""
from __future__ import annotations

import datetime
from datetime import timedelta
import logging
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
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_RT_UPDATED_AT,
    ICON,
    ICONS
)
from .gtfs_helper import get_gtfs, get_next_departure, get_next_service_date, check_datasource_index, create_trip_geojson, check_extracting, get_local_stops_next_departures, update_route_geojson, route_geojson_name, vehicle_positions_name
from .gtfs_rt_helper import get_next_services, get_rt_alerts
from .rt_source import rt_feed_config, rt_headers, with_query_key
from .rt_window import rt_window_gate

_LOGGER = logging.getLogger(__name__)


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
        self._stale_markers_cleaned = False

    async def _async_update_data(self) -> dict[str, str]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)  

        if self._pygtfs and hasattr(self._pygtfs, 'session'):
            try:
                self._pygtfs.session.close()
                self._pygtfs.engine.dispose()
            except Exception:
                pass

        self._pygtfs = get_gtfs(
            self.hass, DEFAULT_PATH, data, False
        )        

        self._data = {
            "schedule": self._pygtfs,
            "origin": data["origin"],
            "destination": data["destination"],
            "offset": options["offset"] if "offset" in options else 0,
            # entries created before this key was always written are still out
            # there, and a KeyError here fails the whole sensor platform
            "include_tomorrow": data.get("include_tomorrow", False),
            "gtfs_dir": DEFAULT_PATH,
            "name": data["name"],
            "file": data["file"],
            "route_type": data["route_type"],
            "route": data["route"],
            "direction": data.get("direction"),
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
        else:
            run_static = True
            _LOGGER.debug("Run static refresh: sensor without gtfs data OR refresh for name: %s", data["name"])
        
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
            await self._export_route_shape(data)

            if not self._data["next_departure"]:
                # Nothing left to show. Look ahead for the next day this journey
                # runs at all, in a key of its own: next_departure has to stay
                # empty, the sensor reads its fields as a real departure.
                #
                # The search starts today, not tomorrow. A line can run today
                # with every departure already behind us, and that is not the
                # same thing as a line resting for days: the sensor tells the
                # two apart by whether the date it gets back is today's.
                try:
                    self._data["next_service_date"] = await self.hass.async_add_executor_job(
                        get_next_service_date, self._pygtfs,
                        data["origin"].split(": ")[0], data["destination"].split(": ")[0],
                        (dt_util.now() + timedelta(
                            minutes=self._data.get("offset", 0) or 0)).strftime("%Y-%m-%d"),
                        data["route_type"],
                    )
                except Exception as ex:  # pylint: disable=broad-except
                    # only enriches an attribute: never fail the update over it
                    _LOGGER.warning("Could not get next service date: %s", ex)
                    self._data["next_service_date"] = None
        
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
            try:
                self._get_rt_alerts = await self.hass.async_add_executor_job(get_rt_alerts, self)
                self._get_next_service = await self.hass.async_add_executor_job(get_next_services, self)
                self._data["next_departure_realtime_attr"] = self._get_next_service
                self._data["next_departure_realtime_attr"]["gtfs_rt_updated_at"] = dt_util.utcnow()
                self._data["alert"] = self._get_rt_alerts
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.error("Error getting gtfs realtime data, for origin: %s with error: %s", data["origin"], ex)
                return self._data
            if self._vehicle_position_url:
                # let map cards locate the geojson written by get_rt_vehicle_positions
                self._data["vehicle_positions_file"] = vehicle_positions_name(self._route_id, self._direction)
            if self._vehicle_position_url and not self._stale_markers_cleaned:
                self._cleanup_stale_vehicle_markers()
                self._stale_markers_cleaned = True
        elif rt_paused is None:
            _LOGGER.debug("GTFS RT: realtime not active for this entry, neither on its source nor in its options")

        return self._data

    async def _export_route_shape(self, data) -> None:
        """Write the geojson of the journey the sensor is following.

        Shape and stops are read from the schedule, so this owes nothing to
        realtime: an entry without a vehicle feed, or with realtime switched
        off entirely, still gets its line drawn on a map card. Nor does it owe
        anything to there being a departure today. Rewritten only when the
        drawn trip changes, which is what makes it cheap enough to sit on
        every static refresh.
        """
        departure = self._data.get("next_departure") or {}
        route_id = departure.get("route_id", None) or (data.get("route") or "").split(": ")[0]
        direction = str(departure.get("trip_direction_id", data.get("direction")))
        # the file is named from the route and the direction, both known even
        # once the last departure of the day is behind us: the attribute stays
        # put so a card keeps its route through the evening, and it is named
        # before the first write rather than a refresh later
        if route_id and direction not in ("None", ""):
            self._data["route_geojson_file"] = route_geojson_name(route_id, direction)
        # No departure to point at (last one of the day gone, or a line resting
        # for days) is not a reason to leave the map empty: the export then
        # picks a representative trip of the same route and direction. Keyed
        # so it is written once, and rewritten as soon as a real trip is back.
        trip_id = departure.get("trip_id", None)
        export_key = trip_id or f"resting:{route_id}_{direction}"
        if not route_id or export_key == self._route_export_trip:
            return
        self._route_id = route_id
        self._direction = direction
        try:
            await self.hass.async_add_executor_job(update_route_geojson, self)
            self._route_export_trip = export_key
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Error writing route geojson: %s", ex)
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

        if self._pygtfs and hasattr(self._pygtfs, 'session'):
            try:
                self._pygtfs.session.close()
                self._pygtfs.engine.dispose()
            except Exception:
                pass
                
        self._pygtfs = get_gtfs(
            self.hass, DEFAULT_PATH, data, False
        )        
        self._data = {
            "schedule": self._pygtfs,
            "include_tomorrow": True,
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
            self._vehicle_position_url = rt_cfg.get(CONF_VEHICLE_POSITION_URL, None)
            self._alerts_url = rt_cfg.get(CONF_ALERTS_URL, None)
            if not self._trip_update_url:
                # local stops read nothing but trip updates: a source living on
                # alerts or vehicle positions alone has nothing for them, and
                # get_local_stops_next_departures would otherwise try to
                # download the missing feed and drop every departure with it
                self._realtime = False
            if rt_cfg.get(CONF_API_KEY_LOCATION, None) == "header":
                # get_local_stops_next_departures reads the raw key fields
                # back out of this dict, so they ride along with the header
                self._headers = {rt_cfg.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME): rt_cfg.get(CONF_API_KEY)}
                self._headers[CONF_API_KEY_LOCATION] = rt_cfg.get(CONF_API_KEY_LOCATION, None)
                self._headers[CONF_API_KEY_NAME] = rt_cfg.get(CONF_API_KEY_NAME, None)
                self._headers[CONF_API_KEY] = rt_cfg.get(CONF_API_KEY, None)
                self._headers[CONF_ACCEPT_HEADER_PB] = rt_cfg.get(CONF_ACCEPT_HEADER_PB, False)
            _LOGGER.debug("RT header: %s", self._headers)
                

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
