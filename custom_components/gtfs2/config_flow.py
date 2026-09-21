"""ConfigFlow for GTFS integration."""
from __future__ import annotations

import logging
from functools import partial
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
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
    CONF_LOOP_DIRECTION,
    CONF_ORIGIN,
    CONF_DESTINATION,
    CONF_NAME,
    CONF_LOCAL_STOP_REFRESH_INTERVAL,
    CONF_RADIUS,
    CONF_TIMERANGE,
    CONF_REFRESH_INTERVAL,
    CONF_OFFSET,
    CONF_REAL_TIME,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    ATTR_API_KEY_LOCATIONS,
    DEFAULT_MAX_LOCAL_STOPS,
    CONF_MAX_LOCAL_STOPS,
)

from .gtfs_helper import (
    get_gtfs,
    get_next_departure,
    get_route_list,
    get_stop_list,
    get_destination_stop_list,
    get_pair_direction,
    get_towards,
    get_datasources,
    remove_datasource,
    check_datasource_index,
    get_agency_list,
    get_local_stop_list,
)
from .stations import get_station_list, get_station_modes
from .route_names import get_route_options_from_zip, get_agencies_in_zip, LINE_MODES, with_modes
from .notifications import _async_text

from .rt_source import (
    RT_OPTION_KEYS,
    datasource_entry,
)
from .const import CONF_NEEDS_API_KEY, TRANSLATION_DESCRIPTION_PLACEHOLDERS
from .flow_train import TrainScreens
from .flow_reload import ReloadScreens
from .flow_source import _source_rt_schema, _collect_source_rt_options
from .flow_source import SourceScreens
from .flow_options import OptionsScreens
from .flow_journey import _stop_id, _stop_name, _base_name
from .flow_journey import JourneyScreens

_LOGGER = logging.getLogger(__name__)


def _stop_options(stops):
    """Picker options for "stop_id: Name (sequence)" entries.

    The value must stay the entry, get_next_departure cuts the id back out of
    it; only the readable part is the rider's to see. Two places of one name
    already carry their station or their rank in it (gtfs_helper._labels_of),
    so the label is that part alone, without the id and the sequence.
    """
    return [selector.SelectOptionDict(value=entry, label=_stop_name(entry))
            for entry in stops]


@config_entries.HANDLERS.register(DOMAIN)
class ConfigFlow(JourneyScreens, SourceScreens, ReloadScreens, TrainScreens, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GTFS."""

    VERSION = 10

    def __init__(self) -> None:
        """Init ConfigFlow."""
        self._pygtfs = ""
        self._data: dict[str, str] = {}
        self._user_inputs: dict = {}
        # the way the rider leaves the origin, when it was asked: it narrows
        # the destination screen and picks a loop's rotation, the entry does
        # not keep it
        self._towards = None
        self._pending_error: str | None = None
        # why the arrival screen sent the rider back to the departure one
        self._stops_error: str | None = None
        self._extract_job = None
        self._extract_task = None
        self._extract_next_step: str | None = None
        self._route_label: str = ""
        # the directions as the direction screen offered them, and the one
        # picked, recalled on the screens that follow
        self._direction_labels: dict = {}
        self._direction_label: str = ""
        # the line as the route screen showed it, recalled on the stop screens
        self._route_shown: str = ""
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
        # the line and direction picked, where another journey on the same
        # line starts from once this one is created
        self._line: dict | None = None

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
            errors: dict[str, str] = {}
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
                # anything typed on this step. user_input is None on the way in,
                # and on a re-show after an error, where it is blanked above.
                if check_data == "extracting":
                    self._user_inputs.update(user_input or {})
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
            # the mode goes after the label where lines of one number differ
            words = {mode: await _async_text(self.hass, f"line_mode_{mode}", mode)
                     for mode in LINE_MODES}
            route_list = [
                # value carries route_type##route_id, label is the readable part
                selector.SelectOptionDict(value=r, label=label)
                for r, label in zip(usable, with_modes(usable, words))
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
        _picked = str(user_input.get(CONF_ROUTE) or "").split('##')
        if len(_picked) < 2 or not _picked[1]:
            # the field takes typed text, which is what makes a long list
            # searchable; text that is not one of the lines left the value
            # without its route and the step raised. The list comes back,
            # saying so
            self._pending_error = "route_not_listed"
            return await self.async_step_route()
        user_input[CONF_ROUTE_TYPE] = _picked[0]
        user_input[CONF_ROUTE] = _picked[1]
        # the readable part is only used to suggest a sensor name
        self._route_label = _picked[2].split(" : ")[0].split(" · ")[0] if len(_picked) > 2 else ""
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


    async def async_step_stops(self, user_input: dict | None = None) -> FlowResult:
        """Pick the origin: every place the line rides, both ways round."""
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                stops = await self.hass.async_add_executor_job(
                    get_stop_list,
                    self._pygtfs,
                    self._user_inputs[CONF_ROUTE],
                    None,
                )
            except Exception as ex:  # pylint: disable=broad-except
                # a bare except here reported every failure as "no stops",
                # a locked database and a bad route id included
                _LOGGER.error("Error reading the stops of route %s: %s",
                              self._user_inputs.get(CONF_ROUTE), ex)
                return self.async_abort(reason="no_stops_read")
            if not stops:
                _LOGGER.debug("No stops for route: %s", self._user_inputs.get(CONF_ROUTE))
                return self.async_abort(reason="no_stops")
            return self.async_show_form(
                step_id="stops",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ORIGIN): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=_stop_options(stops))
                        ),
                    },
                ),
                description_placeholders=self._journey_placeholders(),
                errors=errors,
            )

        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Origin: {self._user_inputs}")
        self._towards = None
        return await self.async_step_towards()

    async def async_step_towards(self, user_input: dict | None = None) -> FlowResult:
        """Ask which way the rider leaves the origin, only when trips from it
        really leave both ways: the destination screen then shows that side
        only, nearest first, and at a loop's terminus the answer is the
        rotation the entry keeps. At the end of a line, where every trip
        leaves the same way, nothing is asked."""
        origin = self._user_inputs[CONF_ORIGIN]
        try:
            ways = await self.hass.async_add_executor_job(
                get_towards,
                self._pygtfs,
                self._user_inputs[CONF_ROUTE],
                _stop_id(origin),
            )
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Error reading the ways out of %s on route %s: %s",
                          _stop_id(origin), self._user_inputs.get(CONF_ROUTE), ex)
            return self.async_abort(reason="no_stops_read")
        if not ways:
            return await self.async_step_destination()
        if user_input is None:
            return self.async_show_form(
                step_id="towards",
                data_schema=vol.Schema(
                    {
                        vol.Required("towards", default=ways[0][0]): vol.In(dict(ways)),
                    },
                ),
                description_placeholders=self._journey_placeholders(
                    origin=_base_name(origin)),
            )
        self._towards = user_input["towards"]
        return await self.async_step_destination()

    async def async_step_destination(self, user_input: dict | None = None) -> FlowResult:
        """Pick the destination among the stops a trip really rides to from
        the chosen origin, so the pair can always be matched to a trip. The
        sensor screen follows: the name, and whether to add the way back."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._user_inputs.update(user_input)
            # the pair says which way the rider goes, except with a loop's
            # terminus at one end: then the entry keeps the rotation the
            # rider answered, or the shorter one
            self._user_inputs[CONF_LOOP_DIRECTION] = await self.hass.async_add_executor_job(
                get_pair_direction,
                self._pygtfs,
                self._user_inputs[CONF_ROUTE],
                _stop_id(self._user_inputs[CONF_ORIGIN]),
                _stop_id(self._user_inputs[CONF_DESTINATION]),
                self._towards,
            )
            _LOGGER.debug(f"UserInputs Destination: {self._user_inputs}")
            return await self.async_step_sensor()

        destinations = await self.hass.async_add_executor_job(
            get_destination_stop_list,
            self._pygtfs,
            self._user_inputs[CONF_ROUTE],
            None,
            _stop_id(self._user_inputs[CONF_ORIGIN]),
            self._towards,
        )
        if not destinations:
            # the origin is the last stop of every trip that calls at it
            return self.async_abort(reason="no_destination")
        return self.async_show_form(
            step_id="destination",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DESTINATION, default=destinations[-1]): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=_stop_options(destinations))
                    ),
                },
            ),
            description_placeholders=self._journey_placeholders(
                origin=_base_name(self._user_inputs[CONF_ORIGIN])),
            errors=errors,
        )


    async def async_step_stops_train(self, user_input: dict | None = None) -> FlowResult:
        """Pick the departure station of a train journey.

        Rail feeds rarely have stop ids a rider can use, and a station is
        several stops in GTFS, so the distinct names are offered, and a name
        can be typed for feeds that list none. One station only: an operator
        can run the coaches that replace its trains from a coach station the
        feed files under a name of its own (SNCF K8+: "Paris-Austerlitz
        Routiere" beside "Paris Austerlitz"), and nothing in the feed ties the
        two together. That coach station is the departure of a journey of its
        own, which the closing screen offers to add on the same line.
        """
        errors: dict[str, str] = {}
        route_id = self._user_inputs.get(CONF_ROUTE)
        stations = await self.hass.async_add_executor_job(
            get_station_list, self._pygtfs, route_id)
        if not stations:
            stations = await self.hass.async_add_executor_job(
                get_station_list, self._pygtfs)
        # a line that mixes trains and coaches says which one calls where:
        # "Paris-Austerlitz Routiere" alone does not read as a coach station
        modes = await self.hass.async_add_executor_job(
            get_station_modes, self._pygtfs, route_id)

        if user_input is None:
            picked = None
            if self._stops_error:
                # back from the arrival screen: keep the pick, say why
                errors["base"], self._stops_error = self._stops_error, None
                picked = self._user_inputs.get(CONF_ORIGIN)
            return self.async_show_form(
                step_id="stops_train",
                data_schema=vol.Schema({
                    vol.Required(CONF_ORIGIN, default=picked or vol.UNDEFINED):
                        selector.SelectSelector(selector.SelectSelectorConfig(
                            options=await self._station_options(stations, modes),
                            custom_value=True)),
                }),
                description_placeholders=self._journey_placeholders(),
                errors=errors,
            )

        self._user_inputs[CONF_ORIGIN] = str(user_input.get(CONF_ORIGIN, "")).strip()
        _LOGGER.debug(f"UserInputs Stops Train: {self._user_inputs}")
        return await self.async_step_destination_train()


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


class GTFSOptionsFlowHandler(OptionsScreens, config_entries.OptionsFlow):
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
            return await self.async_step_source_menu()
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
    # the limit the user just set on this screen, which the refusal tells
    # them to raise: compared with the default, raising it changed nothing
    if count_stops > int(data.get(CONF_MAX_LOCAL_STOPS) or DEFAULT_MAX_LOCAL_STOPS):
        _LOGGER.debug("Checkstops limit reached with: %s", count_stops)
        return "stop_limit_reached"
    return None         
