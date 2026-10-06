"""ConfigFlow for GTFS integration."""
from __future__ import annotations

import logging
import os
from functools import partial
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    id_of,
    DEFAULT_PATH,
    DOMAIN,
    DEFAULT_REFRESH_INTERVAL,
    DEFAULT_LOCAL_STOP_REFRESH_INTERVAL,
    DEFAULT_LOCAL_STOP_TIMERANGE,
    DEFAULT_LOCAL_STOP_RADIUS,
    DEFAULT_OFFSET,
    CONF_URL,
    CONF_EXTRACT_FROM,
    CONF_FILE,
    CONF_DEVICE_TRACKER_ID,
    CONF_AGENCY,
    CONF_ROUTE_TYPE,
    CONF_ROUTE,
    CONF_ORIGIN,
    CONF_DESTINATION,
    CONF_NAME,
    CONF_LOCAL_STOP_REFRESH_INTERVAL,
    CONF_RADIUS,
    CONF_TIMERANGE,
    CONF_REFRESH_INTERVAL,
    CONF_OFFSET,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    DEFAULT_MAX_LOCAL_STOPS,
    CONF_MAX_LOCAL_STOPS,
)

from .data.datasource import get_gtfs, check_datasource_index
from .data.map_files import name_in_use
from .feed.files import remove_datasource, close_schedule
from .domain.line_list import get_agency_list, get_route_count, get_route_list
from .domain.local_stops import get_local_stop_list
from .domain.line_list import get_route_options_from_zip, get_agencies_in_zip
from .domain.line_labels import LINE_MODES, line_number, with_modes
from .notifications import _async_text
from .data.source_refresh import source_lock, source_zip_path, source_zip_url

from .feed.source_entries import (
    datasource_entry,
    datasource_files,
    journey_entry_data,
)
from .const import ALSO_AT, TRANSLATION_DESCRIPTION_PLACEHOLDERS
from .flow_train import ALL_TRAINS, TrainScreens, kept_train_stations, train_station_fields, train_stations_between
from .data.stop_rules import RAIL_ROUTE_TYPES
from .flow_reload import ReloadScreens
from .flow_source import SourceScreens
from .flow_options import OptionsScreens
from .flow_journey import JourneyScreens, kept_stops, stop_fields
from .data.places import get_stops_between

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

    from .domain.stations import RailIndex

_LOGGER = logging.getLogger(__name__)


def _is_rail(value: str) -> bool:
    """Whether a route screen value (route_type##route_id##...) is a train line."""
    try:
        return int(value.split("##")[0]) in RAIL_ROUTE_TYPES
    except ValueError:
        return False


def _label_of(picked: list[str]) -> str:
    """The line number a route screen value names a sensor with, "" for
    the trains from the stations first (each line ticked names its own)."""
    if len(picked) < 3 or picked[1] == ALL_TRAINS:
        return ""
    return line_number(picked[2])


@config_entries.HANDLERS.register(DOMAIN)
class ConfigFlow(JourneyScreens, SourceScreens, ReloadScreens, TrainScreens, OptionsScreens,
                 config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GTFS."""

    VERSION = 10
    # 2: a datasource entry's unique_id is gtfs2-source-<file>
    # 3: a source made from a zip has the file:// url of its kept zip, not "na"
    MINOR_VERSION = 3

    def __init__(self) -> None:
        """Init ConfigFlow."""
        self._pygtfs = ""
        self._user_inputs: dict = {}
        # the way the rider leaves the origin, when it was asked: it narrows
        # the destination screen and picks a loop's rotation, the entry does
        # not keep it
        self._towards: str | None = None
        self._pending_error: str | None = None
        # a source's settings reached from the main menu: the source picked
        # and the screen asked for
        self._picked_source: config_entries.ConfigEntry | None = None
        self._source_screen: str | None = None
        # the screen that picked the source, where an error about the source
        # sends the rider back to (see _back_to_source)
        self._source_step: str | None = None
        # what the departure screen says when it comes up: why the arrival
        # screen sent the rider back to it, or that an import left lines out
        self._stops_error: str | None = None
        self._extract_job = None
        self._extract_next_step: str | None = None
        self._route_label: str = ""
        # the networks an envelope of zips offers, while one is being picked
        self._inner_zips: list = []
        # the line as the route screen showed it, recalled on the stop screens
        self._route_shown: str = ""
        # the lines the route screen offered: only those are taken
        self._routes_offered: set[str] = set()
        # the import running behind the progress screen, and its routes
        self._import_job = None
        self._import_routes: list = []
        # the short wait the progress screens are handed (ReloadScreens._tick)
        self._progress_tick = None
        # the reopening after an import, shared by the calls that step there
        self._reload_done_job = None
        # the lines the import was asked for and did not bring in, as named
        # to the rider
        self._import_missing: str = ""
        # the trains of the source's zip, (file, index), read once for the
        # station screens of a train journey from the stations first
        # (TrainScreens._train_source)
        self._rail: tuple[str, RailIndex | None] | None = None
        # the import running is the one a train journey from the stations
        # first asked for: its options screen comes next, not the departure
        # screen
        self._train_import = False
        # the entries of the train lines ticked, each with its return
        # (TrainScreens._line_plans)
        self._train_plans: list[dict] = []
        # what the last created entry was called, shown on the closing screen
        self._created_name: str = ""
        # the mirror journey, worked out once the stops are known
        self._return_trip: dict | None = None
        self._return_name: str = ""
        # what the realtime screen collected, while its key screen runs
        self._source_rt_inputs: dict = {}
        # the line and direction picked, where another journey on the same
        # line starts from once this one is created
        self._line: dict = {}

    @callback
    def async_remove(self) -> None:
        """Let the datasource the flow opened go (_let_schedule_go)."""
        _let_schedule_go(self)

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        # with no datasource yet, only the first entry can lead anywhere,
        # so say it rather than describing the general case
        datasources = datasource_files(self.hass)
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
            # can read from it, its settings change while sensors read it,
            # and it is removed last
            menu_options=["source", "start_end", "local_stops", "source_real_time",
                          "source_static_refresh", "remove"],
            description_placeholders=placeholders,
        )

                   
    async def async_step_start_end(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        errors: dict[str, str] = {}
        if user_input is not None and await self._fresh_source_of(user_input[CONF_FILE]) \
                and not await self.hass.async_add_executor_job(
                    os.path.exists, source_zip_path(self.hass, user_input[CONF_FILE])):
            # neither a database nor its zip: nothing to read the lines
            # from, until a refresh of the source downloads the feed again
            errors["base"] = "not_built"
        if user_input is None or errors:
            # reached again from the closing screen: the previous journey must
            # not leak into this one
            if self._created_name:
                self._reset_for_next_journey()
            self._take_pending_error(errors)
            datasources = datasource_files(self.hass)
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

        user_input[CONF_URL] = source_zip_url(self.hass, user_input[CONF_FILE])
        user_input[CONF_EXTRACT_FROM] = "zip"
        self._source_step = "start_end"
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Start End: {self._user_inputs}")
        return await self.async_step_agency()            
            
    async def async_step_local_stops(self, user_input: dict | None = None) -> FlowResult:
        """Handle the source."""
        # local stops create the entry directly, they do not go on to pick a route
        self._extract_next_step = "local_stops"
        errors: dict[str, str] = {}       

        async def _show(errors: dict[str, str], previous: dict | None = None) -> FlowResult:
            """Render the form, keeping what the user already typed."""
            previous = previous or {}
            tracker = previous.get(CONF_DEVICE_TRACKER_ID)
            datasources = datasource_files(self.hass)
            return self.async_show_form(
                step_id="local_stops",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_FILE, default=previous.get(CONF_FILE, "")): vol.In(datasources),
                        vol.Required(
                            CONF_DEVICE_TRACKER_ID, **({"default": tracker} if tracker else {}),
                        ): selector.EntitySelector(
                            selector.EntitySelectorConfig(domain=["person","zone"]),                          
                        ),
                        vol.Required(CONF_NAME, default=previous.get(CONF_NAME, "")): str, 
                    },
                ),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                errors=errors,
            )

        if user_input is None:
            if self._take_pending_error(errors):
                return await _show(errors, self._user_inputs)
            if not self._user_inputs.get(CONF_DEVICE_TRACKER_ID):
                return await _show(errors)
            # back from the unpacking wait: what was submitted before it goes
            # on, instead of an empty screen asking for all of it again
            user_input = {key: self._user_inputs.get(key)
                          for key in (CONF_FILE, CONF_DEVICE_TRACKER_ID, CONF_NAME)}
        user_input[CONF_URL] = source_zip_url(self.hass, user_input[CONF_FILE])
        user_input[CONF_EXTRACT_FROM] = "zip"    
        # the stop sensors are named after the stop and the tracker, so a
        # second entry for the same tracker on the same source made sensors
        # the platform then dropped as duplicates; a name in use is refused
        # as on the journey screen
        entries = [e.data for e in self.hass.config_entries.async_entries(DOMAIN)]
        if name_in_use(user_input[CONF_NAME], {e.get(CONF_NAME) for e in entries}):
            errors["base"] = "name_taken"
            return await _show(errors, user_input)
        if any(e.get(CONF_DEVICE_TRACKER_ID) == user_input[CONF_DEVICE_TRACKER_ID]
               and e.get(CONF_FILE) == user_input[CONF_FILE] for e in entries):
            errors["base"] = "local_stops_exists"
            return await _show(errors, user_input)
        await self.async_set_unique_id(
            f"gtfs-local-{user_input[CONF_FILE]}-{user_input[CONF_DEVICE_TRACKER_ID]}")
        self._abort_if_unique_id_configured()
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Local Stops: {self._user_inputs}") 
        check_data = await self._check_data(self._user_inputs)
        if check_data :
            # "extracting" is not a user error: the datasource is being unpacked,
            # there is nothing to correct, so the progress screen waits for it.
            if check_data == "extracting":
                return await self.async_step_extracting()
            errors["base"] = check_data
            return await _show(errors, user_input)
        else:
            return self.async_create_entry(
                title=user_input[CONF_NAME], data=journey_entry_data(self._user_inputs)
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


    async def async_step_source_real_time(self, user_input: dict | None = None) -> FlowResult:
        """A source's realtime feeds, from the main menu."""
        self._source_screen = "real_time"
        return await self.async_step_pick_source()

    async def async_step_source_static_refresh(self, user_input: dict | None = None) -> FlowResult:
        """A source's timetable file and its updates, from the main menu."""
        self._source_screen = "static_refresh"
        return await self.async_step_pick_source()

    async def async_step_pick_source(self, user_input: dict | None = None) -> FlowResult:
        """Which source to set, then the screen its CONFIGURE button opens.

        The source's settings used to be reached from its entry only; the
        main menu offers them too, the same screens (OptionsScreens), saved
        on the source picked (_save_source).
        """
        files = datasource_files(self.hass)
        if not files:
            # the files are there, their entries not yet: the start of
            # Home Assistant creates them
            return self.async_abort(reason="no_source_entry")
        if user_input is None:
            return self.async_show_form(
                step_id="pick_source",
                data_schema=vol.Schema({vol.Required(CONF_FILE): vol.In(files)}),
                description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
            )
        self._picked_source = datasource_entry(self.hass, user_input[CONF_FILE])
        if self._picked_source is None:
            return self.async_abort(reason="no_source_entry")
        if self._source_screen == "real_time":
            return await self.async_step_real_time()
        return await self.async_step_static_refresh()

    def _source(self) -> config_entries.ConfigEntry | None:
        """The source picked from the main menu."""
        return self._picked_source

    def _save_source(self, options: dict[str, Any]) -> FlowResult:
        """A config flow creates no entry here: it updates the source
        picked, whose listeners follow as from its CONFIGURE button, and
        says so."""
        self.hass.config_entries.async_update_entry(self._picked_source, options=options)
        return self.async_abort(reason="source_saved")

    async def async_step_remove(self, user_input: dict | None = None) -> FlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is None:
            datasources = datasource_files(self.hass)
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
            # file deletions, off the event loop, and never under a refresh
            # or an import still writing the same files
            async with source_lock(self.hass, user_input[CONF_FILE]):
                removed = await self.hass.async_add_executor_job(
                    remove_datasource, self.hass, DEFAULT_PATH, user_input[CONF_FILE], True)
            _LOGGER.debug(f"Removed gtfs data source: {removed}")
        except Exception as ex:
            _LOGGER.exception("Error while deleting : %s", {ex})
            return self.async_abort(reason="generic_failure")
        # the datasource entry follows its files out; the journey entries
        # stay, as they always have, and fail on the missing database
        source = datasource_entry(self.hass, user_input[CONF_FILE])
        if source is not None:
            await self.hass.config_entries.async_remove(source.entry_id)
        return self.async_abort(reason="files_deleted")
        

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
            check_data = await self._check_data(self._user_inputs)
            if check_data :
                # nothing to re-type on this step: the problem is the datasource picked
                # earlier, so send the user back there with the message instead of
                # closing the flow. "extracting" goes to the progress screen.
                if check_data == "extracting":
                    # the step is reached on its way in too, with no answer
                    # to keep: there is only something to merge when the
                    # user has just submitted the screen
                    self._user_inputs.update(user_input or {})
                    return await self.async_step_extracting()
                return await self._back_to_source(check_data)

            agencies = await self.hass.async_add_executor_job(
                get_agency_list, self._pygtfs, self._user_inputs)
        if len(agencies) > 1:
            # the value stays "agency_id: agency_name", read back by its id;
            # the screen shows the names, and the id only where two agencies
            # share a name
            names = [agency.split(": ", 1)[-1] for agency in agencies]
            options = {"0: ALL": await _async_text(self.hass, "agency_all", "All operators")}
            options.update({agency: name if names.count(name) == 1 else agency
                            for agency, name in zip(agencies, names)})
            errors = {}
            if user_input is None:
                return self.async_show_form(
                    step_id="agency",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_AGENCY): vol.In(options),
                        },
                    ),
                    description_placeholders={
                        **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                        "source": self._user_inputs.get(CONF_FILE, ""),
                    },
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
        if self._take_pending_error(errors):
            user_input = None
        fresh = await self._fresh_source()
        if not fresh:
            check_data = await self._check_data(self._user_inputs)
            _LOGGER.debug("Source check data: %s", check_data)
            if check_data :
                # same as in async_step_agency: the datasource is the problem, not
                # anything typed on this step. user_input is None on the way in,
                # and on a re-show after an error, where it is blanked above.
                if check_data == "extracting":
                    self._user_inputs.update(user_input or {})
                    return await self.async_step_extracting()
                return await self._back_to_source(check_data)
        if user_input is None:
            gtfs_dir = self.hass.config.path(DEFAULT_PATH)
            if fresh:
                # nothing is imported yet, so the feed itself is the list.
                # Every option carries the "pruned" flag: no timetable is
                # loaded, and that flag is exactly what sends the submission
                # through the screen that imports the line.
                agency = id_of(self._user_inputs.get(CONF_AGENCY, "0: ALL"))
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
            total = len(usable) if fresh else await self.hass.async_add_executor_job(
                get_route_count, self._pygtfs, self._user_inputs)
            # the mode goes after the label where lines of one number differ
            words = {mode: await _async_text(self.hass, f"line_mode_{mode}", mode)
                     for mode in LINE_MODES}
            route_list = [
                # value carries route_type##route_id, label is the readable part
                selector.SelectOptionDict(value=r, label=label)
                for r, label in zip(usable, with_modes(usable, words))
                ]
            first = await self._trains_stations_first(usable)
            route_list = first + route_list
            self._routes_offered = set(usable) | {option["value"] for option in first}
            placeholders = dict(TRANSLATION_DESCRIPTION_PLACEHOLDERS)
            # the lines with a timetable loaded: a "##pruned" one is offered
            # but still to import, and on a fresh source every line is
            placeholders["routes"] = str(sum(not r.endswith("##pruned") for r in usable))
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
        _picked = str(user_input.get(CONF_ROUTE) or "").split('##')
        if user_input.get(CONF_ROUTE) not in self._routes_offered:
            # the field takes typed text, which is what makes a long list
            # searchable; text that is not one of the lines left the value
            # without its route and the step raised, and text shaped like a
            # line the list does not hold, or holds without its timetable,
            # read the stops of nothing. The list comes back, saying so
            self._pending_error = "route_not_listed"
            return await self.async_step_route()
        # every rail type is a train, the extended ones too (109 suburban
        # railway in Helsinki and Leipzig): "2" is what sends the entry
        # down the station screens and its departures through the train query
        user_input[CONF_ROUTE_TYPE] = "2" if _is_rail(user_input[CONF_ROUTE]) else _picked[0]
        user_input[CONF_ROUTE] = _picked[1]
        # the readable part is only used to suggest a sensor name; every
        # train line names none
        self._route_label = _label_of(_picked)
        self._route_shown = _picked[2] if len(_picked) > 2 else ""
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


    async def _trains_stations_first(self, usable: list[str]) -> list[selector.SelectOptionDict]:
        """The route screen's first option on a source with trains: the two
        stations first, then the lines riding between them, a sensor each.
        On a week of works the trains of one line leave from the next
        station, under another line's code. Offered on a fresh source too:
        the stations are read from the feed's zip, and the lines riding
        between the two picked imported then (TrainScreens._train_source).
        [] when there is none to offer."""
        if not any(_is_rail(value) for value in usable):
            return []
        first = await _async_text(self.hass, "trains_stations_first", "Trains: pick the stations, then the lines")
        return [selector.SelectOptionDict(value=f"2##{ALL_TRAINS}##{first}", label=first)]

    async def _check_data(self, data: dict) -> str | None:
        await _reopen_schedule(self, data)
        _LOGGER.debug("Checkdata pygtfs: %s with data: %s", self._pygtfs, data)
        if isinstance(self._pygtfs, str):
            # a sentinel of get_gtfs, whichever: not a schedule to index
            return self._pygtfs
        # a datasource imported before the indexes existed never crosses the
        # import path again, so make sure of them here: costs a handful of
        # sqlite_master lookups when they are already in place
        await self.hass.async_add_executor_job(
                    check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH, data["file"]
                )   
        return None
        

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return GTFSOptionsFlowHandler(config_entry)


class GTFSOptionsFlowHandler(OptionsScreens, config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._pygtfs: Schedule | str | None = ""
        self._user_inputs: dict = {}

    @callback
    def async_remove(self) -> None:
        """Let the datasource the flow opened go (_let_schedule_go)."""
        _let_schedule_go(self)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors: dict[str, str] = {}
        if self.config_entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE:
            return await self.async_step_source_menu()
        if not self.config_entry.data.get(CONF_DEVICE_TRACKER_ID, None):
            return await self._journey_options(user_input)
        # the stops around a person or a zone
        if user_input is not None:
            # a copy: the entry's data is only needed for the check,
            # and written into user_input it ended up in the options
            _data = {**user_input,
                     "file": self.config_entry.data["file"],
                     "device_tracker_id": self.config_entry.data["device_tracker_id"]}
            stop_limit = await _check_stop_list(self, _data)
            if stop_limit :
                return self.async_abort(reason=stop_limit)
            self._user_inputs.update(user_input)
            _LOGGER.debug(f"UserInputs Options Init: {self._user_inputs}")
            return self.async_create_entry(title="", data=self._user_inputs)

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

    async def _journey_options(self, user_input: dict[str, Any] | None) -> FlowResult:
        """A journey's options: how often it reads its timetable and the
        walking time. A train journey's stations to get on or off at as
        well too, which its entry keeps: the departure screen made them
        once, and a week of works asks for another one (SNCF: K8+ trains
        from Les Aubrais, not Orleans) on a sensor made long before."""
        errors: dict[str, str] = {}
        data = self.config_entry.data
        offered = await self._ends_offered(data)
        if user_input is not None:
            options = dict(user_input)
            also_at = options.pop(ALSO_AT, None) or []
            if offered:
                error = await self._keep_ends(data, offered, also_at)
                if error:
                    errors["base"] = error
            if not errors:
                self._user_inputs.update(options)
                _LOGGER.debug(f"UserInputs Options Init: {self._user_inputs}")
                return self.async_create_entry(title="", data=self._user_inputs)
        train = data.get(CONF_ROUTE_TYPE) == "2"
        opt1_schema = {
            vol.Optional(CONF_REFRESH_INTERVAL, default=self.config_entry.options.get(CONF_REFRESH_INTERVAL, DEFAULT_REFRESH_INTERVAL)): int,
            vol.Optional(CONF_OFFSET, default=self.config_entry.options.get(CONF_OFFSET, DEFAULT_OFFSET)): int,
            **(train_station_fields(data, offered, user_input) if train
               else stop_fields(data, offered, user_input)),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(opt1_schema),
            description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
            errors=errors,
        )

    async def _ends_offered(self, data: Mapping[str, Any]) -> list[str]:
        """The stations or stops to get on or off at as well of a journey's
        options: a train's stations between its two, a bus's or a tram's
        stops between its two, as their creation offered them. [] on a
        source that cannot be read."""
        await _reopen_schedule(self, dict(data))
        if isinstance(self._pygtfs, str):
            return []
        if data.get(CONF_ROUTE_TYPE) == "2":
            return await self.hass.async_add_executor_job(train_stations_between, self._pygtfs, data)
        return await self.hass.async_add_executor_job(
            get_stops_between, self._pygtfs, id_of(data.get(CONF_ROUTE)),
            id_of(data.get(CONF_ORIGIN)), id_of(data.get(CONF_DESTINATION)))

    async def _keep_ends(self, data: Mapping[str, Any], offered: list[str],
                         also_at: list[str]) -> str | None:
        """Keep on the entry the stations or stops ticked, the error that
        refuses them else. The entry's own data: the coordinator reads it
        at every refresh."""
        error: str | None = None
        if data.get(CONF_ROUTE_TYPE) == "2":
            error, stations = await self.hass.async_add_executor_job(
                kept_train_stations, self._pygtfs, data, also_at)
            new = {**data, **stations}
        else:
            new = kept_stops(data, offered, also_at)
        if not error and new != dict(data):
            self.hass.config_entries.async_update_entry(self.config_entry, data=new)
        return error


def _let_schedule_go(self: ConfigFlow | GTFSOptionsFlowHandler) -> None:
    """Let the datasource the flow opened go, however the flow ended.

    The flow opens the source's schedule to list its lines and stops,
    and held it to the end: never closed, the file stayed open until
    garbage collection, long enough on Windows to refuse the swap of a
    refresh started meanwhile.
    """
    if self._pygtfs and hasattr(self._pygtfs, "session"):
        self.hass.async_add_executor_job(close_schedule, self._pygtfs)
    self._pygtfs = ""


async def _reopen_schedule(self: ConfigFlow | GTFSOptionsFlowHandler, data: dict) -> None:
    """Let go of the schedule the flow holds and open the source's afresh:
    a refresh or an import may have put another file under its name."""
    close_schedule(self._pygtfs)
    self._pygtfs = await self.hass.async_add_executor_job(
        get_gtfs, self.hass, DEFAULT_PATH, data
    )


async def _check_stop_list(self: GTFSOptionsFlowHandler, data: dict) -> str | None:
    _LOGGER.debug("Checkstops option with data: %s", data)
    await _reopen_schedule(self, data)
    if isinstance(self._pygtfs, str):
        # no database to count in, or one being written: the options are
        # kept as they are, the count is what the sensor meets next time.
        # Handed to the query, the answer string raised
        _LOGGER.debug("Checkstops skipped, datasource answers %s", self._pygtfs)
        return None
    count_stops = await self.hass.async_add_executor_job(
                get_local_stop_list, self.hass, self._pygtfs, data
            )  
    # the limit the user just set on this screen, which the refusal tells
    # them to raise: compared with the default, raising it changed nothing
    if count_stops > int(data.get(CONF_MAX_LOCAL_STOPS) or DEFAULT_MAX_LOCAL_STOPS):
        _LOGGER.debug("Checkstops limit reached with: %s", count_stops)
        return "stop_limit_reached"
    return None         
