"""Data Update coordinator for the GTFS integration."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
import datetime
from datetime import timedelta
import logging
import re
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

from .const import (
    id_of,
    DEFAULT_PATH,
    DEFAULT_REFRESH_INTERVAL, 
    DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_RADIUS,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_VEHICLE_MAX_AGE,
    DEFAULT_VEHICLE_MAX_AGE,
    CONF_ALERTS_URL,
    ATTR_NEXT_RT,
    ATTR_NEXT_RT_TRIPS,
    ICON,
    ICONS
)    
from .gtfs_db import close_schedule, file_edition, real_path
from .gtfs_helper import get_gtfs, get_next_departure, check_datasource_index, check_extracting, journey_data, shown_ends
from .local_stops import get_local_stops_next_departures, drop_gone_local_departures
from .geojson import clear_vehicle_file, vehicle_positions_name
from .gtfs_rt_helper import _names_trip, get_next_services, get_rt_alerts, merge_struck
from .rt_source import rt_feed_config, rt_headers, with_query_key
from .rt_window import rt_window_gate
from .refresh_steps import drop_struck_trips, next_service_date_for
from .departure_attributes import departure_records
from .exports import export_leg, export_route_shape, export_timetable

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


async def _still_unpacking(coordinator: GTFSUpdateCoordinator | GTFSLocalStopUpdateCoordinator,
                           previous_data: dict[str, Any]) -> bool:
    """Whether the source is still being unpacked; its last reading is then
    kept, marked extracting, for the entry to show meanwhile."""
    data = coordinator._data
    # two file checks, off the event loop like every other file read here
    if not await coordinator.hass.async_add_executor_job(
            check_extracting, coordinator.hass,
            coordinator.hass.config.path(data['gtfs_dir']), data['file']):
        return False
    _LOGGER.debug("Cannot update this sensor as still unpacking: %s", data["file"])
    data.update(previous_data)
    data["extracting"] = True
    return True


def _database_edition(hass: HomeAssistant, file: str) -> tuple[int, int, int] | None:
    """The source's database as far as reopening it goes: which file, its size, its last write.

    The file first: a refresh swaps another one in under the same name, and
    a schedule opened on the old one goes on reading it.
    """
    try:
        path = real_path(hass.config.path(DEFAULT_PATH), file)
    except TypeError:
        return None
    return file_edition(path)


async def schedule_for(coordinator: GTFSUpdateCoordinator | GTFSLocalStopUpdateCoordinator,
                       data: Mapping[str, Any]) -> Schedule | str | None:
    """The source's schedule, reopened only when its database changed.

    Opening one is an engine, a create_all over every table and a query of
    the feeds, and every coordinator did it every minute, closing the one
    before. The database only changes when a refresh swaps a new one in or
    a writer adds to it, which its size and last write tell: until then the
    schedule opened is the one used. get_gtfs decides whenever there is no
    schedule, or the file is not there: it answers why, and builds nothing.
    """
    hass = coordinator.hass
    edition = await hass.async_add_executor_job(_database_edition, hass, data["file"])
    current = coordinator._pygtfs
    if (edition is not None and edition == coordinator._pygtfs_edition
            and hasattr(current, "session")):
        return current
    await hass.async_add_executor_job(close_schedule, current)
    # get_gtfs opens the sqlite file: blocking work that has no place on
    # the loop
    schedule = await hass.async_add_executor_job(get_gtfs, hass, DEFAULT_PATH, data)
    coordinator._pygtfs_edition = edition if hasattr(schedule, "session") else None
    return schedule


def shown_departure_left(previous: Mapping[str, Any], now: datetime.datetime) -> bool:
    """Whether the departure the sensor shows has left, and nothing says otherwise.

    The departures are read again at the static refresh interval, 15
    minutes by default, and until then the sensor kept its state on a bus
    already gone. A departure whose time is past is read again at once,
    unless the realtime still has that trip coming: a late bus stays on the
    board as long as the feed says it has not left. Another trip coming is
    no reason: on a frequent line the feed always has one, and IDFM metro
    4 kept a 09:17:56 departure on the board at 09:27.
    """
    departure = previous.get("next_departure") or {}
    shown: Any = departure.get("departure_time")
    if not (hasattr(shown, "tzinfo") and shown.tzinfo is not None) or shown > now:
        return False
    realtime = previous.get("next_departure_realtime_attr") or {}
    coming = zip(realtime.get(ATTR_NEXT_RT) or [], realtime.get(ATTR_NEXT_RT_TRIPS) or [])
    return not any(hasattr(moment, "tzinfo") and moment.tzinfo is not None and moment > now
                   and _names_trip(departure.get("trip_id"), trip)
                   for moment, trip in coming)


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

        self._pygtfs: Schedule | str | None = ""
        # what the database file was when the schedule was opened (see schedule_for)
        self._pygtfs_edition: tuple[int, int, int] | None = None
        self._data: dict[str, Any] = {}
        # the trip picked to draw the route, and what it was picked for (see
        # export_route_shape): picked again when the stops or the database change
        self._representative_pick: tuple[str, str, str, str, tuple[int, int, int] | None] | None = None
        self._representative_trip: str | None = None
        # the trip whose stops are already exported, so the geojson is
        # rewritten when the journey changes and not on every refresh
        self._route_export_trip: tuple[str, str, tuple[int, int, int] | None,
                                       tuple[int, int, int] | None] | None = None
        # the writing of the route file under way, if any (see _export_route_shape)
        self._route_task: asyncio.Task[None] | None = None
        # the service day, zip and database editions the timetable file was written for
        self._timetable_export: tuple[str, tuple[int, int, int] | None,
                                      tuple[int, int, int] | None] | None = None
        # the writing of it under way, if any (see _export_timetable)
        self._timetable_task: asyncio.Task[None] | None = None
        self._stale_markers_cleaned = False

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)  

        # the same schedule as long as the database is the same one
        self._pygtfs = await schedule_for(self, data)

        self._data = self._entry_data(data, options)

        if await _still_unpacking(self, previous_data):
            return self._data

        # a database gone since the last reading: read the timetable now,
        # which empties the board, rather than showing the last departures
        # until the refresh interval is up
        run_static = (self._pygtfs is None or isinstance(self._pygtfs, str)
                      or self._static_refresh_due(previous_data, options, data["name"]))

        # the trip updates of this refresh, when realtime reads them below
        rt_feed = None
        if not run_static:
            # do nothing awaiting refresh interval and use existing data
            self._data = previous_data
            # reaching this point means check_extracting said no, so clear the flag
            # rather than carrying over the one previous_data was left with
            self._data["extracting"] = False
            # and the schedule of this minute, not the last reading's:
            # schedule_for closes that one when the database changed, and
            # the realtime and the leg file read through what is here
            self._data["schedule"] = self._pygtfs
        else:
            await self._read_timetable(data)

        # collect and return rt attributes
        # STILL REQUIRES A SOLUTION IF CONNECTION TIMING OUT
        # the feeds come from the source's datasource entry when it exists,
        # from this entry's own options otherwise: one configuration per
        # source, every sensor of the source follows it
        rt_cfg, rt_active = rt_feed_config(self.hass, self.config_entry)
        rt_paused = None
        if rt_active:
            rt_paused = await self._realtime_paused(data, rt_cfg)
        if rt_active and not rt_paused:
            # the trip updates just read, kept for the leg file below:
            # they carry the realtime of every stop, the sensor reads one.
            # Not read, the departures stand as the timetable gave them,
            # and the leg file still follows a timetable read this minute:
            # it used to return here, the file left on the last list
            if await self._read_realtime(data, rt_cfg, run_static):
                rt_feed = getattr(self, "_feed_entities", None)
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

    def _entry_data(self, data: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
        """What a refresh starts from: the entry's own fields, no departure yet."""
        return {
            **journey_data(self._pygtfs, data, options),
            "extracting": False,
            "next_departure": {},
            "next_departure_realtime_attr": {},
            "alert": {}
        }

    def _static_refresh_due(self, previous_data: dict[str, Any], options: Mapping[str, Any],
                            name: str) -> bool:
        """Whether the departures are read again from the timetable this minute."""
        # determine static + rt or only static (refresh schedule depending)
        #1. sensor exists with data but refresh interval not yet reached, use existing data
        # read back with fromisoformat, the reverse of the isoformat it was
        # written with: a strptime on '.%f' failed the whole update on a
        # reading made on a whole second, which isoformat writes without
        # its microseconds
        if "gtfs_updated_at" in previous_data and (
            datetime.datetime.fromisoformat(previous_data["gtfs_updated_at"])
            + timedelta(minutes=options.get("refresh_interval", DEFAULT_REFRESH_INTERVAL))
        ) > dt_util.utcnow() + timedelta(seconds=1):
            _LOGGER.debug("No run static refresh: sensor exists but not yet refresh for name: %s", name)
            if shown_departure_left(previous_data, dt_util.utcnow()):
                _LOGGER.debug("Run static refresh: the departure shown for %s has left", name)
                return True
            return False
        _LOGGER.debug("Run static refresh: sensor without gtfs data OR refresh for name: %s", name)
        return True

    async def _read_timetable(self, data: Mapping[str, Any]) -> None:
        """Read the departures from the timetable, and write the files drawn from it."""
        if self._pygtfs is None or isinstance(self._pygtfs, str):
            # a sentinel of get_gtfs: no database to read. The index check,
            # the departures, the route shape and the next service date each
            # said so in the log beside the sensor, five warnings a sensor
            # for one missing file; the sensor says it once. No reading time
            # is written, so the minute after a refresh builds the database
            # reads it
            _LOGGER.debug("No usable schedule for %s (%s), timetable not read",
                          data["file"], self._pygtfs or "empty")
            return
        await self.hass.async_add_executor_job(
                check_datasource_index, self.hass, self._pygtfs, self.hass.config.path(DEFAULT_PATH), data["file"]
            )

        try:
            self._data["next_departure"] = await self.hass.async_add_executor_job(
                get_next_departure, self.hass, self._data
            )
            self._data["gtfs_updated_at"] = dt_util.utcnow().isoformat()
        except Exception as ex:  # pylint: disable=broad-except
            if not isinstance(ex, SQLAlchemyError):
                # Home Assistant says an UpdateFailed in one line: enough for
                # a database that cannot answer, not for a mistake
                _LOGGER.exception("Error in getting gtfs data: %s", ex)
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

    async def _realtime_paused(self, data: Mapping[str, Any], rt_cfg: Mapping[str, Any]) -> str | None:
        """Why the realtime feeds are not read now, None when they are."""
        # the polling window is derived from the timetable: outside it
        # the feeds are left alone and the static screen carries on
        rt_paused = await self.hass.async_add_executor_job(
            rt_window_gate, self.hass, self._data["file"], self._pygtfs,
            with_query_key(rt_cfg.get(CONF_TRIP_UPDATE_URL), rt_cfg))
        if rt_paused:
            if rt_cfg.get(CONF_VEHICLE_POSITION_URL):
                # nothing will refresh the positions until the window
                # opens again, so the map is told rather than left on
                # the last vehicles seen
                route_id, direction, _origin, _destination = shown_ends(
                    data, self._data.get("next_departure") or {})
                await self.hass.async_add_executor_job(
                    clear_vehicle_file, self.hass, route_id, direction)
        return rt_paused

    def _realtime_targets(self, data: Mapping[str, Any], rt_cfg: Mapping[str, Any]) -> None:
        """Set what the realtime readers read off the coordinator: the feeds,
        and the route, stop and trip of the departure shown."""
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
        self._get_next_service: dict[str, Any] = {}
        self._route_delimiter: str | None = None
        self._trip_update_url = with_query_key(rt_cfg.get(CONF_TRIP_UPDATE_URL), rt_cfg)
        self._vehicle_position_url = with_query_key(rt_cfg.get(CONF_VEHICLE_POSITION_URL), rt_cfg)
        self._vehicle_max_age = rt_cfg.get(CONF_VEHICLE_MAX_AGE, DEFAULT_VEHICLE_MAX_AGE)
        self._alerts_url = with_query_key(rt_cfg.get(CONF_ALERTS_URL), rt_cfg)
        self._headers = rt_headers(rt_cfg)
        self._icon = ICONS.get(int(self._data["route_type"]), ICON)
        self._destination_id = id_of(data["destination"])
        self._follow_departure(data)
        self._relative = False

    def _follow_departure(self, data: Mapping[str, Any]) -> None:
        """Point the realtime readers at the departure shown: its route,
        stop, trip and direction, the entry's own where it names none."""
        departure = self._data.get("next_departure") or {}
        self._route_id, self._direction, self._stop_id, _destination = shown_ends(data, departure)
        self._stop_sequence = departure.get("origin_stop_sequence", None)
        self._trip_id = departure.get('trip_id', None) or "no_trip_information"
        self._trip_short_name = departure.get('trip_short_name', None)
        self._trip_list = departure.get("next_departures_trip_id", [])[:10]

    async def _read_realtime(self, data: Mapping[str, Any], rt_cfg: Mapping[str, Any],
                             run_static: bool) -> bool:
        """Read the alerts, then the trip updates; False when the trip
        updates could not be read, the timetable standing alone."""
        self._realtime_targets(data, rt_cfg)
        # the alerts first and on their own: they are a feed of their
        # own, often a different host, and read together with the trip
        # updates one bad answer there took the departure times down
        # with it, leaving the sensor on last cycle's
        try:
            self._data["alert"] = await self.hass.async_add_executor_job(get_rt_alerts, self)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error getting gtfs realtime alerts, for origin: %s with error: %s", data["origin"], ex)
        try:
            self._get_next_service = await self.hass.async_add_executor_job(get_next_services, self)
            self._data["next_departure_realtime_attr"] = self._get_next_service
            self._data["next_departure_realtime_attr"]["gtfs_rt_updated_at"] = dt_util.utcnow()
            await drop_struck_trips(self, data, run_static)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error getting gtfs realtime data, for origin: %s with error: %s", data["origin"], ex)
            return False
        if self._vehicle_position_url:
            # let map cards locate the geojson written by get_rt_vehicle_positions
            self._data["vehicle_positions_file"] = vehicle_positions_name(self._route_id, self._direction)
        if self._vehicle_position_url and not self._stale_markers_cleaned:
            self._cleanup_stale_vehicle_markers()
            self._stale_markers_cleaned = True
        return True

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
        except SQLAlchemyError as ex:
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
        """Initialize the coordinator.

        It runs every minute and reads the stops around the tracker at the
        entry's own pace, 15 minutes by default: in between it only takes
        out the departures gone. Its sensors hear of a minute only when
        one has gone (always_update False): the state they write carries
        the time, and a line of history a minute per stop was for nothing.
        """
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=entry.entry_id,
            update_interval=timedelta(minutes=1),
            always_update=False,
        )
        self.config_entry = entry
        self.hass = hass

        self._pygtfs: Schedule | str | None = ""
        # what the database file was when the schedule was opened (see schedule_for)
        self._pygtfs_edition: tuple[int, int, int] | None = None
        self._data: dict[str, Any] = {}

    def _read_lately(self, previous_data: dict[str, Any], options: Mapping[str, Any]) -> bool:
        """Whether the stops were read from this very database within the
        entry's pace, and nothing is being written."""
        if not previous_data.get("gtfs_updated_at") or previous_data.get("extracting"):
            return False
        if previous_data.get("schedule") is not self._pygtfs:
            # a database swapped in or reopened: read it at once
            return False
        pace = timedelta(minutes=options.get("local_stop_refresh_interval", DEFAULT_LOCAL_STOP_REFRESH_INTERVAL))
        read = datetime.datetime.fromisoformat(previous_data["gtfs_updated_at"])
        return read + pace > dt_util.utcnow() + timedelta(seconds=1)

    def _without_gone(self, previous_data: dict[str, Any]) -> dict[str, Any]:
        """The last answer without the departures gone since; the very
        same answer when none has, so the sensors are not updated."""
        listed = previous_data.get("local_stops_next_departures") or []
        left = drop_gone_local_departures(
            listed, dt_util.now() + timedelta(minutes=previous_data.get("offset", 0)))
        if left is listed:
            return self.data
        _LOGGER.debug("Local stops of %s: departures gone taken out", previous_data.get("name"))
        return {**previous_data, "local_stops_next_departures": left}

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from GTFS and GTFS relatime, depending refresh interval"""
        data = self.config_entry.data
        options = self.config_entry.options
        previous_data = {} if self.data is None else self.data.copy()
        _LOGGER.debug("Previous data: %s", previous_data)

        # the same schedule as long as the database is the same one
        self._pygtfs = await schedule_for(self, data)

        if self._read_lately(previous_data, options):
            return self._without_gone(previous_data)

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

        
        if await _still_unpacking(self, previous_data):
            return self._data

        if self._no_schedule(data["file"]):
            self._data["local_stops_next_departures"] = []
            return self._data

        await self.hass.async_add_executor_job(
                check_datasource_index, self.hass, self._pygtfs, self.hass.config.path(DEFAULT_PATH), data["file"]
            )

        self._realtime = False
        # same resolution as the generic coordinator: the datasource entry
        # of the source first, this entry's own options as the fallback
        rt_cfg, rt_active = rt_feed_config(self.hass, self.config_entry)
        if rt_active:
            self._realtime = True
            self._get_next_service: dict[str, Any] = {}
            """Initialize the info object."""
            self._route_delimiter: str | None = None
            self._headers = rt_headers(rt_cfg) or {}
            self._rt_group = "trip"
            self._trip_update_url = with_query_key(rt_cfg.get(CONF_TRIP_UPDATE_URL), rt_cfg)
            # a local stops sensor lists departures of every line around a
            # position, so it owns no route to draw: reading the vehicle
            # feed here would fetch it once per listed line and write the
            # map file of a route this entry does not speak for
            self._vehicle_position_url: str | None = None
            self._alerts_url = rt_cfg.get(CONF_ALERTS_URL, None)
            if not self._trip_update_url:
                # local stops read nothing but trip updates: a source living on
                # alerts or vehicle positions alone has nothing for them, and
                # get_local_stops_next_departures would otherwise try to
                # download the missing feed and drop every departure with it
                self._realtime = False
                

        if self._realtime:
            # same automatic window as the generic coordinator; the url the
            # fetches use is the one already carrying its query key
            rt_paused = await self.hass.async_add_executor_job(
                rt_window_gate, self.hass, data["file"], self._pygtfs,
                self._trip_update_url)
            if rt_paused:
                self._realtime = False
        try:
            self._data["local_stops_next_departures"] = await self.hass.async_add_executor_job(
                    get_local_stops_next_departures, self
                )
        except Exception as ex:
            _LOGGER.exception("Error getting local stops data: %s", ex)
            raise UpdateFailed(f"Error in getting local stops data: {ex}")
        #_LOGGER.debug("Data from coordinator: %s", self._data)
        return self._data

    def _no_schedule(self, file: str) -> bool:
        """Whether get_gtfs answered a word, no database to read, said once.

        The index check and the stops each warned at every refresh. The
        stop sensors cannot say it for them: there is one per stop found,
        and with no database none is found. So the coordinator says it,
        once for each reason, and again only once the database is back
        and gone again.
        """
        if self._pygtfs is not None and not isinstance(self._pygtfs, str):
            self._nothing_said: str | None = None
            return False
        reason = self._pygtfs or "empty"
        if reason != getattr(self, "_nothing_said", None):
            _LOGGER.warning("Datasource %s has no usable schedule (%s), no local stops", file, reason)
            self._nothing_said = reason
        return True
