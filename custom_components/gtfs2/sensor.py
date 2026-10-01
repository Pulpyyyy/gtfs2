"""Support for GTFS."""
from datetime import datetime, date
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import homeassistant.util.dt as dt_util

from .const import (
    id_of,
    ATTR_RT_UPDATED_AT,
    ATTR_INFO,
    ATTR_INFO_RT,
    ATTR_OFFSET,
    DEFAULT_NAME,
    DOMAIN,
    ICON,
    ICONS,
    TIME_STR_FORMAT,                    
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    CONF_FILE,
    CONF_RT_ENABLED,
)
from .coordinator import GTFSUpdateCoordinator, GTFSLocalStopUpdateCoordinator
from .rt_source import has_rt_feed, source_device
from .rt_window import window_state
from .feed_window import read_feed_window, timetable_state
from .source_refresh import SIGNAL_SOURCE_REFRESH, source_zip_path
from .departure_attributes import (
    alert_details, departure_times, map_files, next_departure_attributes, next_departure_lists,
    next_service_info, realtime_attributes, route_and_trip_attributes,
    station_attributes, stop_time_attributes,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.exceptions import PlatformNotReady

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    ) -> None:
    """Initialize the setup."""
    if config_entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        # the source's diagnostic entities: whether realtime runs, and why
        # not; and how long the timetable is good for
        timetable = GTFSDatasourceTimetableSensor(hass, config_entry)
        await timetable.async_load_window()
        async_add_entities([GTFSDatasourceRTSensor(config_entry), timetable])
        return
    if config_entry.data.get('device_tracker_id',None):
        sensors = []
        coordinator: GTFSLocalStopUpdateCoordinator = config_entry.runtime_data
        if coordinator.data is None:
            await coordinator.async_config_entry_first_refresh()
        else:
            # a retry after PlatformNotReady: the entry is loaded by now,
            # and Home Assistant refuses a first refresh outside its setup
            # (ConfigEntryError), which left the entry without sensors
            await coordinator.async_refresh()
        if coordinator.data["extracting"]:
            # the stops around the person are known only once the source is
            # unpacked, and nothing created them afterwards: the entry sat
            # empty until reloaded by hand. Home Assistant retries a platform
            # that says it is not ready yet
            raise PlatformNotReady(
                f"Datasource {coordinator.data.get('file')} is still being unpacked")
        for stop in coordinator.data["local_stops_next_departures"]:
            sensors.append(
                    GTFSLocalStopSensor(stop, coordinator, coordinator.data.get("name", "No Name"))
                )
        
    else:
        journey_coordinator: GTFSUpdateCoordinator = config_entry.runtime_data
        # The first refresh reads the departures, and at startup every entry
        # reads them at once: waiting for it held the sensor platform past
        # Home Assistant's ten seconds (IDFM metro lines, 5 to 9 s each). The
        # sensor is added now, empty, and fills in when its first refresh
        # is done; a failed one is retried at the next interval.
        config_entry.async_create_background_task(
            hass, journey_coordinator.async_refresh(), f"gtfs2 first refresh {config_entry.title}")
        
        sensors = [
            GTFSDepartureSensor(journey_coordinator),
        ]

    async_add_entities(sensors, False)


class GTFSDatasourceRTSensor(SensorEntity):
    """The realtime state of one source: active, paused, or off, and why.

    An automatically silenced feed has to be visible, or a pause looks
    exactly like a breakage - the map's stale-feed lesson. The state is what
    the polling window gate last decided; the sensors' coordinators refresh
    it every cycle, this entity only reads it back.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Realtime"
    _attr_icon = "mdi:signal-variant"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._file = entry.data.get(CONF_FILE)
        self._attr_unique_id = f"gtfs2_datasource_rt_{self._file}"
        self._attr_device_info = source_device(self._file)

    @property
    def native_value(self) -> str:
        if not has_rt_feed(self._entry.options):
            return "off"
        if not self._entry.options.get(CONF_RT_ENABLED, True):
            # silenced by its switch: the config is there, nobody reads it
            return "disabled"
        state = window_state(self._file)
        if state is None:
            # no sensor of this source has run its gate yet this session
            return "unknown"
        return "paused" if state.get("paused") else "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = window_state(self._file) or {}
        return {
            "rt_paused": state.get("paused"),
            "window_start": state.get("window_start"),
            "window_end": state.get("window_end"),
            "extended_until": state.get("extended_until"),
            "checked_at": state.get("checked_at"),
        }


class GTFSDatasourceTimetableSensor(SensorEntity):
    """The last day the source's timetable runs, and how it stands today.

    Past that day every sensor of the source shows nothing, and nothing
    says why: the install looks broken when the feed merely ran out. The
    state is the last service day read from the kept zip (feed_window),
    the attributes what the feed says of itself and whether the timetable
    reads valid, ending or expired today. Re-read whenever the source is
    refreshed, since that is when the zip changes.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_name = "Timetable"
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.DATE

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._file = entry.data.get(CONF_FILE)
        self._window: dict[str, str | None] = {}
        self._attr_unique_id = f"gtfs2_datasource_timetable_{self._file}"
        self._attr_device_info = source_device(self._file)

    async def async_load_window(self) -> None:
        """Read the zip; a file, so never on the loop."""
        self._window = await self.hass.async_add_executor_job(
            read_feed_window, source_zip_path(self.hass, self._file)) or {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_SOURCE_REFRESH.format(self._file),
            self._async_source_moved))

    async def _async_source_moved(self) -> None:
        await self.async_load_window()
        self.async_write_ha_state()

    @property
    def native_value(self) -> date | None:
        last = self._window.get("last_service_day")
        try:
            return date.fromisoformat(last) if last else None
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state, days_left = timetable_state(self._window, dt_util.now().date())
        return {
            "timetable": state,
            "days_left": days_left,
            "first_service_day": self._window.get("first_service_day"),
            "feed_start_date": self._window.get("feed_start_date"),
            "feed_end_date": self._window.get("feed_end_date"),
            "feed_version": self._window.get("feed_version"),
            "feed_publisher": self._window.get("feed_publisher_name"),
        }


def _entry_device(name: str) -> DeviceInfo:
    """The device of a journey or local stops entry, named after it."""
    return DeviceInfo(
        name=f"GTFS - {name}",
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, f"GTFS - {name}")},
        manufacturer="GTFS",
        model=name,
    )


class GTFSDepartureSensor(CoordinatorEntity, SensorEntity, RestoreEntity):
    """Implementation of a GTFS departure sensor."""

    # The device already carries the sensor's name: naming the entity too
    # made Home Assistant compose the two into ids like
    # sensor.gtfs_x_y_x_y. The entity takes the device name instead.
    _attr_has_entity_name = True
    _attr_name = None

    # What the recorder does not keep. The lists of the next departures,
    # their realtime and the alert stacks are several kilobytes a sensor,
    # and the two refresh stamps change every minute: together they wrote
    # a new attributes row per sensor and per minute into the database,
    # for a history nobody reads. The state, the next departure and the
    # alert sentences stay recorded; cards read the rest live.
    _unrecorded_attributes = frozenset({
        "next_departures", "next_departures_lines", "next_departures_headsign",
        "next_departures_trips", "next_departures_durations",
        "next_departures_destination_arrival_times", "next_departures_origin_stop_id",
        "next_departures_route_types", "next_departures_realtime",
        "next_delays_realtime", "origin_stop_alerts", "destination_stop_alerts",
        ATTR_INFO, ATTR_INFO_RT,
        "next_departures_realtime_trips", "cancelled_trips_realtime",
        "skipped_trips_realtime",
        ATTR_RT_UPDATED_AT, "gtfs_updated_at",
    })

    def __init__(self, coordinator: GTFSUpdateCoordinator) -> None:
        """Initialize the GTFSsensor."""
        super().__init__(coordinator)
        # the entry knows the name before the first refresh has run
        self._name = coordinator.config_entry.data["name"]
        self._attributes: dict[str, Any] = {}
        # _update_attrs returns early when the source is extracting or broken,
        # before it reaches the line that sets the icon: everything a property
        # serves must already exist here, or adding the entity raises and the
        # sensor never appears at all
        self._icon = ICON
        self._state: datetime | None = None

        self._attr_unique_id = f"gtfs-{self._name}"
        self._attr_device_info = _entry_device(self._name)
        # _update_attrs fills self._attributes in place and returns None on
        # its early paths: assigning its return here replaced the dict with
        # None, and the next update crashed writing into it
        self._update_attrs()
        self._attr_extra_state_attributes = self._attributes

    async def async_added_to_hass(self) -> None:
        """Show the departure known before the restart while the first
        refresh runs, when it is still ahead.

        The sensor is added before its first refresh (see async_setup_entry)
        and would read unknown for those seconds. What Home Assistant kept
        of it is shown instead, attributes included, as long as the
        departure it names has not left yet: a train gone during the
        restart is not shown as the next one. The refresh replaces it all.
        """
        await super().async_added_to_hass()
        if self.coordinator.data is not None:
            return
        last = await self.async_get_last_state()
        when = dt_util.parse_datetime(str(last.state)) if last is not None else None
        if when is None or when <= dt_util.utcnow():
            return
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_native_value = when
        # what Home Assistant adds of its own is not the sensor's to write back
        self._attributes = {k: v for k, v in last.attributes.items()
                            if k not in ("friendly_name", "icon", "device_class", "attribution")}
        self._attr_extra_state_attributes = self._attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()

    @property
    def icon(self) -> str:
        """Icon to use in the frontend, if any."""
        return self._icon

    def _say_once(self, message: str, *args: Any) -> None:
        """Log why the sensor shows nothing, when the reason is new.

        The sensor is updated every minute, and saying the same thing every
        minute buried everything else in the log: an extraction of twenty
        minutes was twenty lines per sensor.
        """
        text = message % args if args else message
        if text != getattr(self, "_nothing_said", None):
            _LOGGER.warning(text)
            self._nothing_said: str | None = text

    def _show_nothing(self, message: str, *args: Any) -> dict[str, Any]:
        """Clear the sensor rather than leave the last departure it showed.

        An early return used to keep the state and the attributes of the
        refresh before: a stop the new timetable renamed, or a datasource
        gone, and the sensor announced its last bus for ever. The agency it
        named goes too: it spoke for a departure no longer shown.
        """
        self._clear({})
        self._say_once(message, *args)
        return self._attributes

    def _clear(self, attributes: dict[str, Any]) -> dict[str, Any]:
        """No departure shown, no agency named, only these attributes."""
        self._attr_native_value = None
        self._attr_attribution = None
        self._attributes = attributes
        self._attr_extra_state_attributes = attributes
        return attributes

    def _update_attrs(self) -> dict[str, Any]:  # noqa: PLR0911
        _LOGGER.debug("SENSOR update attr data: %s", self.coordinator.data)
        self._icon = ICON
        if self.coordinator.data is None:
            # added before its first refresh (see async_setup_entry): no
            # departure known yet, the refresh fills the sensor in
            return self._clear({})
        if self.coordinator.data["extracting"]:
            self._say_once("Extracting datasource: %s ,for sensor: %s", self.coordinator.data["file"], self._name)
            return self._clear({"extracting": True})
        # built afresh at every update: nothing of the last one is left over
        self._attributes = {}

        self._pygtfs = self.coordinator.data["schedule"]
        if self._pygtfs is None or isinstance(self._pygtfs, str):
            # a sentinel of get_gtfs: no zip, no database, a feed all in
            # the future. Nothing to describe, and nothing to query
            return self._show_nothing("Datasource %s has no usable schedule (%s), nothing to show for %s",
                                      self.coordinator.data.get("file"), self._pygtfs or "empty", self._name)
        self.origin = id_of(self.coordinator.data["origin"])
        self.destination = id_of(self.coordinator.data["destination"])
        self._offset = self.coordinator.data["offset"]
        self._departure = self.coordinator.data.get("next_departure",None)
        self._departure_rt = self.coordinator.data.get("next_departure_realtime_attr",None)
        self._route_type = self.coordinator.data["route_type"]
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        # The stops, the trip, the route and its agency, as the coordinator
        # read them in the executor (departure_records): this runs on the
        # event loop, where the four or five SQLite reads it used to make
        # held the whole of Home Assistant up at every update.
        records = self.coordinator.data.get("records") or {}
        self._origin = records.get("origin")
        self._destination = records.get("destination")
        self._trip = records.get("trip")
        self._route = records.get("route")
        self._agency = records.get("agency")
        if self._route_type != "2":
            if not self._origin:
                return self._show_nothing("Origin stop ID %s not found", self.origin)
            if not self._destination:
                return self._show_nothing("Destination stop ID %s not found", self.destination)
        else:
            # a train names its ends by station: the entry's own names stand
            self._origin = self._origin or self.origin
            self._destination = self._destination or self.destination
        # the sensor has something to say again: a later problem is news
        self._nothing_said = None

        # fetch next departures
        self._departure = self.coordinator.data["next_departure"]
        self._next_departures = self._departure.get("next_departures") if self._departure else None

        if self._agency is False:
            _LOGGER.debug(
                (
                    "Agency ID '%s' was not found in agency table, "
                    "you may want to update the routes database table "
                    "to fix this missing reference"
                ),
                getattr(self._route, "agency_id", None),
            )

        # the state is the departure the helper read, in its own zone
        self._state = self._departure.get("departure_time") if self._departure else None
        _LOGGER.debug("Self._departure time from helper: %s", self._state)
        self._attr_native_value = self._state
        self._attr_attribution = self._agency.agency_name if self._agency else None
        self._icon = ICONS.get(self._route.route_type, ICON) if self._route else ICON

        name = (
            f"{getattr(self._agency, 'agency_name', DEFAULT_NAME)} "
            f"{self._origin} to {self._destination} next departure"
        )
        if not self._departure:
            name = f"{DEFAULT_NAME}"
        self._name = self._name or name

        departure_times(self._attributes, self._departure)
        # Add contextual information
        self._attributes[ATTR_OFFSET] = self._offset
        next_service_info(self._attributes, self._state,
                          self.coordinator.data.get("next_service_date"), self._offset)
        station_attributes(self._attributes, self._departure, self._agency,
                           self._origin, self._destination, self._route_type)
        route_and_trip_attributes(self._attributes, self._route, self._trip)
        stop_time_attributes(self._attributes, self._departure)
        next_departure_attributes(self._attributes, self._departure, self._next_departures)
        next_departure_lists(self._attributes, self._departure, self._next_departures)

        # .get: an options change drops the stamp so the next update reads
        # the timetable again, and a refresh failing in between must not
        # take the sensor with it
        self._attributes["gtfs_updated_at"] = self.coordinator.data.get(
            "gtfs_updated_at")

        map_files(self._attributes, self.coordinator.data)

        self._attributes["origin_stop_alert"] = self.coordinator.data[
            "alert"].get("origin_stop_alert", "no info")
        self._attributes["destination_stop_alert"] = self.coordinator.data[
            "alert"].get("destination_stop_alert", "no info")

        alert_details(self._attributes, self.coordinator.data["alert"])
        realtime_attributes(self._attributes, self._departure_rt)

        self._attr_extra_state_attributes = self._attributes
        return self._attr_extra_state_attributes


class GTFSLocalStopSensor(CoordinatorEntity, SensorEntity):
    """Implementation of a GTFS local stops departures sensor."""

    # every line's departures at the stop, past the recorder's 16 kB
    # limit on a busy one, and the refresh stamp that changes each time
    _unrecorded_attributes = frozenset({"next_departures_lines", "gtfs_updated_at"})

    def __init__(self, stop: dict[str, Any], coordinator: GTFSLocalStopUpdateCoordinator, name: str) -> None:
        """Initialize the GTFSsensor."""
        super().__init__(coordinator)
        self._stop = stop
        self._name = self._stop["stop_id"] + "_local_stop_" + self.coordinator.data['device_tracker_id']
        self._attributes: dict[str, Any] = {}

        self._attr_unique_id = self._name
        self._attr_device_info = _entry_device(name)
        self._stop = stop
        # same as the departures sensor: keep the dict when the first update
        # returns early because the source is still extracting
        self._update_attrs()
        self._attr_extra_state_attributes = self._attributes

    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return self._name

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_attrs()
        super()._handle_coordinator_update()

    def _update_attrs(self) -> dict[str, Any]:  # noqa: C901 PLR0911
        _LOGGER.debug("SENSOR: %s, update with attr data: %s", self._name, self.coordinator.data)
        self._departure = self.coordinator.data.get("local_stops_next_departures",None) 
        self._state: str | None = None
        # if no data or extracting, stop
        if self.coordinator.data["extracting"]:  
            # every minute of the unpacking: the flow and its notification say it already
            _LOGGER.debug("Extracting datasource: %s ,for sensor: %s", self.coordinator.data["file"], self._name)
            self._attr_native_value = None
            self._attributes = {"extracting": True}
            self._attr_extra_state_attributes = self._attributes
            return self._attributes
        
        self._attributes = {}
        
        self._state = self._stop["stop_name"] + " (" +  str(dt_util.now().replace(tzinfo=None).strftime(TIME_STR_FORMAT)) + ")"

        self._attr_native_value = self._state        
        # .get: an options change drops the stamp so the next update reads
        # the timetable again, and a refresh failing in between must not
        # take the sensor with it
        self._attributes["gtfs_updated_at"] = self.coordinator.data.get(
            "gtfs_updated_at")  
        self._attributes["device_tracker_id"] = self.coordinator.data[
            "device_tracker_id"]
        self._attributes["offset"] = self.coordinator.data[
            "offset"]
        
        # Add next departures with their lines
        self._attributes["next_departures_lines"] = {}
        if self._departure:
            for stop in self._departure:
                if stop["stop_id"] == self._stop["stop_id"]:
                    self._attributes["next_departures_lines"] = stop["departure"]
                    self._attributes["latitude"] = stop["latitude"]  
                    self._attributes["longitude"] = stop["longitude"]  
                    
        self._attr_extra_state_attributes = self._attributes
        return self._attr_extra_state_attributes
