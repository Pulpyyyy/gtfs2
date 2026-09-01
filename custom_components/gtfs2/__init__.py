"""The GTFS integration."""
from __future__ import annotations

import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady

from datetime import timedelta

from .const import DOMAIN, PLATFORMS, DEFAULT_PATH, DEFAULT_PATH_RT, DEFAULT_REFRESH_INTERVAL
from homeassistant.const import CONF_HOST
from .coordinator import GTFSUpdateCoordinator, GTFSLocalStopUpdateCoordinator
import voluptuous as vol
from .gtfs_helper import get_gtfs, update_gtfs_local_stops, get_route_departures, get_trip_stops
from .gtfs_db import prune_gtfs_datasource, intern_gtfs_datasource
from .gtfs_rt_helper import get_gtfs_rt

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

def _routes_in_use(hass: HomeAssistant, filename: str):
    """Collect the route_ids configured against a datasource.

    Returns (routes, unrestricted). A datasource is unrestricted when at least
    one entry queries it without a route: local stop entries walk every route
    around a position, so their datasource must keep the full feed.
    """
    routes: set[str] = set()
    unrestricted = False
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("file") != filename:
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
    files = {e.data["file"] for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("file")}
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
    # same contract as async_prune_datasources: one name, several, or none
    wanted = data.get("file")
    if isinstance(wanted, str):
        wanted = [wanted] if wanted else []
    files = {e.data["file"] for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("file")}
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
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
     

def setup(hass, config):
    """Setup the service component."""

    def update_gtfs(call):
        """My GTFS Update service."""
        _LOGGER.debug("Updating GTFS with: %s", call.data)
        get_gtfs(hass, DEFAULT_PATH, call.data, True)
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
        DOMAIN, "prune_datasource", prune_datasource,supports_response=SupportsResponse.OPTIONAL)
    hass.services.register(
        DOMAIN, "intern_datasource", intern_datasource,supports_response=SupportsResponse.OPTIONAL)
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    hass.data[DOMAIN][entry.entry_id]['coordinator'].update_interval = timedelta(minutes=1)
    return True