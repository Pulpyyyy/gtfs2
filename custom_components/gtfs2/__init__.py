"""The GTFS integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from datetime import timedelta

from .const import DOMAIN, PLATFORMS, DATASOURCE_PLATFORMS, DEFAULT_PATH, DEFAULT_PATH_RT, CONF_KIND, ENTRY_KIND_DATASOURCE, CONF_FILE, CONF_URL, CONF_API_KEY, CONF_EXTRACT_FROM, ATTR_API_KEY_LOCATIONS, id_of
from .coordinator import GTFSUpdateCoordinator, GTFSLocalStopUpdateCoordinator
import voluptuous as vol
from .departure_services import get_route_departures, get_route_arrivals, get_trip_stops
from .local_stops import update_gtfs_local_stops
from .notifications import (async_notify_line_orphaned, async_notify_source_unused,
                            clear_line_orphaned, clear_source_unused)
from .exports import remove_entry_geojson
from .datasource_services import async_intern_datasources, async_prune_datasources, async_update_gtfs
from .gtfs_db import real_path, routes_in, route_name_in, get_datasources, close_schedule
from .rt_local import get_gtfs_rt
from .key_mask import hide_keys_in_logs, note_entry_keys, note_key
from .flow_source import SOURCE_URL_SCHEMES, valid_feed_url
from .rt_source import (
    source_readers,
    async_bootstrap_datasource_entries,
    datasource_unique_id,
)
from .source_refresh import (
    SIGNAL_SOURCE_REFRESH,
    source_lock,
    source_zip_path,
    source_zip_url,
    async_adopt_kept_zip,
    async_arm_source_check,
    async_disarm_source_check,
    async_rearm_source_check,
)
from .stations import refresh_rail_index

_LOGGER = logging.getLogger(__name__)

# no api key in the logs, whichever module writes the line
hide_keys_in_logs(__name__, __path__)

def _unique_id_at_1_2(config_entry: ConfigEntry) -> str | None:
    """The unique_id an entry has from minor version 2 on: a datasource
    entry's takes its prefix, the others keep theirs."""
    if config_entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        return datasource_unique_id(config_entry.data[CONF_FILE])
    return config_entry.unique_id


def _data_at_1_3(hass: HomeAssistant, config_entry: ConfigEntry) -> dict:
    """An entry's data from minor version 3 on: the url "na" a source made
    from a zip in the folder held, and the journeys on it, becomes the
    file:// url of that zip, read as any other url from then on. A
    datasource entry drops extract_from: it is fetched from its url."""
    data = {**config_entry.data}
    if data.get(CONF_FILE) and data.get(CONF_URL) in (None, "", "na"):
        data[CONF_URL] = source_zip_url(hass, data[CONF_FILE])
    if data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        data.pop(CONF_EXTRACT_FROM, None)
    return data


def _migrate_minor(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """The minor versions of version 10, in turn."""
    if config_entry.minor_version < 2:
        # a datasource entry's unique_id takes its own prefix: the bare file
        # name could be the gtfs-<name> of a journey. A minor version, so an
        # install going back to upstream still loads the entry
        hass.config_entries.async_update_entry(
            config_entry, unique_id=_unique_id_at_1_2(config_entry), minor_version=2)
    if config_entry.minor_version < 3:
        # every source is fetched from a url, a zip in the folder by its
        # file:// one. A minor version, so an install going back to
        # upstream still loads the entry, whose extract_from tells it the
        # zip is where it lies
        hass.config_entries.async_update_entry(
            config_entry, data=_data_at_1_3(hass, config_entry), minor_version=3)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry.

    Each step hands its version to async_update_entry with the rest: set
    on the entry itself, Home Assistant refuses it since 2024.3, and the
    migration failed before it had written anything.
    """
    _LOGGER.warning("Migrating from version %s", config_entry.version)

    if config_entry.version == 4:

        new_options = {**config_entry.options}
        new_data = {**config_entry.data}
        new_data['route_type'] = '99'
        # an entry that never had an offset in its data has one of 0
        new_options['offset'] = new_data.pop('offset', 0)
        new_data['agency'] = '0: ALL'

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=9)

    if config_entry.version == 5:

        new_data = {**config_entry.data}
        new_data['route_type'] = '99'
        new_data['agency'] = '0: ALL'

        hass.config_entries.async_update_entry(config_entry, data=new_data, version=9)

    if config_entry.version == 6:

        new_data = {**config_entry.data}
        new_data['agency'] = '0: ALL'

        hass.config_entries.async_update_entry(config_entry, data=new_data, version=9)

    if config_entry.version == 7 or config_entry.version == 8 or config_entry.version == 9:

        new_data = {**config_entry.data}
        new_options = {**config_entry.options}
        if config_entry.options.get('api_key', None):
            new_options['api_key_name'] = "Authorization"
            new_options['api_key'] = config_entry.options.get('api_key')
        if config_entry.options.get('x_api_key', None):
            new_options['api_key_name'] = "x_api_key"
            new_options['api_key'] = config_entry.options.get('x_api_key')
        if config_entry.options.get('ocp_apim_subscription_key', None):
            new_options['api_key_name'] = "ocp_apim_subscription_key"
            new_options['api_key'] = config_entry.options.get('ocp_apim_subscription_key')
            new_options.pop('ocp_apim_subscription_key')
        if "x_api_key" in config_entry.options:
            new_options.pop('x_api_key')

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=10)

    if config_entry.version == 10:
        _migrate_minor(hass, config_entry)

    _LOGGER.warning("Migration to version %s successful", config_entry.version)

    return True

async def _bootstrap_sources(hass: HomeAssistant) -> None:
    """Give every source on disk or in an entry its datasource entry."""
    datasources = await get_datasources(hass, DEFAULT_PATH)
    await async_bootstrap_datasource_entries(hass, datasources)


@callback
def _rail_index_again(hass: HomeAssistant, file: str | None) -> None:
    """Read the trains of a source's zip again, in the background, once a
    refresh of it is over. The refresh's signal is sent at its start too,
    the lock still held then; and the zip's stamp says whether there is
    anything to read (refresh_rail_index)."""
    if not file or source_lock(hass, file).locked():
        return
    hass.async_create_background_task(
        hass.async_add_executor_job(refresh_rail_index, source_zip_path(hass, file)),
        f"gtfs2 rail index {file}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GTFS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    note_entry_keys(entry)

    # every start walks the known sources once, in the background, and gives
    # each its datasource entry; guarded so the entries this creates do not
    # schedule further walks of their own
    if not hass.data[DOMAIN].get("rt_bootstrap_started"):
        hass.data[DOMAIN]["rt_bootstrap_started"] = True
        hass.async_create_background_task(
            _bootstrap_sources(hass),
            name="gtfs2 datasource bootstrap",
        )

    if entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        # a datasource entry runs no coordinator: it carries the source's
        # realtime feeds, which the sensors' coordinators resolve each cycle.
        # Its entities are the diagnostic saying whether realtime runs, and
        # why not, and the switch that silences it without losing the config.
        # the scheduled look at the source's host, armed per the entry's
        # options and re-armed when they change
        async_arm_source_check(hass, entry)
        entry.async_on_unload(entry.add_update_listener(async_rearm_source_check))
        entry.async_on_unload(lambda: async_disarm_source_check(hass, entry))
        # the trains a flow read from the source's zip are read again once a
        # new edition is in, in the background (stations.refresh_rail_index)
        file = entry.data.get(CONF_FILE)
        entry.async_on_unload(async_dispatcher_connect(
            hass, SIGNAL_SOURCE_REFRESH.format(file), lambda: _rail_index_again(hass, file)))
        # a source the stock integration built kept no record of its download
        if file:
            await async_adopt_kept_zip(hass, file)
        await hass.config_entries.async_forward_entry_setups(entry, DATASOURCE_PLATFORMS)
        return True

    if entry.data.get('device_tracker_id',None):
        coordinator = GTFSLocalStopUpdateCoordinator(hass, entry)
    else:
        coordinator = GTFSUpdateCoordinator(hass, entry)

    # the entry's own, on the entry: hass.data[DOMAIN] is the store the
    # sources share, their locks, checks and flags
    entry.runtime_data = coordinator
    # a sensor on a line said to be read by nobody: it is read again, and
    # so is its source
    clear_line_orphaned(hass, entry.data.get(CONF_FILE), id_of(entry.data.get("route")))
    clear_source_unused(hass, entry.data.get(CONF_FILE))

    entry.async_on_unload(entry.add_update_listener(update_listener))
      
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        # only the diagnostic and the switch to take down; the update
        # listener unloads itself
        return await hass.config_entries.async_unload_platforms(entry, DATASOURCE_PLATFORMS)
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # the schedule it held open: every reload used to leave one behind
        await hass.async_add_executor_job(
            close_schedule, getattr(entry.runtime_data, "_pygtfs", None))

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up after a removed entry, then say what its removal orphaned.

    Two lots each brought a hook of this name - the geojson cleanup of
    sanitize-geojson-filenames and the orphaned line notification of
    prune-on-remove - and Python keeps only the last definition, so one of
    them silently never ran. This is their combination, and the place to
    extend when another lot needs the removal moment.
    """
    if entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        # the datasource entry only carries config: removing it deletes no
        # file and no journey entry, they fall back on their own options.
        # Only the word that the source served nobody goes with it
        clear_source_unused(hass, entry.data.get(CONF_FILE))
        return
    await remove_entry_geojson(hass, entry)
    await _notify_orphaned_line(hass, entry)


async def _notify_orphaned_line(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Say when a removed sensor leaves a line nobody reads.

    There is no flow where a line is removed: it happens here, when its last
    sensor is deleted. Nothing is pruned on its own - the timetable may be
    wanted again tomorrow - but silence would leave dead weight nobody knows
    about. So a repairs issue names the line, and its fix drops it when the
    user asks: the choice stays with them.
    """
    filename = entry.data.get("file")
    route = id_of(entry.data.get("route"))
    if not filename or not route or entry.data.get("device_tracker_id"):
        return
    routes, unrestricted = source_readers(hass, filename, exclude=entry.entry_id)
    if unrestricted or route in routes:
        # the line is still read (a return sensor, often), or the datasource
        # must stay whole for a local stops entry
        return
    database = real_path(hass.config.path(DEFAULT_PATH), filename)
    loaded = await hass.async_add_executor_job(routes_in, database)
    if not loaded or route not in loaded:
        # the timetable is already gone, or the database would not say:
        # either way there is nothing worth saying
        return
    # the entry keeps the line's id alone (ORLEANS:Line:12): the issue
    # names it as riders do, by the name the database lists for it; an
    # older entry wrote its label after the id
    label = await hass.async_add_executor_job(
        route_name_in, database, route)
    label = label or (entry.data.get("route") or "").split(": ", 1)[-1]
    if set(loaded) == {route}:
        # the source's last line: it is not dropped alone, the source is
        # what to remove (async_prune_line answers last_line)
        await async_notify_source_unused(hass, filename, label or route)
        return
    await async_notify_line_orphaned(hass, filename, route, label or route)
     

# the key of a feed, static or realtime, and where it travels
_KEY_FIELDS: dict[vol.Marker, Any] = {
    vol.Optional(CONF_API_KEY): cv.string,
    vol.Optional("api_key_name"): cv.string,
    vol.Optional("api_key_location"): vol.In(ATTR_API_KEY_LOCATIONS),
}
def _without_legacy_none(data: dict) -> dict:
    """update_gtfs once defaulted its address to "na", meant as none, and
    an automation written then still sends it, for the key too: read as
    not given, and said, so the call can be mended. extract_from the same:
    where the feed is read from is its url's to say, http(s) or file
    alike. Said at WARNING: Home Assistant's cv.removed said it at ERROR,
    every call, for an automation written against an older version."""
    legacy = [key for key in (CONF_URL, CONF_API_KEY) if data.get(key) == "na"]
    if legacy:
        _LOGGER.warning("update_gtfs: %s given as \"na\", read as not given; "
                        "leave the field out instead", " and ".join(legacy))
    if CONF_EXTRACT_FROM in data:
        _LOGGER.warning("update_gtfs: extract_from is no longer read, the url says "
                        "where the feed comes from; leave the field out")
        legacy.append(CONF_EXTRACT_FROM)
    return {key: value for key, value in data.items() if key not in legacy}


def _source_url(value: str) -> str:
    """An address a source can be fetched from, as the source screen takes it."""
    if not valid_feed_url(value):
        raise vol.Invalid("not a valid address (" + ", ".join(SOURCE_URL_SCHEMES) + ")")
    return value


# the fields services.yaml lists, checked before a handler reads them: a
# missing one used to surface as a KeyError from deep inside. Extra keys
# still pass, for the automations written against older field lists
_UPDATE_GTFS_SCHEMA = vol.All(
    _without_legacy_none,
    vol.Schema({
        vol.Required("file"): cv.string,
        vol.Optional(CONF_URL): vol.All(cv.string, _source_url),
        **_KEY_FIELDS,
        vol.Optional("clean_feed_info"): cv.boolean,
        vol.Optional("check_source_dates"): cv.boolean,
    }, extra=vol.ALLOW_EXTRA),
)
_UPDATE_GTFS_RT_SCHEMA = vol.Schema({
    vol.Required("file"): cv.string,
    vol.Required(CONF_URL): vol.All(cv.string, _source_url),
    vol.Required("rt_type"): vol.In(["trip_data", "vehicle_positions", "alerts"]),
    **_KEY_FIELDS,
    vol.Optional("accept"): cv.boolean,
    vol.Optional("entity_for_siri"): cv.entity_id,
    vol.Optional("debug_output"): cv.boolean,
}, extra=vol.ALLOW_EXTRA)
_ENTITY_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id},
                            extra=vol.ALLOW_EXTRA)
_EXTRACT_DEPARTURES_SCHEMA = vol.Schema({
    vol.Required("config_entry"): cv.string,
    # the time selector sends 08:15:00, a yaml call often 08:15: both are
    # read as a time and handed on in the one form the handler parses
    vol.Optional("from_time"): vol.All(cv.time, lambda t: t.strftime("%H:%M:%S")),
}, extra=vol.ALLOW_EXTRA)
_DATASOURCES_SCHEMA = vol.Schema({
    vol.Optional("file"): vol.Any(None, cv.string, [cv.string]),
    vol.Optional("dry_run"): cv.boolean,
}, extra=vol.ALLOW_EXTRA)


def setup(hass: HomeAssistant, config: dict) -> bool:
    """Setup the service component."""

    async def update_gtfs(call: ServiceCall) -> bool:
        """My GTFS Update service."""
        return await async_update_gtfs(hass, call.data)

    def update_gtfs_rt_local(call: ServiceCall) -> bool:
        """My GTFS RT service."""
        note_key(call.data.get(CONF_API_KEY))
        _LOGGER.debug("Updating GTFS RT with: %s", call.data)
        get_gtfs_rt(hass, DEFAULT_PATH_RT, call.data)
        return True  

    async def update_local_stops(call: ServiceCall) -> bool:
        """My GTFS Update Local Stops service."""
        _LOGGER.debug("Updating GTFS Local Stops with: %s", call.data)
        await update_gtfs_local_stops(hass, call.data)
        return True
    
    async def extract_departures(call: ServiceCall) -> dict[str, Any]:
        """My GTFS Departures service."""
        _LOGGER.debug("Retrieving next departures with: %s", call.data)
        departures = await get_route_departures(hass, call.data)
        return departures

    async def extract_arrivals(call: ServiceCall) -> dict[str, Any]:
        """My GTFS Arrivals service."""
        _LOGGER.debug("Retrieving arrivals with: %s", call.data)
        return await get_route_arrivals(hass, call.data)
        
    async def extract_trip_stops(call: ServiceCall) -> dict[str, Any]:
        """My GTFS Trip Stops service."""
        _LOGGER.debug("Retrieving trip stops with: %s", call.data)
        stops = await get_trip_stops(hass, call.data)
        return stops       

    async def prune_datasource(call: ServiceCall) -> dict[str, list]:
        """My GTFS Prune Datasource service."""
        _LOGGER.debug("Pruning GTFS datasource with: %s", call.data)
        return await async_prune_datasources(hass, call.data)

    async def intern_datasource(call: ServiceCall) -> dict[str, list]:
        """My GTFS Intern Datasource service."""
        _LOGGER.debug("Interning GTFS datasource with: %s", call.data)
        return await async_intern_datasources(hass, call.data)

    hass.services.register(
        DOMAIN, "update_gtfs", update_gtfs, schema=_UPDATE_GTFS_SCHEMA)
    hass.services.register(
        DOMAIN, "update_gtfs_rt_local", update_gtfs_rt_local, schema=_UPDATE_GTFS_RT_SCHEMA)
    hass.services.register(
        DOMAIN, "update_gtfs_local_stops", update_local_stops, schema=_ENTITY_SCHEMA)
    hass.services.register(
        DOMAIN, "extract_departures", extract_departures, schema=_EXTRACT_DEPARTURES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "extract_arrivals", extract_arrivals, schema=_EXTRACT_DEPARTURES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "extract_trip_stops", extract_trip_stops, schema=_ENTITY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "prune_datasource", prune_datasource, schema=_DATASOURCES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "intern_datasource", intern_datasource, schema=_DATASOURCES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle options update.

    Every coordinator runs every minute: the journey one reads its static
    refresh_interval inside the update, and the local stops one its
    local_stop_refresh_interval, taking out the departures gone in between
    without walking the stops around a person again.
    """
    coordinator = entry.runtime_data
    coordinator.update_interval = timedelta(minutes=1)
    # the options just changed, an offset or an interval: the answer the old
    # ones gave was served until the next static refresh. Dropping its stamp
    # has the next update read the timetable again, and it runs now
    if coordinator.data:
        coordinator.data.pop("gtfs_updated_at", None)
    await coordinator.async_request_refresh()
    return True
