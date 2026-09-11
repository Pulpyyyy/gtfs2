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
    return True

async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    hass.data[DOMAIN][entry.entry_id]['coordinator'].update_interval = timedelta(minutes=1)
    return True