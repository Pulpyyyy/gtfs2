"""The GTFS integration."""
from __future__ import annotations

import logging
import os
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady

from datetime import timedelta

from .const import DOMAIN, PLATFORMS, DEFAULT_PATH, DEFAULT_PATH_RT, DEFAULT_PATH_GEOJSON, DEFAULT_REFRESH_INTERVAL, CONF_KIND, ENTRY_KIND_DATASOURCE
from homeassistant.const import CONF_HOST
from .coordinator import GTFSUpdateCoordinator, GTFSLocalStopUpdateCoordinator
import voluptuous as vol
from .gtfs_helper import refresh_datasource, update_gtfs_local_stops, get_route_departures, get_trip_stops, route_geojson_name, vehicle_positions_name, async_notify_line_orphaned
from .gtfs_db import prune_gtfs_datasource, intern_gtfs_datasource, real_path, routes_in
from .gtfs_rt_helper import get_gtfs_rt
from .rt_source import async_bootstrap_datasource_entries, async_mirror_rt_to_entries

_LOGGER = logging.getLogger(__name__)

async def async_migrate_entry(hass, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.warning("Migrating from version %s", config_entry.version)
      
    if config_entry.version == 4:

        new_options = {**config_entry.options}
        new_data = {**config_entry.data}
        new_data['route_type'] = '99'
        new_options['offset'] = 0
        new_data.pop('offset')
        new_data['agency'] = '0: ALL'        

        config_entry.version = 9
        hass.config_entries.async_update_entry(config_entry, data=new_data)
        hass.config_entries.async_update_entry(config_entry, options=new_options)          
        
    if config_entry.version == 5:

        new_data = {**config_entry.data}
        new_data['route_type'] = '99'
        new_data['agency'] = '0: ALL'

        config_entry.version = 9
        hass.config_entries.async_update_entry(config_entry, data=new_data)  
        
    if config_entry.version == 6:

        new_data = {**config_entry.data}
        new_data['agency'] = '0: ALL'

        config_entry.version = 9
        hass.config_entries.async_update_entry(config_entry, data=new_data)  

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

        
        config_entry.version = 10
        
        hass.config_entries.async_update_entry(config_entry, data=new_data)  
        hass.config_entries.async_update_entry(config_entry, options=new_options)             

    _LOGGER.warning("Migration to version %s successful", config_entry.version)

    return True

def _routes_in_use(hass: HomeAssistant, filename: str, exclude=None):
    """Collect the route_ids configured against a datasource.

    Returns (routes, unrestricted). A datasource is unrestricted when at least
    one entry queries it without a route: local stop entries walk every route
    around a position, so their datasource must keep the full feed.

    exclude leaves one entry_id out of the count: whether or not an entry
    still lists while its removal hook runs, it must not count as a reader.
    """
    routes: set[str] = set()
    unrestricted = False
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude:
            continue
        if entry.data.get("file") != filename:
            continue
        # the datasource entry names the file but reads nothing from it, and
        # counting it as a reader would make every source look unrestricted
        if entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
            continue
        if entry.data.get("device_tracker_id"):
            unrestricted = True
            continue
        if route := entry.data.get("route"):
            routes.add(route.split(": ")[0])
        else:
            unrestricted = True
    return routes, unrestricted


async def async_prune_datasources(hass: HomeAssistant, data):
    """Prune the picked datasources, or every one, down to the routes in use."""
    dry_run = data.get("dry_run", False)
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    # the field takes one name or several; a lone string still comes in as a
    # string when the service is called from yaml or an old automation
    wanted = data.get("file")
    if isinstance(wanted, str):
        wanted = [wanted] if wanted else []
    # only sources some sensor actually reads: a datasource entry names its
    # file without reading it, and sweeping a source nothing reads would
    # prune it down to nothing
    files = {e.data["file"] for e in hass.config_entries.async_entries(DOMAIN)
             if e.data.get("file") and e.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE}
    if wanted:
        unknown = sorted(set(wanted) - files)
        if unknown:
            _LOGGER.error("No configured entry uses datasource(s): %s", ", ".join(unknown))
        files &= set(wanted)
        if not files:
            return {"pruned": [], "unknown": unknown}

    pruned = []
    for filename in sorted(files):
        routes, unrestricted = _routes_in_use(hass, filename)
        if unrestricted:
            _LOGGER.warning(
                "Skipping datasource %s: an entry uses it without a route (local stops), "
                "pruning would remove data it needs", filename)
            continue
        stats = await hass.async_add_executor_job(
            prune_gtfs_datasource, gtfs_dir, filename, routes, dry_run)
        if stats:
            pruned.append(stats)
    return {"pruned": pruned}


async def async_intern_datasources(hass: HomeAssistant, data):
    """Intern the identifiers of the picked datasources, or every one."""
    dry_run = data.get("dry_run", False)
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    # same contract as async_prune_datasources: one name, several, or none,
    # and the same reader-entries-only sweep
    wanted = data.get("file")
    if isinstance(wanted, str):
        wanted = [wanted] if wanted else []
    files = {e.data["file"] for e in hass.config_entries.async_entries(DOMAIN)
             if e.data.get("file") and e.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE}
    if wanted:
        unknown = sorted(set(wanted) - files)
        if unknown:
            _LOGGER.error("No configured entry uses datasource(s): %s", ", ".join(unknown))
        files &= set(wanted)
        if not files:
            return {"interned": [], "unknown": unknown}

    interned = []
    for filename in sorted(files):
        stats = await hass.async_add_executor_job(
            intern_gtfs_datasource, gtfs_dir, filename, dry_run)
        if stats:
            interned.append(stats)
    return {"interned": interned}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up GTFS from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # every start walks the known sources once, in the background, and gives
    # each its datasource entry; guarded so the entries this creates do not
    # schedule further walks of their own
    if not hass.data[DOMAIN].get("rt_bootstrap_started"):
        hass.data[DOMAIN]["rt_bootstrap_started"] = True
        hass.async_create_background_task(
            async_bootstrap_datasource_entries(hass),
            name="gtfs2 datasource bootstrap",
        )

    if entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        # a datasource entry runs no coordinator: it carries the source's
        # realtime feeds, which the sensors' coordinators resolve each cycle.
        # Every edit is mirrored back onto the journey entries, so a
        # downgrade to upstream falls back on current values rather than the
        # ones frozen at bootstrap. Its one entity is the diagnostic saying
        # whether realtime runs, and why not.
        entry.async_on_unload(entry.add_update_listener(async_mirror_rt_to_entries))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    if entry.data.get('device_tracker_id',None):
        coordinator = GTFSLocalStopUpdateCoordinator(hass, entry)
    else:
        coordinator = GTFSUpdateCoordinator(hass, entry)    

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady
      
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator
    }

    entry.async_on_unload(entry.add_update_listener(update_listener))
      
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
        # only the diagnostic entity to take down; the update listener
        # unloads itself
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

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
        # file and no journey entry, they fall back on their own options
        return
    await _remove_entry_geojson(hass, entry)
    await _notify_orphaned_line(hass, entry)


async def _notify_orphaned_line(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Say when a removed sensor leaves a line nobody reads.

    There is no flow where a line is removed: it happens here, when its last
    sensor is deleted. Nothing is pruned on its own - the timetable may be
    wanted again tomorrow - but silence would leave dead weight nobody knows
    about. So a notification names the line and the service that drops it,
    and the choice stays with the user.
    """
    filename = entry.data.get("file")
    route = (entry.data.get("route") or "").split(": ")[0]
    if not filename or not route or entry.data.get("device_tracker_id"):
        return
    routes, unrestricted = _routes_in_use(hass, filename, exclude=entry.entry_id)
    if unrestricted or route in routes:
        # the line is still read (a return sensor, often), or the datasource
        # must stay whole for a local stops entry
        return
    gtfs_dir = hass.config.path(DEFAULT_PATH)
    loaded = await hass.async_add_executor_job(
        routes_in, real_path(gtfs_dir, filename))
    if route not in loaded:
        # the timetable is already gone, nothing worth saying
        return
    label = (entry.data.get("route") or "").split(": ", 1)[-1]
    await async_notify_line_orphaned(hass, filename, label or route)
     

def setup(hass, config):
    """Setup the service component."""

    def update_gtfs(call):
        """My GTFS Update service.

        Refreshes the datasource through the scratch database: the fresh
        feed is filtered down to the routes actually followed, rebuilt
        beside the live file and swapped in, so the sensors never read a
        half-built database. Falls back to the legacy full extract when
        the datasource has no routes to refresh.
        """
        _LOGGER.debug("Updating GTFS with: %s", call.data)
        refresh_datasource(hass, DEFAULT_PATH, call.data)
        return True

    def update_gtfs_rt_local(call):
        """My GTFS RT service."""
        _LOGGER.debug("Updating GTFS RT with: %s", call.data)
        get_gtfs_rt(hass, DEFAULT_PATH_RT, call.data)
        return True  

    async def update_local_stops(call):
        """My GTFS Update Local Stops service."""
        _LOGGER.debug("Updating GTFS Local Stops with: %s", call.data)
        await update_gtfs_local_stops(hass, call.data)
        return True
    
    async def extract_departures(call):
        """My GTFS Departures service."""
        _LOGGER.debug("Retrieving next departures with: %s", call.data)
        departures = await get_route_departures(hass, call.data)
        return departures
        
    async def extract_trip_stops(call):
        """My GTFS Trip Stops service."""
        _LOGGER.debug("Retrieving trip stops with: %s", call.data)
        stops = await get_trip_stops(hass, call.data)
        return stops

    async def set_include_tomorrow(call: ServiceCall):
        """Flip include_tomorrow on config entries without recreating them.

        The field lives in entry.data and is not exposed by the options
        flow; this service updates it via async_update_entry and reloads
        the affected entries. Without entry_id it applies to every entry
        that carries the field (start/end sensors).
        """
        enabled = bool(call.data.get("enabled", True))
        target = call.data.get("entry_id", None)
        changed = 0
        for entry in hass.config_entries.async_entries(DOMAIN):
            if target and entry.entry_id != target:
                continue
            if "include_tomorrow" not in entry.data:
                continue
            if bool(entry.data.get("include_tomorrow")) == enabled:
                continue
            new_data = dict(entry.data)
            new_data["include_tomorrow"] = enabled
            hass.config_entries.async_update_entry(entry, data=new_data)
            await hass.config_entries.async_reload(entry.entry_id)
            changed += 1
            _LOGGER.info("Set include_tomorrow=%s on entry: %s", enabled, entry.title)
        return {"changed": changed}

    async def prune_datasource(call):
        """My GTFS Prune Datasource service."""
        _LOGGER.debug("Pruning GTFS datasource with: %s", call.data)
        return await async_prune_datasources(hass, call.data)

    async def intern_datasource(call):
        """My GTFS Intern Datasource service."""
        _LOGGER.debug("Interning GTFS datasource with: %s", call.data)
        return await async_intern_datasources(hass, call.data)

    hass.services.register(
        DOMAIN, "update_gtfs", update_gtfs)
    hass.services.register(
        DOMAIN, "update_gtfs_rt_local", update_gtfs_rt_local)     
    hass.services.register(
        DOMAIN, "update_gtfs_local_stops", update_local_stops)
    hass.services.register(
        DOMAIN, "extract_departures", extract_departures,supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "extract_trip_stops", extract_trip_stops,supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "set_include_tomorrow", set_include_tomorrow, supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "prune_datasource", prune_datasource,supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "intern_datasource", intern_datasource,supports_response=SupportsResponse.OPTIONAL)
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    hass.data[DOMAIN][entry.entry_id]['coordinator'].update_interval = timedelta(minutes=1)
    return True


async def _remove_entry_geojson(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the geojson files an entry leaves behind on disk.

    Home Assistant clears the entity registry of a removed entry by itself,
    right after this callback, but nothing knows about the files: the map
    export writes www/gtfs2/<route>_<direction>.json and its _route.json
    companion, and they would stay there for good.

    Both are named after the route and the direction rather than the entry,
    so two entries on the same line share them: only remove them when no
    other entry still needs them.
    """
    route = (entry.data.get("route") or "").split(": ")[0]
    direction = entry.data.get("direction")
    if not route or direction is None:
        return
    still_used = any(
        e.entry_id != entry.entry_id
        and (e.data.get("route") or "").split(": ")[0] == route
        and str(e.data.get("direction")) == str(direction)
        for e in hass.config_entries.async_entries(DOMAIN)
    )
    if still_used:
        _LOGGER.debug("Keeping geojson for route %s direction %s, another entry uses it",
                      route, direction)
        return
    # www/gtfs2, where the export writes them, not the datasource folder
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    names = [vehicle_positions_name(route, direction), route_geojson_name(route, direction)]
    # the files written before the ids were sanitised carry the raw name and
    # nothing else would ever remove them; an id that is not a plain file name
    # never wrote in this directory, so it is not looked for there
    legacy = f"{route}_{direction}"
    if os.path.basename(legacy) == legacy and ".." not in legacy:
        names += [legacy + ".json", legacy + "_route.json"]
    for name in dict.fromkeys(names):
        path = os.path.join(geojson_dir, name)
        if os.path.exists(path):
            try:
                os.remove(path)
                _LOGGER.info("Removed %s", path)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", path, ex)