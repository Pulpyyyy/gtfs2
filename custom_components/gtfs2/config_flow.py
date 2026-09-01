"""ConfigFlow for GTFS integration."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant import data_entry_flow
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    DEFAULT_PATH, 
    DOMAIN, 
    DEFAULT_API_KEY_LOCATION,
    DEFAULT_REFRESH_INTERVAL, 
    DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_RADIUS,
    DEFAULT_OFFSET,
    CONF_API_KEY_LOCATION, 
    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_ACCEPT_HEADER_PB,
    DEFAULT_ACCEPT_HEADER_PB,
    DEFAULT_API_KEY_NAME,
    CONF_VEHICLE_POSITION_URL, 
    CONF_TRIP_UPDATE_URL,
    CONF_ALERTS_URL,
    CONF_URL,
    CONF_EXTRACT_FROM,
    CONF_FILE,
    CONF_DEVICE_TRACKER_ID,
    CONF_AGENCY,
    CONF_ROUTE_TYPE,
    CONF_ROUTE,
    CONF_DIRECTION,
    CONF_ORIGIN,
    CONF_DESTINATION,
    CONF_NAME,
    CONF_INCLUDE_TOMORROW,
    CONF_LOCAL_STOP_REFRESH_INTERVAL,
    CONF_RADIUS,
    CONF_TIMERANGE,
    CONF_REFRESH_INTERVAL,
    CONF_OFFSET,
    CONF_REAL_TIME,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    CONF_RT_ENABLED,
    ATTR_API_KEY_LOCATIONS,
    DEFAULT_MAX_LOCAL_STOPS,
    CONF_MAX_LOCAL_STOPS
)

from .gtfs_helper import (
    get_gtfs,
    get_next_departure,
    get_route_list,
    get_stop_list,
    get_station_list,
    has_trip_between,
    has_train_trip_between,
    get_direction_labels,
    get_datasources,
    get_zipfiles,
    check_extracting,
    check_extraction_result,
    remove_datasource,
    check_datasource_index,
    get_agency_list,
    get_local_stop_list,
    get_route_labels,
    get_route_labels_from_zip,
    get_routes_in_zip,
    get_route_options_from_zip,
    get_agencies_in_zip,
    routes_in_zip_for_agency,
    ensure_source_zip,
    open_datasource,
    async_watch_extraction,
    async_notify_import,
    build_scratch_database
)

from .gtfs_db import import_routes, routes_in, real_path, optimise_datasource
from .rt_source import RT_OPTION_KEYS, async_ensure_datasource_entry, datasource_entry

_LOGGER = logging.getLogger(__name__)

# only used to branch inside the flow, never written to the config entry
CONF_NEEDS_API_KEY = "needs_api_key"
CONF_ADD_RETURN = "add_return"
# other pruned lines to bring back in the same import, never saved
CONF_ALSO_RELOAD = "also_reload"

def _stop_id(entry):
    """The stop_id of a "stop_id: Name (sequence)" entry."""
    return entry.rsplit(": ", 1)[0].strip()


def _stop_name(entry):
    """The readable part of a "stop_id: Name (sequence)" entry.

    Ids carry colons of their own, so cut from the right.
    """
    return entry.rsplit(": ", 1)[-1].rsplit(" (", 1)[0].strip()


def _base_name(entry):
    """_stop_name without the flow's own " #n" disambiguation suffix.

    The suffixed name is what the pickers and the by-name matching need;
    a sensor name is for reading, so the suffix goes.
    """
    return re.sub(r" #\d+$", "", _stop_name(entry))


def _database_size(gtfs_dir, filename):
    """How big the datasource is right now, as a readable string.

    An extraction only ever adds rows, so the file grows steadily and its size
    is the one honest sign of progress available from outside the fork. There
    is no total to compare against, since it depends on the network.
    """
    for suffix in (".import.sqlite", ".sqlite"):
        path = os.path.join(gtfs_dir, filename + suffix)
        if os.path.exists(path):
            try:
                return f"{os.path.getsize(path) / 1048576:.0f} MB"
            except OSError:
                break
    return "0 MB"


def _scratch_size(gtfs_dir, filename):
    """How big the import database has grown, as a readable string.

    Only the scratch file: the real datasource is not being written during an
    import, so reporting its size would show a figure that never moves.
    """
    path = os.path.join(gtfs_dir, filename + ".import.sqlite")
    try:
        return f"{os.path.getsize(path) / 1048576:.0f} MB"
    except OSError:
        return "0 MB"


def _source_rt_schema(opts):
    """The realtime feeds screen of a source, prefilled with what it has.

    Shared between the creation flow and the datasource entry's options so
    the two places a source is edited can never drift apart. Every field is
    optional: emptied means removed.
    """
    return {
        vol.Optional(CONF_TRIP_UPDATE_URL, default=opts.get(CONF_TRIP_UPDATE_URL, "")): str,
        vol.Optional(CONF_VEHICLE_POSITION_URL, default=opts.get(CONF_VEHICLE_POSITION_URL, "")): str,
        vol.Optional(CONF_ALERTS_URL, default=opts.get(CONF_ALERTS_URL, "")): str,
        # the three key fields only matter for the few feeds that need one
        vol.Optional(CONF_NEEDS_API_KEY, default=bool(opts.get(CONF_API_KEY))): selector.BooleanSelector(),
    }


def _source_rt_key_schema(opts):
    """The realtime api key screen, shown only when the source needs one."""
    return {
        vol.Required(CONF_API_KEY, default=opts.get(CONF_API_KEY, "")): cv.string,
        vol.Required(
            CONF_API_KEY_NAME,
            default=opts.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME),
        ): cv.string,
        vol.Required(
            CONF_API_KEY_LOCATION,
            default=opts.get(CONF_API_KEY_LOCATION, "query_string"),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[l for l in ATTR_API_KEY_LOCATIONS if l != "not_applicable"],
                translation_key="api_key_location",
            )
        ),
        # gtfs_rt_helper only sends this when the key is in a header
        vol.Optional(
            CONF_ACCEPT_HEADER_PB,
            default=opts.get(CONF_ACCEPT_HEADER_PB, False),
        ): selector.BooleanSelector(),
    }


def _collect_source_rt_options(url_fields, key_fields, previous=None):
    """The options a datasource entry stores: what was typed, nothing empty.

    An emptied field means removal, so blanks and stray spaces never make it
    into the options - the coordinators and the mirror both read absence as
    "this source does not have that feed". Without a key, none of the key
    fields survive either. The rt_enabled switch is not on these screens,
    so its position rides through an edit untouched.
    """
    options = {}
    for key in (CONF_TRIP_UPDATE_URL, CONF_VEHICLE_POSITION_URL, CONF_ALERTS_URL):
        value = (url_fields.get(key) or "").strip()
        if value:
            options[key] = value
    if (key_fields.get(CONF_API_KEY) or "").strip():
        options[CONF_API_KEY] = key_fields[CONF_API_KEY].strip()
        options[CONF_API_KEY_NAME] = key_fields.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME)
        options[CONF_API_KEY_LOCATION] = key_fields.get(CONF_API_KEY_LOCATION, "query_string")
        options[CONF_ACCEPT_HEADER_PB] = bool(key_fields.get(CONF_ACCEPT_HEADER_PB, False))
    if previous is not None and CONF_RT_ENABLED in previous:
        options[CONF_RT_ENABLED] = previous[CONF_RT_ENABLED]
    return options


TRANSLATION_DESCRIPTION_PLACEHOLDERS = {
    "docu_extracting": "https://github.com/vingerha/gtfs2/wiki/01:-Initial-Setup-of-the-Static-GTFS-Data-Source#extraction-of-data-from-the-datasource",
    "docu_menu_options": "https://github.com/vingerha/gtfs2/wiki/00:-Installation-and-Main-Menu",
    "docu_select_source": "https://github.com/vingerha/gtfs2/wiki/01:-Initial-Setup-of-the-Static-GTFS-Data-Source",
    "docu_local_stops": "https://github.com/vingerha/gtfs2/wiki/03:-Adding-a-location%E2%80%90based-dynamic-departures-sensor",
    "docu_new_route": "https://github.com/vingerha/gtfs2/wiki/02:-Adding-a-route",
    "docu_setup_train": "https://github.com/vingerha/gtfs2/wiki/02b:-Adding-a-route-(using-city-method)",
    "docu_configuring_options": "https://github.com/vingerha/gtfs2/wiki/04:-Configuring-a-route's-options-(inc.-adding-real%E2%80%90time)",
    "model": "Example model",
}

@config_entries.HANDLERS.register(DOMAIN)
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GTFS."""

    VERSION = 10

    def __init__(self) -> None:
        """Init ConfigFlow."""
        self._pygtfs = ""
        self._data: dict[str, str] = {}
        self._user_inputs: dict = {}
        self._pending_error: str | None = None
        self._extract_job = None
        self._extract_task = None
        self._extract_next_step: str | None = None
        self._route_label: str = ""
        # how big the database has grown, shown while it is being built
        self._extract_size: str = "0 MB"
        # the import running behind the progress screen, and its routes
        self._import_job = None
        self._import_task = None
        self._import_routes: list = []
        # what the last created entry was called, shown on the closing screen
        self._created_name: str = ""
        # the mirror journey, worked out once the stops are known
        self._return_trip: dict | None = None
        self._return_name: str = ""
        # what the realtime screen collected, while its key screen runs
        self._source_rt_inputs: dict = {}

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        errors: dict[str, str] = {}

        # with no datasource yet, only the first entry can lead anywhere,
        # so say it rather than describing the general case
        datasources = await get_datasources(self.hass, DEFAULT_PATH)
        placeholders = dict(TRANSLATION_DESCRIPTION_PLACEHOLDERS)
        if not datasources:
            # nothing to build a sensor on yet: only the first entry leads
            # anywhere, so use the wording that says so
            return self.async_show_menu(
                step_id="user_empty",
                menu_options=["source"],
                description_placeholders=placeholders,
            )


        return self.async_show_menu(
            step_id="user",
            # ordered by lifecycle: a datasource must exist before a sensor
            # can read from it, and is removed last
            menu_options=["source", "start_end", "local_stops", "remove"],
            description_placeholders=placeholders,
        )

    async def async_step_user_empty(self, user_input: dict | None = None) -> FlowResult:
        """The first-run menu, when no datasource exists yet.

        A step rendered under its own step_id needs a method of that name:
        Home Assistant looks one up as soon as the user picks an entry, and
        refuses the whole flow with "doesn't support step user_empty" without
        it. Reached again when the last datasource goes away, which is how it
        surfaced: renaming the zip left the entries pointing at nothing.
        """
        return await self.async_step_user(user_input)
                   
    async def async_step_start_end(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        errors: dict[str, str] = {}
        if user_input is None:
            # reached again from the closing screen: the previous journey must
            # not leak into this one
            if self._created_name:
                self._reset_for_next_journey()
            if self._pending_error:
                errors["base"] = self._pending_error
                self._pending_error = None
            datasources = await get_datasources(self.hass, DEFAULT_PATH)
            return self.async_show_form(
                step_id="start_end",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_FILE, default=self._user_inputs.get(CONF_FILE, "")): vol.In(datasources),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,                
                errors=errors,
            )

        user_input[CONF_URL] = "na"
        user_input[CONF_EXTRACT_FROM] = "zip"
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Start End: {self._user_inputs}")
        return await self.async_step_agency()            
            
    async def async_step_local_stops(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        # local stops create the entry directly, they do not go on to pick a route
        self._extract_next_step = "local_stops"
        errors: dict[str, str] = {}       

        async def _show(errors, previous=None):
            """Render the form, keeping what the user already typed."""
            previous = previous or {}
            datasources = await get_datasources(self.hass, DEFAULT_PATH)
            return self.async_show_form(
                step_id="local_stops",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_FILE, default=previous.get(CONF_FILE, "")): vol.In(datasources),
                        vol.Required(CONF_DEVICE_TRACKER_ID): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain=["person","zone"]),                          
                        ),
                        vol.Required(CONF_NAME, default=previous.get(CONF_NAME, "")): str, 
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            if self._pending_error:
                errors["base"] = self._pending_error
                self._pending_error = None
            return await _show(errors)
        user_input[CONF_URL] = "na"
        user_input[CONF_EXTRACT_FROM] = "zip"    
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Local Stops: {self._user_inputs}") 
        check_data = await self._check_data(self._user_inputs)
        if check_data :
            # "extracting" is not a user error: the datasource is being unpacked,
            # there is nothing to correct, so it keeps its own abort message.
            if check_data == "extracting":
                self._user_inputs.update(user_input)
                return await self.async_step_extracting()
            errors["base"] = check_data
            return await _show(errors, user_input)
        else:
            return self.async_create_entry(
                title=user_input[CONF_NAME], data=self._user_inputs
                )                
                   
    async def async_step_source(self, user_input: dict | None = None) -> FlowResult:
        """Ask where the data comes from, then branch to the matching step."""
        if user_input is None:
            return self.async_show_menu(
                step_id="source",
                menu_options=["source_url", "source_zip"],
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
            )
        return await self.async_step_source_url()

    async def async_step_source_url(self, user_input: dict | None = None) -> FlowResult:
        """Download the feed from a url."""
        errors: dict[str, str] = {}

        def _show(errors, previous=None):
            previous = previous or {}
            return self.async_show_form(
                step_id="source_url",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_URL, default=previous.get(CONF_URL, "")): str,
                        vol.Required(CONF_FILE, default=previous.get(CONF_FILE, "")): str,
                        # the three key fields only matter for the few sources that
                        # need one, so they live behind this toggle
                        vol.Optional(CONF_NEEDS_API_KEY, default=False): selector.BooleanSelector(),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            if self._pending_error:
                errors["base"] = self._pending_error
                self._pending_error = None
            return _show(errors)
        user_input[CONF_EXTRACT_FROM] = "url"
        if user_input.pop(CONF_NEEDS_API_KEY, False):
            self._user_inputs.update(user_input)
            return await self.async_step_source_key()
        user_input[CONF_API_KEY_LOCATION] = DEFAULT_API_KEY_LOCATION
        # only the zip is fetched here: importing waits until the lines are
        # chosen, so a national feed no longer means unpacking the whole
        # network before the first screen that asks what to keep
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, user_input)
        if check_data:
            # "extracting" is not a user error: the datasource is being unpacked,
            # there is nothing to correct, so it keeps its own abort message.
            if check_data == "extracting":
                self._user_inputs.update(user_input)
                self._ensure_datasource_entry()
                return await self.async_step_unpacking()
            errors["base"] = check_data
            return _show(errors, user_input)
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Source url: {self._user_inputs}")
        return await self.async_step_source_rt()

    async def async_step_unpacking(self, user_input: dict | None = None) -> FlowResult:
        """A brand new source is unpacking: end here, and notify when it is done.

        Building a datasource from scratch takes minutes, sometimes more than
        ten on a large network. Holding the flow open for that is a poor trade:
        nothing further can be chosen until it finishes, and a window left open
        that long is closed anyway. So the flow ends now and the notification
        carries the news.

        This is what separates it from async_step_extracting, which is worth
        waiting on: there, the unpacking is usually already done and the flow
        continues immediately.
        """
        file = self._user_inputs.get(CONF_FILE, "")
        self.hass.async_create_background_task(
            async_watch_extraction(self.hass, file),
            name=f"gtfs2 watch extraction {file}",
        )
        return self.async_abort(
            reason="unpacking",
            description_placeholders={
                **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                "file": file,
            },
        )

    async def async_step_source_key(self, user_input: dict | None = None) -> FlowResult:
        """Ask for the api key, only when the source needs one."""
        errors: dict[str, str] = {}

        def _show(errors, previous=None):
            previous = previous or {}
            return self.async_show_form(
                step_id="source_key",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_API_KEY, default=previous.get(CONF_API_KEY, "")): str,
                        vol.Required(
                            CONF_API_KEY_NAME,
                            default=previous.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME),
                        ): str,
                        vol.Required(
                            CONF_API_KEY_LOCATION,
                            default=previous.get(CONF_API_KEY_LOCATION, "query_string"),
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[l for l in ATTR_API_KEY_LOCATIONS if l != "not_applicable"],
                                translation_key="api_key_location",
                            )
                        ),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            return _show(errors)
        self._user_inputs.update(user_input)
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, self._user_inputs)
        if check_data:
            if check_data == "extracting":
                self._ensure_datasource_entry()
                return await self.async_step_extracting()
            errors["base"] = check_data
            return _show(errors, user_input)
        _LOGGER.debug(f"UserInputs Source key: {self._user_inputs}")
        return await self.async_step_source_rt()

    async def async_step_source_rt(self, user_input: dict | None = None) -> FlowResult:
        """Offer the source's realtime feeds right after it is brought in.

        Optional by design: submitting the screen empty just moves on, and
        the feeds can be added or changed later from the datasource entry's
        CONFIGURE button. A source picked again shows what it already has.
        """
        errors: dict[str, str] = {}
        source = datasource_entry(self.hass, self._user_inputs.get(CONF_FILE))
        opts = source.options if source else {}
        if user_input is None:
            return self.async_show_form(
                step_id="source_rt",
                data_schema=vol.Schema(_source_rt_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        if user_input.pop(CONF_NEEDS_API_KEY, False):
            self._source_rt_inputs = user_input
            return await self.async_step_source_rt_key()
        await self._store_source_rt(user_input, {})
        return await self.async_step_agency()

    async def async_step_source_rt_key(self, user_input: dict | None = None) -> FlowResult:
        """Ask for the realtime api key, only when the source needs one."""
        errors: dict[str, str] = {}
        source = datasource_entry(self.hass, self._user_inputs.get(CONF_FILE))
        opts = source.options if source else {}
        if user_input is None:
            return self.async_show_form(
                step_id="source_rt_key",
                data_schema=vol.Schema(_source_rt_key_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        await self._store_source_rt(self._source_rt_inputs, user_input)
        return await self.async_step_agency()

    async def _store_source_rt(self, url_fields, key_fields):
        """Put what the realtime screens collected onto the datasource entry."""
        inputs = self._user_inputs
        await async_ensure_datasource_entry(
            self.hass, inputs.get(CONF_FILE),
            url=inputs.get(CONF_URL) or "na",
            extract_from=inputs.get(CONF_EXTRACT_FROM) or "zip")
        source = datasource_entry(self.hass, inputs.get(CONF_FILE))
        if source is None:
            _LOGGER.error("No datasource entry to store the realtime config on: %s",
                          inputs.get(CONF_FILE))
            return
        self.hass.config_entries.async_update_entry(
            source, options=_collect_source_rt_options(
                url_fields, key_fields, previous=source.options))

    async def async_step_source_zip(self, user_input: dict | None = None) -> FlowResult:
        """Use a zip the user already dropped in the gtfs2 folder."""
        errors: dict[str, str] = {}

        async def _show(errors):
            zipfiles = await get_zipfiles(self.hass, DEFAULT_PATH)
            if not zipfiles:
                return self.async_abort(
                    reason="no_zip_in_folder",
                    description_placeholders={
                        **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                        "folder": self.hass.config.path(DEFAULT_PATH),
                    },
                )
            return self.async_show_form(
                step_id="source_zip",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_FILE): vol.In(zipfiles),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            if self._pending_error:
                errors["base"] = self._pending_error
                self._pending_error = None
            return await _show(errors)
        # the url is unused here, but get_gtfs still reads the key
        user_input[CONF_EXTRACT_FROM] = "zip"
        user_input[CONF_URL] = "na"
        check_data = await self.hass.async_add_executor_job(
            ensure_source_zip, self.hass, DEFAULT_PATH, user_input)
        if check_data:
            if check_data == "extracting":
                self._user_inputs.update(user_input)
                self._ensure_datasource_entry()
                return await self.async_step_extracting()
            errors["base"] = check_data
            return await _show(errors)
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Source zip: {self._user_inputs}")
        return await self.async_step_source_rt()

    def _ensure_datasource_entry(self):
        """Give the source picked in this flow its datasource entry.

        Scheduled, not awaited: the entry is bookkeeping this flow should
        neither wait on nor fail over, and the creation aborts on its
        unique_id when the entry already exists.
        """
        inputs = self._user_inputs
        self.hass.async_create_background_task(
            async_ensure_datasource_entry(
                self.hass, inputs.get(CONF_FILE),
                url=inputs.get(CONF_URL) or "na",
                extract_from=inputs.get(CONF_EXTRACT_FROM) or "zip"),
            name=f"gtfs2 datasource entry {inputs.get(CONF_FILE)}",
        )

    async def async_step_remove(self, user_input: dict | None = None) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is None:
            datasources = await get_datasources(self.hass, DEFAULT_PATH)
            return self.async_show_form(
                step_id="remove",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_FILE, default=""): vol.In(datasources),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        try:
            removed = remove_datasource(self.hass, DEFAULT_PATH, user_input[CONF_FILE], True)
            _LOGGER.debug(f"Removed gtfs data source: {removed}")
        except Exception as ex:
            _LOGGER.error("Error while deleting : %s", {ex})
            return self.async_abort(reason="generic_failure")
        # the datasource entry follows its files out; the journey entries
        # stay, as they always have, and fail on the missing database
        source = datasource_entry(self.hass, user_input[CONF_FILE])
        if source is not None:
            await self.hass.config_entries.async_remove(source.entry_id)
        return self.async_abort(reason="files_deleted")
        
    async def _fresh_source(self):
        """Whether the source picked in this flow has no database yet.

        A fresh source is served from its zip: operators and lines are read
        from the feed, the user chooses, and only the chosen lines are ever
        imported. The full network never has to be unpacked, which on a
        national feed is what makes the difference between a flow that
        continues and one that parks the user behind a progress screen.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        return not await self.hass.async_add_executor_job(
            os.path.exists, real_path(gtfs_dir, self._user_inputs[CONF_FILE]))

    async def async_step_agency(self, user_input: dict | None = None) -> FlowResult:
        """Handle the agency."""
        errors: dict[str, str] = {}
        if await self._fresh_source():
            # no database yet: the feed is the only thing there is to read,
            # and nothing starts importing before the lines are chosen
            agencies = await self.hass.async_add_executor_job(
                get_agencies_in_zip, self.hass.config.path(DEFAULT_PATH),
                self._user_inputs[CONF_FILE])
        else:
            if self._pygtfs and hasattr(self._pygtfs, 'session'):
                try:
                    self._pygtfs.session.close()
                    self._pygtfs.engine.dispose()
                except Exception:
                    pass
            self._pygtfs = await self.hass.async_add_executor_job(
                get_gtfs,
                self.hass,
                DEFAULT_PATH,
                self._user_inputs,
                False,
            )
            check_data = await self._check_data(self._user_inputs)
            if check_data :
                # nothing to re-type on this step: the problem is the datasource picked
                # earlier, so send the user back there with the message instead of
                # closing the flow. "extracting" keeps its own abort message.
                if check_data == "extracting":
                    self._user_inputs.update(user_input)
                    return await self.async_step_extracting()
                return await self._back_to_source(check_data)

            agencies = await self.hass.async_add_executor_job(
                get_agency_list, self._pygtfs, self._user_inputs)
        if len(agencies) > 1:
            agencies[:0] = ["0: ALL"]
            errors: dict[str, str] = {}
            if user_input is None:
                return self.async_show_form(
                    step_id="agency",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_AGENCY): vol.In(agencies),
                        },
                    ),
                    description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    errors=errors,
                ) 
        else:
            user_input = {}
            user_input[CONF_AGENCY] = "0: ALL"
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Agency: {self._user_inputs}")
        # no route_type step any more: the type is read from the route itself,
        # 99 means "do not filter the route list"
        self._user_inputs[CONF_ROUTE_TYPE] = "99"
        return await self.async_step_route()
        
    async def async_step_route(self, user_input: dict | None = None) -> FlowResult:
        """Handle the route and reset the route_type to the proper one."""
        errors: dict[str, str] = {}
        # coming back from a failed reload: say why, on the screen that lets
        # another line be picked
        if self._pending_error:
            errors["base"] = self._pending_error
            self._pending_error = None
            user_input = None
        fresh = await self._fresh_source()
        if not fresh:
            check_data = await self._check_data(self._user_inputs)
            _LOGGER.debug("Source check data: %s", check_data)
            if check_data :
                # same as in async_step_agency: the datasource is the problem, not
                # anything typed on this step.
                if check_data == "extracting":
                    self._user_inputs.update(user_input)
                    return await self.async_step_extracting()
                return await self._back_to_source(check_data)

            if self._pygtfs and hasattr(self._pygtfs, 'session'):
                try:
                    self._pygtfs.session.close()
                    self._pygtfs.engine.dispose()
                except Exception:
                    pass
            self._pygtfs = await self.hass.async_add_executor_job(
                get_gtfs,
                self.hass,
                DEFAULT_PATH,
                self._user_inputs,
                False,
            )
            # a datasource imported before the indexes existed never crosses the
            # import path again, so make sure of them here: costs a handful of
            # sqlite_master lookups when they are already in place
            await self.hass.async_add_executor_job(
                check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH,
                self._user_inputs[CONF_FILE])
        if user_input is None:
            gtfs_dir = self.hass.config.path(DEFAULT_PATH)
            if fresh:
                # nothing is imported yet, so the feed itself is the list.
                # Every option carries the "pruned" flag: no timetable is
                # loaded, and that flag is exactly what sends the submission
                # through the screen that imports the line.
                agency = self._user_inputs.get(CONF_AGENCY, "0: ALL").split(': ')[0]
                usable = await self.hass.async_add_executor_job(
                    get_route_options_from_zip, gtfs_dir,
                    self._user_inputs[CONF_FILE], agency)
            else:
                # only offer routes that actually carry trips: a datasource keeps its
                # full routes table even when the trips of a route are not loaded, and
                # picking one of those leads to an empty stop list and "no_stops".
                usable = await self.hass.async_add_executor_job(
                    partial(get_route_list, self._pygtfs, self._user_inputs,
                            with_trips_only=True, gtfs_dir=gtfs_dir))
            if not usable:
                return self.async_abort(
                    reason="no_routes_with_trips",
                    description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                )
            total = len(usable) if fresh else len(
                await self.hass.async_add_executor_job(
                    get_route_list, self._pygtfs, self._user_inputs))
            route_list = [
                # value carries route_type##route_id, label is the readable part
                selector.SelectOptionDict(value=r, label=r.split('##')[2])
                for r in usable
                ]
            placeholders = dict(TRANSLATION_DESCRIPTION_PLACEHOLDERS)
            placeholders["routes"] = str(len(usable))
            placeholders["routes_total"] = str(total)
            return self.async_show_form(
                step_id="route",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ROUTE, default = ""): selector.SelectSelector(selector.SelectSelectorConfig(options=route_list, translation_key="route",custom_value=True)),
                    },
                ),
                description_placeholders=placeholders,
                errors=errors,
            )
        _picked = user_input.get(CONF_ROUTE).split('##')
        user_input[CONF_ROUTE_TYPE] = _picked[0]
        user_input[CONF_ROUTE] = _picked[1]
        # the readable part is only used to suggest a sensor name
        self._route_label = _picked[2].split(" : ")[0] if len(_picked) > 2 else ""
        was_pruned = len(_picked) > 3 and _picked[3] == "pruned"
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Route: {self._user_inputs}")
        if was_pruned:
            # a previous prune removed this line's timetable to save space, so
            # there are no stops to offer. Reload the datasource from the zip
            # kept next to it before going on, rather than sending the user to
            # an empty list.
            return await self.async_step_route_reload()
        return await self.async_step_direction()

    async def async_step_route_reload_only(self, user_input: dict | None = None) -> FlowResult:
        """The reload screen when no other line is missing.

        Same step, different wording: only the step_id changes, so this hands
        straight over. Without a method of this name Home Assistant refuses the
        flow the moment the user submits it.
        """
        return await self.async_step_route_reload(user_input)

    async def async_step_route_reload(self, user_input: dict | None = None) -> FlowResult:
        """Offer to bring the line back, and any other missing one at the same time.

        Unpacking the feed is the slow part, minutes on a large network, and it
        costs the same whether one line or ten are copied out of it. Adding
        three lines one after the other would pay that price three times, so
        the other missing lines are offered here.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        filename = self._user_inputs[CONF_FILE]
        route_id = self._user_inputs[CONF_ROUTE]

        if user_input is None:
            # every line the feed declares but the datasource has no trips for,
            # minus the one being added, which is not optional here
            in_zip = await self.hass.async_add_executor_job(
                get_routes_in_zip, gtfs_dir, filename)
            loaded = await self.hass.async_add_executor_job(
                routes_in, real_path(gtfs_dir, filename))
            missing = sorted(in_zip - loaded - {route_id})
            # the operator was named on the agency screen: offer that
            # operator's missing lines, not the whole feed's
            agency = self._user_inputs.get(CONF_AGENCY, "0: ALL").split(': ')[0]
            missing = await self.hass.async_add_executor_job(
                routes_in_zip_for_agency, gtfs_dir, filename, missing, agency)
            if self._pygtfs and hasattr(self._pygtfs, 'session'):
                labels = await self.hass.async_add_executor_job(
                    get_route_labels, self._pygtfs, missing)
            else:
                # a fresh source has no database to ask yet
                labels = await self.hass.async_add_executor_job(
                    get_route_labels_from_zip, gtfs_dir, filename, missing)
            schema = {}
            if labels:
                # checkboxes read well for a handful of missing lines; a
                # national feed leaves thousands, which only a searchable
                # dropdown survives
                mode = (selector.SelectSelectorMode.LIST if len(labels) <= 25
                        else selector.SelectSelectorMode.DROPDOWN)
                schema[vol.Optional(CONF_ALSO_RELOAD, default=[])] = selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[selector.SelectOptionDict(value=r, label=l)
                                 for r, l in labels.items()],
                        multiple=True, mode=mode,
                    ))
            # with nothing else missing, the paragraph about other lines would
            # read "0 other lines": use the wording that does not mention them
            return self.async_show_form(
                step_id="route_reload" if labels else "route_reload_only",
                data_schema=vol.Schema(schema),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "route": self._route_label,
                    "missing": str(len(labels)),
                },
            )
        # Bring back the wanted lines. The zip is unpacked into a scratch
        # database the sensors never open, the lines are copied across, and the
        # scratch file is dropped: the other sensors keep reading a complete
        # datasource throughout, and nothing else that was pruned comes back.
        self._import_routes = [route_id] + [
            r for r in user_input.get(CONF_ALSO_RELOAD, []) if r != route_id]
        return await self.async_step_importing()

    async def async_step_importing(self, user_input: dict | None = None) -> FlowResult:
        """Run the import while showing how far the scratch database has grown.

        Unpacking takes minutes on a large network. Calling it straight from a
        step leaves Home Assistant waiting on the coroutine with nothing on
        screen, so the work goes in a task and this step reports on it.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        filename = self._user_inputs.get(CONF_FILE, "")

        if self._import_job is None:
            clean = self._user_inputs.get("clean_feed_info", False)
            routes = list(self._import_routes)

            def _build(scratch_file):
                # the feed is filtered down to the wanted lines before pygtfs
                # sees it: on a national feed this is what turns the import
                # from an hour into a minute
                return build_scratch_database(
                    gtfs_dir, filename + ".zip", scratch_file, clean,
                    only_routes=routes)

            # The work runs in the executor and finishes whatever happens to
            # this window, but nobody would hear about it once the flow is
            # abandoned. A background task outlives the flow and reports the
            # outcome, which is what the screen promises.
            async def _watch():
                added = await self._import_job
                await async_notify_import(self.hass, filename, routes, added)

            self._import_job = self.hass.async_add_executor_job(
                import_routes, gtfs_dir, filename, routes, _build)
            self.hass.async_create_background_task(
                _watch(), name=f"gtfs2 watch import {filename}")

        if not self._import_job.done():
            # Home Assistant redraws a progress screen only when its
            # progress_task finishes, never while it runs. So the task handed
            # over is a short wait, not the import itself: it ends every few
            # seconds, the step is called again, and the size is read afresh.
            # Watching the import directly would freeze the figure at 0 - the
            # scratch file does not exist yet when the screen first appears.
            self._extract_size = await self.hass.async_add_executor_job(
                _scratch_size, gtfs_dir, filename)

            async def _tick():
                await asyncio.wait({self._import_job}, timeout=3)

            self._import_task = self.hass.async_create_task(_tick())
            return self.async_show_progress(
                step_id="importing",
                progress_action="importing",
                progress_task=self._import_task,
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "file": filename,
                    "size": self._extract_size,
                    "routes": str(len(self._import_routes)),
                },
            )

        added = self._import_job.result()
        self._import_job = None
        self._import_task = None
        if not added:
            return self.async_show_progress_done(next_step_id="reload_failed")
        return self.async_show_progress_done(next_step_id="reload_done")

    async def async_step_reload_failed(self, user_input: dict | None = None) -> FlowResult:
        """The import could not run: the datasource is untouched.

        Back to the line picker rather than out of the flow. The source and the
        operator are still valid - only this one line could not be brought
        back - and abandoning would throw away everything chosen since, for a
        failure that says nothing about the rest.
        """
        self._pending_error = "reload_failed"
        return await self.async_step_route()

    async def async_step_reload_done(self, user_input: dict | None = None) -> FlowResult:
        """Carry on picking the journey, with the lines now loaded."""
        # reopen directly, without get_gtfs's extracting gate: the import
        # just succeeded so the file exists, and a coordinator adding an
        # index at this very moment leaves a journal that the gate mistakes
        # for an unpacking still running. Measured in the field: get_gtfs
        # answered "extracting" and the direction screen crashed on a string.
        self._pygtfs = await self.hass.async_add_executor_job(
            open_datasource, self.hass.config.path(DEFAULT_PATH),
            self._user_inputs[CONF_FILE])
        if not self._pygtfs:
            return await self.async_step_reload_failed()
        # a fresh source was just created by this very import and never went
        # through the step that checks the indexes, so check them here
        await self.hass.async_add_executor_job(
            check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH,
            self._user_inputs[CONF_FILE])
        return await self.async_step_direction()

    async def async_step_direction(self, user_input: dict | None = None) -> FlowResult:
        """Pick the direction, labelled with where the vehicle actually goes."""
        errors: dict[str, str] = {}
        if user_input is None:
            # direction_id alone means nothing to the user, so show the first
            # and last stop of each one
            labels = await self.hass.async_add_executor_job(
                get_direction_labels, self._pygtfs, self._user_inputs[CONF_ROUTE])
            options = [
                selector.SelectOptionDict(value=k, label=labels[k])
                for k in sorted(labels)
            ] or [
                selector.SelectOptionDict(value="0", label="0"),
                selector.SelectOptionDict(value="1", label="1"),
            ]
            return self.async_show_form(
                step_id="direction",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_DIRECTION): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=options)
                        ),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Direction: {self._user_inputs}")
        # GTFS route_type 2 is rail: those feeds rarely have usable stop ids,
        # so they are matched on city names instead of picked from a list
        if self._user_inputs[CONF_ROUTE_TYPE] == "2":
            return await self.async_step_stops_train()
        return await self.async_step_stops()

    async def async_step_stops(self, user_input: dict | None = None) -> FlowResult:
        """Pick the two stops of the journey."""
        errors: dict[str, str] = {}
        try:
            stops = await self.hass.async_add_executor_job(
                get_stop_list,
                self._pygtfs,
                self._user_inputs[CONF_ROUTE],
                self._user_inputs[CONF_DIRECTION],
            )
        except Exception as ex:  # pylint: disable=broad-except
            # a bare except here reported every failure as "no stops",
            # a locked database and a bad route id included
            _LOGGER.error("Error reading the stops of route %s: %s",
                          self._user_inputs.get(CONF_ROUTE), ex)
            return self.async_abort(reason="generic_failure")
        if not stops:
            _LOGGER.debug("No stops for route: %s", self._user_inputs.get(CONF_ROUTE))
            return self.async_abort(reason="no_stops")

        # the value must stay "stop_id: Name (seq)": get_next_departure cuts
        # the id back out of it. Only the label is the user's to read.
        # Two platforms of one terminus can share a name while being separate
        # stops, so number those: they are genuinely different departures.
        _names = [_stop_name(e) for e in stops]
        _nth = {}
        stop_options = []
        for entry in stops:
            name = _stop_name(entry)
            if _names.count(name) > 1:
                _nth[name] = _nth.get(name, 0) + 1
                name = f"{name} ({_nth[name]})"
            stop_options.append(selector.SelectOptionDict(value=entry, label=name))

        def _show(errors, previous=None):
            """Render the form, keeping what was already picked."""
            previous = previous or {}
            return self.async_show_form(
                step_id="stops",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_ORIGIN, default=previous.get(CONF_ORIGIN, stops[0])
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=stop_options)
                        ),
                        vol.Required(
                            CONF_DESTINATION,
                            default=previous.get(CONF_DESTINATION, stops[-1]),
                        ): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=stop_options)
                        ),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            return _show(errors)

        # get_stop_list returns the stops in travel order and
        # get_next_departure only matches an origin before its destination, so
        # an arrival picked above the departure can be rejected without a query
        origin, destination = user_input[CONF_ORIGIN], user_input[CONF_DESTINATION]
        if stops.index(destination) <= stops.index(origin):
            errors["base"] = "stops_out_of_order"
            return _show(errors, user_input)

        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Stops: {self._user_inputs}")
        return await self.async_step_sensor()

    async def async_step_sensor(self, user_input: dict | None = None) -> FlowResult:
        """Name the sensor, now that both stops are known."""
        errors: dict[str, str] = {}
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        line = self._route_label
        trip = f"{_base_name(origin)} → {_base_name(destination)}"
        if _base_name(origin) == _base_name(destination):
            # circular line: both ends read the same, so name the journey by
            # where its rotation heads first, exactly like the return offer
            labels = await self.hass.async_add_executor_job(
                get_direction_labels, self._pygtfs, self._user_inputs[CONF_ROUTE]
            )
            trip = labels.get(str(self._user_inputs.get(CONF_DIRECTION)), "") or trip
        suggested = f"{line} {trip}".strip() if line else trip
        if self._return_trip is None:
            await self._find_return_trip(origin, destination)

        def _show(errors, previous=None):
            previous = previous or {}
            return self.async_show_form(
                step_id="sensor",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_NAME, default=previous.get(CONF_NAME, suggested)
                        ): str,
                        vol.Optional(
                            CONF_INCLUDE_TOMORROW,
                            default=previous.get(CONF_INCLUDE_TOMORROW, False),
                        ): selector.BooleanSelector(),
                        **({vol.Optional(
                            CONF_ADD_RETURN, default=True
                        ): selector.BooleanSelector()} if self._return_trip else {}),
                    },
                ),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "trip": trip,
                    "return_trip": self._return_name or "",
                },
                errors=errors,
            )

        if user_input is None:
            return _show(errors)

        # only used to branch, it must not end up in the entry
        add_return = user_input.pop(CONF_ADD_RETURN, False)
        # an unticked BooleanSelector is simply absent from user_input, and the
        # coordinator reads data["include_tomorrow"] directly
        user_input.setdefault(CONF_INCLUDE_TOMORROW, False)
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Sensor: {self._user_inputs}")
        # whether a bus is due right now is the coordinator's business: a
        # sensor created in the evening, or on a day the line does not run,
        # is still valid. Only ask whether the journey exists at all.
        exists = await self.hass.async_add_executor_job(
            has_trip_between,
            self._pygtfs,
            self._user_inputs[CONF_ROUTE],
            _stop_id(origin),
            _stop_id(destination),
        )
        if not exists:
            errors["base"] = "no_trip_between"
            return _show(errors, user_input)

        if add_return:
            await self._create_return_trip()
        # async_create_entry ends the flow, so the sensor is created through a
        # second flow, the same way the return journey already is. That leaves
        # this one alive to offer what comes next.
        # a name already taken would create an entry the sensor platform then
        # drops as a duplicate unique_id: say so here instead
        if any(e.data.get(CONF_NAME) == user_input[CONF_NAME]
               for e in self.hass.config_entries.async_entries(DOMAIN)):
            errors["base"] = "name_taken"
            return _show(errors, user_input)
        # the second flow can still refuse - a unique_id taken between the check
        # above and here, or an import step that aborts - and announcing a
        # sensor that was never created would send the user looking for it
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=dict(self._user_inputs),
        )
        if result.get("type") != data_entry_flow.FlowResultType.CREATE_ENTRY:
            _LOGGER.error("The sensor was not created: %s", result.get("reason"))
            errors["base"] = "not_created"
            return _show(errors, user_input)
        self._created_name = user_input[CONF_NAME]
        return await self.async_step_finished()

    async def async_step_finished(self, user_input: dict | None = None) -> FlowResult:
        """Offer what usually comes next: another journey, or a cleanup.

        Adding several journeys is the normal case - a line each way, a couple
        of lines - and each one used to mean starting the flow again from the
        menu. Optimising is offered here too because this is the moment it pays
        off: the datasource has just grown.
        """
        if user_input is None:
            return self.async_show_menu(
                step_id="finished",
                menu_options=["start_end", "optimise", "finish"],
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "name": self._created_name,
                },
            )
        return await self.async_step_finish()

    def _reset_for_next_journey(self):
        """Forget the journey just created, keep the datasource.

        Going round again must not inherit the previous stops or sensor name,
        but re-picking the same source every time would be tedious, so the file
        and what was derived from it stay.
        """
        keep = {k: v for k, v in self._user_inputs.items()
                if k in (CONF_FILE, CONF_URL, CONF_EXTRACT_FROM, CONF_AGENCY,
                         CONF_API_KEY, CONF_API_KEY_NAME, CONF_API_KEY_LOCATION)}
        self._user_inputs = keep
        self._route_label = ""
        self._return_trip = None
        self._return_name = ""

    async def async_step_finish(self, user_input: dict | None = None) -> FlowResult:
        """Leave the flow without doing anything else."""
        return self.async_abort(
            reason="finished",
            description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
        )

    async def async_step_optimise(self, user_input: dict | None = None) -> FlowResult:
        """Shrink the datasource: drop unfollowed lines, then intern the rest.

        Both steps in one go, in that order: pruning first leaves interning
        less to rewrite. Measured on two Orleans lines, 85.7 MB became 32.1 MB.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        filename = self._user_inputs.get(CONF_FILE, "")
        # only the routes some entry actually reads: anything else in the file
        # is weight nothing queries
        keep = {e.data["route"].split(": ")[0]
                for e in self.hass.config_entries.async_entries(DOMAIN)
                if e.data.get("file") == filename and e.data.get("route")
                and not e.data.get("device_tracker_id")}
        # the entries created by this flow are made through separate flows, so
        # they are not guaranteed to be registered yet. Without this, the line
        # just added could be pruned away moments after being imported.
        if self._user_inputs.get(CONF_ROUTE):
            keep.add(self._user_inputs[CONF_ROUTE].split(": ")[0])
        if self._return_trip and self._return_trip.get(CONF_ROUTE):
            keep.add(self._return_trip[CONF_ROUTE].split(": ")[0])
        # same reasoning for the lines this flow imported alongside: their
        # sensors come in the next flows, and dropping them here would undo
        # an import the user asked for minutes ago
        keep.update(self._import_routes)
        unrestricted = any(
            e.data.get("device_tracker_id") or not e.data.get("route")
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if e.data.get("file") == filename
            # the datasource entry reads nothing: it must not make its own
            # source look unrestricted
            and e.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE)

        if user_input is None:
            size = await self.hass.async_add_executor_job(
                _database_size, gtfs_dir, filename)
            # name what goes and what stays: "frees space" is not enough to
            # accept losing timetables, and the count of each is what decides
            loaded = await self.hass.async_add_executor_job(
                routes_in, real_path(gtfs_dir, filename))
            dropped = {} if unrestricted else {r: 1 for r in loaded if r not in keep}
            # counted, not named: a network drops dozens of lines here and the
            # list buried the two figures that decide it
            return self.async_show_form(
                step_id="optimise",
                data_schema=vol.Schema({}),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "file": filename,
                    "size": size,
                    "kept": str(len(keep & loaded) if not unrestricted else len(loaded)),
                    "dropped": str(len(dropped)),
                },
            )
        result = await self.hass.async_add_executor_job(
            optimise_datasource, gtfs_dir, filename,
            None if unrestricted else keep)
        _LOGGER.info("Optimised datasource %s: %s", filename, result)
        return self.async_abort(
            reason="optimised",
            description_placeholders={
                **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                "size": await self.hass.async_add_executor_job(
                    _database_size, gtfs_dir, filename),
            },
        )

    async def async_step_stops_train(self, user_input: dict | None = None) -> FlowResult:
        """Handle the stops when train, as often impossible to select ID"""
        errors: dict[str, str] = {}

        # a station is several stops in GTFS, so offer the distinct names
        # rather than ids, and let a name be typed for feeds that list none
        stations = await self.hass.async_add_executor_job(
            get_station_list, self._pygtfs, self._user_inputs.get(CONF_ROUTE))
        if not stations:
            stations = await self.hass.async_add_executor_job(
                get_station_list, self._pygtfs)
        station_select = selector.SelectSelector(
            selector.SelectSelectorConfig(options=stations, custom_value=True)
        )

        def _show(errors, previous=None):
            """Render the form, keeping the city names already picked."""
            previous = previous or {}
            return self.async_show_form(
                step_id="stops_train",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ORIGIN, default=previous.get(CONF_ORIGIN, "")): station_select,
                        vol.Required(CONF_DESTINATION, default=previous.get(CONF_DESTINATION, "")): station_select,
                        vol.Optional(CONF_INCLUDE_TOMORROW, default=previous.get(CONF_INCLUDE_TOMORROW, False)): selector.BooleanSelector(),
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            return _show(errors)
        # an unticked BooleanSelector is simply absent from user_input
        user_input.setdefault(CONF_INCLUDE_TOMORROW, False)
        self._user_inputs.update(user_input)
        self._user_inputs[CONF_DIRECTION] = 0
        self._user_inputs[CONF_ROUTE] = "train"
        # the picked line's code: the departures hold to that line
        self._user_inputs["line"] = self._route_label
        _LOGGER.debug(f"UserInputs Stops Train: {self._user_inputs}")
        check_config = await self._check_config(self._user_inputs)
        if check_config:
            _LOGGER.debug(f"CheckConfig: {check_config}")
            # city names are typed by hand here: re-show the form with the message
            # instead of closing the flow on a typo.
            errors["base"] = check_config
            return _show(errors, user_input)
        else:
            return await self.async_step_sensor_train()

    async def async_step_sensor_train(self, user_input: dict | None = None) -> FlowResult:
        """Name the train sensor, suggested from the line and both stations."""
        errors: dict[str, str] = {}
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        # the outward keeps the line picked in the flow; the return may run
        # under its own code (SNCF: K8+ out, P8 back), so its line is read
        # from the schedule for that very direction
        line = self._route_label
        trip = f"{origin} → {destination}"
        suggested = f"{line} {trip}".strip() if line else trip
        if self._return_trip is None:
            # trains rarely run one way only, but check before offering.
            # A train sensor covers the station pair, not one line, so the
            # return wears the same label as the outward.
            back = f"{destination} → {origin}"
            self._return_name = f"{line} {back}".strip() if line else back
            exists = await self.hass.async_add_executor_job(
                has_train_trip_between, self._pygtfs, destination, origin,
                self._route_label or None,
            )
            self._return_trip = {
                CONF_ORIGIN: destination,
                CONF_DESTINATION: origin,
                CONF_NAME: self._return_name,
            } if exists else {}

        def _show(errors, previous=None):
            previous = previous or {}
            return self.async_show_form(
                step_id="sensor_train",
                data_schema=vol.Schema(
                    {
                        vol.Required(
                            CONF_NAME, default=previous.get(CONF_NAME, suggested)
                        ): str,
                        **({vol.Optional(
                            CONF_ADD_RETURN, default=False
                        ): selector.BooleanSelector()} if self._return_trip else {}),
                    },
                ),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "trip": trip,
                    "return_trip": self._return_name or "",
                },
                errors=errors,
            )

        if user_input is None:
            return _show(errors)
        # only used to branch, it must not end up in the entry
        add_return = user_input.pop(CONF_ADD_RETURN, False)
        # a name already taken would create an entry the sensor platform then
        # drops as a duplicate unique_id: say so here instead
        if any(e.data.get(CONF_NAME) == user_input[CONF_NAME]
               for e in self.hass.config_entries.async_entries(DOMAIN)):
            errors["base"] = "name_taken"
            return _show(errors, user_input)
        if add_return:
            # spawned as its own flow, before this one closes on the entry
            await self._create_return_trip()
        self._user_inputs.update(user_input)
        return self.async_create_entry(
            title=user_input[CONF_NAME], data=self._user_inputs
        )

    async def async_step_import(self, import_data: dict) -> FlowResult:
        """Create an entry from data built by the flow, with no screens.

        Used for the journeys the main flow hands over - the outward one, its
        return, any further one added from the closing screen - and for the
        datasource entries the bootstrap and the source steps create.
        """
        if import_data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
            # one datasource entry per source: the file name is the identity,
            # so a second creation aborts here instead of duplicating
            await self.async_set_unique_id(import_data[CONF_FILE])
            self._abort_if_unique_id_configured()
            options = import_data.pop("options", None) or {}
            return self.async_create_entry(
                title=import_data[CONF_FILE], data=import_data, options=options
            )
        # the sensor derives its unique_id from the name, so a second entry
        # under the same name would be created and then silently dropped by the
        # sensor platform. Refuse it here, where the flow can still say so.
        await self.async_set_unique_id(f"gtfs-{import_data[CONF_NAME]}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=import_data[CONF_NAME], data=import_data
        )

    async def async_step_extracting(self, user_input: dict | None = None) -> FlowResult:
        """Wait for the background unpacking to finish, showing progress.

        get_gtfs forks the extraction and returns immediately, so there is no
        task to await. check_extracting watches the files the unpacking leaves
        behind, which is the only signal available from here.
        """
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        file = self._user_inputs.get(CONF_FILE, "")
        if self._extract_job is None:
            # Watched separately, on the integration side: closing this window
            # abandons the flow while the unpacking carries on, and the user
            # would otherwise never learn that it finished, or that it failed.
            # async_create_background_task outlives the flow; the notification
            # is raised there.
            self.hass.async_create_background_task(
                async_watch_extraction(self.hass, file),
                name=f"gtfs2 watch extraction {file}",
            )
            self._extract_job = self.hass.async_create_task(
                self._wait_for_extraction())

        if not self._extract_job.done():
            # Home Assistant redraws a progress screen only when its
            # progress_task finishes. Handing it the whole wait would freeze
            # the figure on its first value, so it gets a short tick instead
            # and the size is read again each time the step comes back.
            self._extract_size = await self.hass.async_add_executor_job(
                _database_size, gtfs_dir, file)

            async def _tick():
                await asyncio.wait({self._extract_job}, timeout=3)

            self._extract_task = self.hass.async_create_task(_tick())
            # The database file only grows while rows are written, so its size
            # is the one honest sign that something is happening. There is no
            # total to compare it against - it depends on the network - so it
            # is shown as a running figure, not as a percentage.
            return self.async_show_progress(
                step_id="extracting",
                progress_action="extracting",
                progress_task=self._extract_task,
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "file": self._user_inputs.get(CONF_FILE, ""),
                    "size": self._extract_size,
                },
            )

        self._extract_job = None
        self._extract_task = None
        return self.async_show_progress_done(
            next_step_id=self._extract_next_step or "agency")

    async def _wait_for_extraction(self):
        """Poll until the datasource stops looking like it is being unpacked."""
        gtfs_dir = self.hass.config.path(DEFAULT_PATH)
        file = self._user_inputs.get(CONF_FILE, "")
        # Every source now comes through here, including one whose datasource
        # is already built: waiting five seconds to discover there is nothing
        # to wait for would be five seconds of nothing. A finished feed is
        # recognised at once.
        ok, _ = await self.hass.async_add_executor_job(
            check_extraction_result, gtfs_dir, file)
        if ok and not await self.hass.async_add_executor_job(
            check_extracting, self.hass, gtfs_dir, file
        ):
            return
        # the fork needs a moment before it creates the journal file, so do not
        # treat a not-yet-started extraction as a finished one
        await asyncio.sleep(5)
        while await self.hass.async_add_executor_job(
            check_extracting, self.hass, gtfs_dir, file
        ):
            await asyncio.sleep(5)

    async def _find_return_trip(self, origin, destination):
        """Look for the same journey the other way round.

        Both directions of a line share one route_id, so the mirror is the
        same route with direction_id flipped. The stops are matched on name,
        not id: a terminus often has one stop per platform, so the id differs
        between directions even though the stop is the same place.
        """
        self._return_trip = {}
        other = "0" if str(self._user_inputs.get(CONF_DIRECTION)) == "1" else "1"
        try:
            stops = await self.hass.async_add_executor_job(
                get_stop_list, self._pygtfs, self._user_inputs[CONF_ROUTE], other
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug("No return journey for %s: %s",
                          self._user_inputs.get(CONF_ROUTE), ex)
            return
        by_name = {}
        for entry in stops:
            by_name.setdefault(_stop_name(entry), entry)
        # the outward destination becomes the return origin, and the reverse
        back_origin = by_name.get(_stop_name(destination))
        back_destination = by_name.get(_stop_name(origin))
        if not back_origin or not back_destination:
            _LOGGER.debug("Return journey: one of the stops is not served the other way")
            return
        exists = await self.hass.async_add_executor_job(
            has_trip_between,
            self._pygtfs,
            self._user_inputs[CONF_ROUTE],
            _stop_id(back_origin),
            _stop_id(back_destination),
        )
        trip = ""
        if not exists:
            # circular line: the other rotation also leaves the shared
            # terminus in the same stop order, so the mirrored pair is a
            # journey nothing runs. The return is then the same pair the
            # other way round the loop, held to that direction since the
            # pair alone no longer implies it.
            loop_origin = by_name.get(_stop_name(origin))
            loop_destination = by_name.get(_stop_name(destination))
            if loop_origin and loop_destination:
                exists = await self.hass.async_add_executor_job(
                    has_trip_between,
                    self._pygtfs,
                    self._user_inputs[CONF_ROUTE],
                    _stop_id(loop_origin),
                    _stop_id(loop_destination),
                    other,
                )
            if not exists:
                _LOGGER.debug("Return journey: no trip runs it")
                return
            back_origin, back_destination = loop_origin, loop_destination
            # the plain ends would collide with the outward sensor's name:
            # tell the rotations apart by where each heads first
            labels = await self.hass.async_add_executor_job(
                get_direction_labels, self._pygtfs, self._user_inputs[CONF_ROUTE]
            )
            trip = labels.get(other, "")
        trip = trip or f"{_base_name(back_origin)} → {_base_name(back_destination)}"
        line = self._route_label
        self._return_name = f"{line} {trip}".strip() if line else trip
        # only what differs: this runs when the screen opens, before the
        # options on it are answered, so the rest is merged at creation time
        self._return_trip = {
            CONF_DIRECTION: other,
            CONF_ORIGIN: back_origin,
            CONF_DESTINATION: back_destination,
            CONF_NAME: self._return_name,
        }

    async def _create_return_trip(self):
        """Create the return sensor through a second flow.

        A flow creates one entry, so the mirror is handed to a fresh flow on
        its import step, which creates it without showing anything.
        """
        if not self._return_trip:
            return
        # merged now, so the options answered on this screen are carried over
        data = {**self._user_inputs, **self._return_trip}
        _LOGGER.debug("Creating return journey: %s", data.get(CONF_NAME))
        # awaited rather than scheduled: a task created here is tied to a flow
        # that is about to finish, and would be cancelled with it
        result = await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=data,
        )
        # the return is a convenience, so a refusal must not stop the outward
        # journey being created: log it and carry on
        if result.get("type") != data_entry_flow.FlowResultType.CREATE_ENTRY:
            _LOGGER.warning("The return journey was not created: %s",
                            result.get("reason"))

    async def _back_to_source(self, reason):
        """Return to the step that picked the datasource, carrying the error."""
        self._pending_error = reason
        if self._user_inputs.get(CONF_DEVICE_TRACKER_ID, None):
            return await self.async_step_local_stops()
        if self._user_inputs.get(CONF_EXTRACT_FROM, None) == "url":
            return await self.async_step_source_url()
        if self._user_inputs.get(CONF_EXTRACT_FROM, None) == "zip" and self._user_inputs.get(CONF_URL, None) == "na":
            return await self.async_step_source_zip()
        return await self.async_step_start_end()

    async def _check_data(self, data):
        if self._pygtfs and hasattr(self._pygtfs, 'session'):
            try:
                self._pygtfs.session.close()
                self._pygtfs.engine.dispose()
            except Exception:
                pass
        self._pygtfs = await self.hass.async_add_executor_job(
            get_gtfs, self.hass, DEFAULT_PATH, data, False
        )
        _LOGGER.debug("Checkdata pygtfs: %s with data: %s", self._pygtfs, data)
        if self._pygtfs in ['no_data_file', 'no_zip_file', 'extracting'] :
            return self._pygtfs
        check_index = await self.hass.async_add_executor_job(
                    check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH, data["file"]
                )            
        return None
        
    async def _check_config(self, data):
        if self._pygtfs and hasattr(self._pygtfs, 'session'):
            try:
                self._pygtfs.session.close()
                self._pygtfs.engine.dispose()
            except Exception:
                pass
        self._pygtfs = await self.hass.async_add_executor_job(
            get_gtfs, self.hass, DEFAULT_PATH, data, False
        )
        if self._pygtfs == "no_data_file":
            return "no_data_file"
        self._data = {
            "schedule": self._pygtfs,
            "origin": data["origin"],
            "destination": data["destination"],
            "offset": 0,
            "include_tomorrow": True,
            "gtfs_dir": DEFAULT_PATH,
            "name": data.get(CONF_NAME, ""),
            "next_departure": None,
            "file": data["file"],
            "route_type": data["route_type"],
            "line": data.get("line", "")
        }
        # check and/or add indexes
        check_index = await self.hass.async_add_executor_job(
                    check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH, data["file"]
                )
        try:
            self._data["next_departure"] = await self.hass.async_add_executor_job(
                get_next_departure, self.hass, self._data
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error(
                "Config: error getting gtfs data from generic helper: %s",
                {ex},
                exc_info=1,
            )
            return "generic_failure"
        if self._data["next_departure"]:
            return None
        return "stop_incorrect"

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return GTFSOptionsFlowHandler(config_entry)


class GTFSOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._pygtfs = ""
        self._data: dict[str, str] = {}
        self._user_inputs: dict = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if self.config_entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
            # the datasource entry's CONFIGURE button is where the source's
            # realtime feeds are added, changed and removed; the sensors
            # pick the change up on their next cycle, without a reload
            return await self.async_step_real_time()
        if user_input is not None:
            if self.config_entry.data.get(CONF_DEVICE_TRACKER_ID, None):
                _data = user_input
                _data["file"] = self.config_entry.data["file"]
                _data["url"] = self.config_entry.data["url"]
                _data["extract_from"] = self.config_entry.data["extract_from"]
                _data["device_tracker_id"] = self.config_entry.data["device_tracker_id"]
                _data["radius"] = user_input["radius"]
                stop_limit = await _check_stop_list(self, _data)
                if stop_limit :
                    return self.async_abort(reason=stop_limit)
            # the realtime fields mirrored from the datasource entry live in
            # these options too, for a downgrade to fall back on: an edit of
            # the sensor's own knobs must not wipe them
            for key in (*RT_OPTION_KEYS, CONF_REAL_TIME):
                if key in self.config_entry.options and key not in user_input:
                    self._user_inputs[key] = self.config_entry.options[key]
            self._user_inputs.update(user_input)
            _LOGGER.debug(f"UserInputs Options Init: {self._user_inputs}")
            return self.async_create_entry(title="", data=self._user_inputs)

        if self.config_entry.data.get(CONF_DEVICE_TRACKER_ID, None):
            opt1_schema = {
                    vol.Optional(CONF_LOCAL_STOP_REFRESH_INTERVAL, default=self.config_entry.options.get(CONF_LOCAL_STOP_REFRESH_INTERVAL, DEFAULT_LOCAL_STOP_REFRESH_INTERVAL)): int,
                    vol.Optional(CONF_RADIUS, default=self.config_entry.options.get(CONF_RADIUS, DEFAULT_LOCAL_STOP_RADIUS)): vol.All(vol.Coerce(int), vol.Range(min=50, max=5000)),
                    vol.Optional(CONF_TIMERANGE, default=self.config_entry.options.get(CONF_TIMERANGE, DEFAULT_LOCAL_STOP_TIMERANGE)): vol.All(vol.Coerce(int), vol.Range(min=15, max=120)),
                    vol.Optional(CONF_OFFSET, default=self.config_entry.options.get(CONF_OFFSET, DEFAULT_OFFSET)): int,
                    vol.Required(CONF_MAX_LOCAL_STOPS, default=self.config_entry.options.get(CONF_MAX_LOCAL_STOPS, DEFAULT_MAX_LOCAL_STOPS)): int,
                }
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(opt1_schema),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors = errors
            )

        else:
            opt1_schema = {
                        vol.Optional(CONF_REFRESH_INTERVAL, default=self.config_entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)): int,
                        vol.Optional(CONF_OFFSET, default=self.config_entry.options.get(CONF_OFFSET, DEFAULT_OFFSET)): int,
                    }
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(opt1_schema),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
            )

    async def async_step_real_time(
           self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """The source's realtime feeds, shared by every sensor reading it.

        Every field is optional: emptying them all removes realtime from the
        source, which is the one gesture the old per-sensor screens never
        offered. The key fields stay behind their toggle.
        """
        errors: dict[str, str] = {}
        opts = self.config_entry.options

        if user_input is None:
            return self.async_show_form(
                step_id="real_time",
                data_schema=vol.Schema(_source_rt_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input.pop(CONF_NEEDS_API_KEY, False):
            self._user_inputs.update(user_input)
            return await self.async_step_real_time_key()
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInput Source realtime: {self._user_inputs}")
        return self.async_create_entry(
            title="", data=_collect_source_rt_options(
                self._user_inputs, {}, previous=self.config_entry.options))

    async def async_step_real_time_key(
           self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the realtime api key, only when the source needs one."""
        errors: dict[str, str] = {}
        opts = self.config_entry.options
        if user_input is None:
            return self.async_show_form(
                step_id="real_time_key",
                data_schema=vol.Schema(_source_rt_key_schema(opts)),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )
        _LOGGER.debug("UserInput Source realtime key received")
        return self.async_create_entry(
            title="", data=_collect_source_rt_options(
                self._user_inputs, user_input, previous=self.config_entry.options))


async def _check_stop_list(self, data):
    _LOGGER.debug("Checkstops option with data: %s", data)
    if self._pygtfs and hasattr(self._pygtfs, 'session'):
        try:
            self._pygtfs.session.close()
            self._pygtfs.engine.dispose()
        except Exception:
            pass    
    self._pygtfs = await self.hass.async_add_executor_job(
        get_gtfs, self.hass, DEFAULT_PATH, data, False
    )
    count_stops = await self.hass.async_add_executor_job(
                get_local_stop_list, self.hass, self._pygtfs, data
            )  
    if count_stops > DEFAULT_MAX_LOCAL_STOPS:
        _LOGGER.debug("Checkstops limit reached with: %s", count_stops)
        return "stop_limit_reached"
    return None         
