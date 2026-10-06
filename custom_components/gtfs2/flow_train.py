"""The train screens of the config flow: departure station, arrival station, options and sensor.

A train journey is configured by station name, from one line or from the
stations first. The departure station comes first, then the arrival
station, offered among the ones a train really reaches from there, then
the options (stations to get on or off at as well, the lines), and the
sensor screen that names the entry; several lines ticked make one entry
a line, named on a screen of their own. Mixed in ConfigFlow; every method
reads and writes the flow's own state (self).
"""
# mixin: The screens of a train journey: departure station, arrival station, options, sensor.
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    ALSO_AT,
    stations_of_both_ends,
    CONF_ADD_RETURN,
    CONF_DESTINATION,
    CONF_DESTINATION_STATIONS,
    CONF_DIRECTION,
    CONF_FILE,
    CONF_NAME,
    CONF_ORIGIN,
    CONF_ORIGIN_STATIONS,
    CONF_ROUTE,
    DEFAULT_PATH,
    DOMAIN,
    TRANSLATION_DESCRIPTION_PLACEHOLDERS,
    entry_lines,
)
from .datasource import check_datasource_index, get_gtfs
from .flow_journey import _Step
from .geojson import name_in_use
from .feed.files import close_schedule, feed_zip, real_path, routes_in
from .line_labels import _names_nothing
from .notifications import _async_text
from .stations import (
    RailIndex,
    get_line_code,
    get_station_list,
    get_station_modes,
    get_train_destination_list,
    get_train_lines_between,
    get_train_stations_between,
    rail_index,
    train_line_ends,
    train_routes_both_ways,
)

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)

# the route a train entry stores, and the one the route screen's "stations
# first" option carries: the journey rides whichever train serves its
# stations, held to the line it lists ("line"; none on the entries made
# before the lines were picked, and on a line the feed gives no code)
ALL_TRAINS = "train"


def _picked_route(route: str | None) -> str | None:
    """The route the train screens read the stations of, None for every
    rail line."""
    return None if route in (None, "", ALL_TRAINS) else route


def _options_schema(between: list[str], lines: dict[str, str], previous: dict) -> vol.Schema:
    """The options screen: the stations between to get on or off at as
    well, when there are some, and the lines, previous the answers shown."""
    def _many(options: list[selector.SelectOptionDict]) -> selector.SelectSelector:
        return selector.SelectSelector(selector.SelectSelectorConfig(
            options=options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN))

    stations = [selector.SelectOptionDict(value=name, label=name) for name in between]
    # the code is what the departures match, its long name what the rider
    # recognises: "K8+ (Paris - Orleans)"; a code that only says the line
    # has none ("INCONNU") gives way to the name
    line_options = [selector.SelectOptionDict(
        value=code, label=name if name and _names_nothing(code) else f"{code} ({name})" if name else code)
        for code, name in lines.items()]
    fields: dict[vol.Marker, Any] = {}
    if between:
        fields[vol.Optional(ALSO_AT, default=previous.get(ALSO_AT, []))] = _many(stations)
    fields[vol.Optional("lines", default=previous.get("lines", []))] = _many(line_options)
    return vol.Schema(fields)


def train_stations_between(schedule: Schedule, data: Mapping[str, Any]) -> list[str]:
    """The stations a train entry's options screen offers to get on or off
    at as well: those strictly between its two ends, on its line. Blocking,
    for the executor."""
    return get_train_stations_between(schedule, str(data.get(CONF_ORIGIN) or ""),
                                      str(data.get(CONF_DESTINATION) or ""), entry_lines(data) or None)


def train_station_fields(data: Mapping[str, Any], between: list[str],
                         previous: Mapping[str, Any] | None = None) -> dict[vol.Marker, Any]:
    """The field of a train entry's options screen, the stations it gets
    on or off at as well ticked: those of either end, an entry made when
    the screen asked the two apart included; {} when there is none to offer."""
    if not between:
        return {}
    held = [*(data.get(CONF_ORIGIN_STATIONS) or []), *(data.get(CONF_DESTINATION_STATIONS) or [])]
    ticked = [s for s in (previous or {}).get(ALSO_AT, held) if s in between]
    stations = [selector.SelectOptionDict(value=name, label=name) for name in between]
    return {vol.Optional(ALSO_AT, default=list(dict.fromkeys(ticked))): selector.SelectSelector(
        selector.SelectSelectorConfig(options=stations, multiple=True,
                                      mode=selector.SelectSelectorMode.DROPDOWN))}


def kept_train_stations(schedule: Schedule, data: Mapping[str, Any],
                        also_at: list[str]) -> tuple[str | None, dict[str, list[str]]]:
    """(error, the entry's stations) of a train entry's options screen:
    the stations of each end its line serves, of those ticked, as the
    creation keeps them (train_line_ends). Blocking, for the executor."""
    ends = stations_of_both_ends(str(data.get(CONF_ORIGIN) or ""), str(data.get(CONF_DESTINATION) or ""),
                                 also_at)
    origins, destinations = ends[CONF_ORIGIN_STATIONS], ends[CONF_DESTINATION_STATIONS]
    ons, offs = train_line_ends(schedule, origins, destinations, entry_lines(data) or None)
    if not ons or not offs:
        return "no_train_between", {}
    return None, {CONF_ORIGIN_STATIONS: ons, CONF_DESTINATION_STATIONS: offs}


def _station_label(name: str, modes: set[str] | None, words: dict[str, str]) -> str:
    """A station as the picker shows it. On a line that mixes trains and
    coaches every station says which of them call there, "Orléans (train,
    coach)", so a coach station reads as one; elsewhere the plain name."""
    if not modes:
        return name
    return f"{name} ({', '.join(words[m] for m in ('train', 'coach') if m in modes)})"


class TrainScreens:
    """The screens of a train journey: departure station, arrival station, options, sensor."""

    # what these screens use of the flow they are mixed in (ConfigFlow)
    hass: HomeAssistant
    _pygtfs: Schedule | str | None
    _user_inputs: dict
    _stops_error: str | None
    _return_trip: dict | None
    _return_name: str
    _route_label: str
    async_show_form: Callable[..., FlowResult]
    async_abort: Callable[..., FlowResult]
    _import_missing: str
    _import_routes: list
    _rail: tuple[str, RailIndex | None] | None
    _train_import: bool
    async_step_extracting: _Step
    async_step_importing: _Step
    _journey_placeholders: Callable[..., dict[str, str]]
    _suggested_name: Callable[..., str]
    _name_and_create: _Step
    _import_entry: Callable[[dict], Awaitable[str | None]]
    _created_name: str
    _train_plans: list[dict]
    async_step_finished: _Step

    async def _train_source(self) -> Schedule | RailIndex | str | None:
        """What the station screens read: from the stations first, the
        trains of the feed's zip (RailIndex), since the source holds only the lines asked
        for so far (.239: K6 and K8+, none of P8, K6+, K5+ or the
        Intercites from Orleans to Paris); on one line, the source itself,
        which holds it. The index is read once per source and flow, and the
        source stands in when the zip cannot be read."""
        if _picked_route(self._user_inputs.get(CONF_ROUTE)) is not None:
            return self._pygtfs
        file = self._user_inputs.get(CONF_FILE, "")
        if self._rail is None or self._rail[0] != file:
            path = feed_zip(self.hass.config.path(DEFAULT_PATH), file)
            index = await self.hass.async_add_executor_job(rail_index, path) if file else None
            self._rail = (file, index)
        return self._rail[1] or self._pygtfs

    async def _train_lines_missing(self, origin: str, destination: str) -> list[str]:
        """The lines riding between the two stations picked first, either
        way, that the source does not hold yet: imported before the options screen reads
        them. [] when nothing is missing, or nothing can tell (no index,
        a database that would not answer): the screens then read the
        source as it stands."""
        index = self._rail[1] if self._rail else None
        if index is None:
            return []
        wanted = await self.hass.async_add_executor_job(
            train_routes_both_ways, index, origin, destination)
        loaded = await self.hass.async_add_executor_job(
            routes_in, real_path(self.hass.config.path(DEFAULT_PATH), self._user_inputs.get(CONF_FILE, "")))
        if loaded is None:
            _LOGGER.warning("Cannot read the lines of %s, importing none for %s -> %s",
                            self._user_inputs.get(CONF_FILE), origin, destination)
            return []
        return [route_id for route_id in wanted if route_id not in loaded]

    async def _station_options(self, names: list[str],
                               modes: dict[str, set[str]]) -> list[selector.SelectOptionDict]:
        """Picker options for station names. On a line that mixes trains and
        coaches each one says which of them it is for; the value stays the
        plain name, which is what the queries match."""
        words = {mode: await _async_text(self.hass, f"mode_{mode}", mode)
                 for mode in ("train", "coach")}
        return [selector.SelectOptionDict(
            value=name, label=_station_label(name, modes.get(name), words))
            for name in names]

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
        # None from the stations first: every station a train takes riders
        # on at
        route_id = _picked_route(self._user_inputs.get(CONF_ROUTE))
        source = await self._train_source()
        stations = await self.hass.async_add_executor_job(
            get_station_list, source, route_id)
        if not stations:
            stations = await self.hass.async_add_executor_job(
                get_station_list, source)
        # a line that mixes trains and coaches says which one calls where:
        # "Paris-Austerlitz Routiere" alone does not read as a coach station
        modes = await self.hass.async_add_executor_job(
            get_station_modes, source, route_id)

        if user_input is None:
            picked = None
            if self._stops_error:
                # back from the arrival screen: keep the pick, say why. Or
                # the import that brought the line in left others out, and
                # nothing was picked yet
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
                description_placeholders=self._journey_placeholders(
                    missing=self._import_missing),
                errors=errors,
            )

        self._user_inputs[CONF_ORIGIN] = str(user_input.get(CONF_ORIGIN, "")).strip()
        _LOGGER.debug(f"UserInputs Stops Train: {self._user_inputs}")
        return await self.async_step_destination_train()

    async def _check_config(self, data: dict) -> str | None:
        schedule = await self.hass.async_add_executor_job(
            get_gtfs, self.hass, DEFAULT_PATH, data
        )
        if isinstance(schedule, str):
            # a sentinel of get_gtfs, not a schedule. It used to replace the
            # flow's own, and the screen shown again with the error then read
            # its stations from a string: the next submit ended the flow on
            # no_stops_read. The flow keeps the schedule it has
            return schedule
        close_schedule(self._pygtfs)
        self._pygtfs = schedule
        # check and/or add indexes
        await self.hass.async_add_executor_job(
                    check_datasource_index, self.hass, self._pygtfs, DEFAULT_PATH, data["file"]
                )
        # no departure is looked for: the arrival was offered from the trips
        # that ride it from the departure, so the journey exists (see
        # async_step_destination_train)
        return None

    async def async_step_destination_train(self, user_input: dict | None = None) -> FlowResult:
        """Pick the arrival station, among the ones a trip of the line really
        reaches from the departure station.

        A trip rides one mode from end to end, so a train station leads to the
        train arrivals and a coach station to the coach ones: a pair no trip
        rides cannot be picked, and nothing has to be rejected afterwards.
        """
        errors: dict[str, str] = {}
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        route_id = _picked_route(self._user_inputs.get(CONF_ROUTE))
        source = await self._train_source()
        try:
            # the line's own code, not the label shown for it: see get_line_code.
            # None from the stations first: every rail line reaches the arrivals
            line = (await self.hass.async_add_executor_job(get_line_code, self._pygtfs, route_id)
                    if route_id else None)
            reached = await self.hass.async_add_executor_job(
                get_train_destination_list, source, route_id, origin, line)
            mixed = await self.hass.async_add_executor_job(
                get_station_modes, source, route_id)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error reading the destinations from %s on route %s: %s",
                          origin, route_id, ex)
            return self.async_abort(reason="no_stops_read")
        if not reached:
            # a terminus, or a typed name no trip of the line calls at
            self._stops_error = "no_destination"
            return await self.async_step_stops_train()
        options = await self._station_options(list(reached), reached if mixed else {})

        def _show(errors: dict[str, str], picked: str | None = None) -> FlowResult:
            return self.async_show_form(
                step_id="destination_train",
                data_schema=vol.Schema({
                    vol.Required(CONF_DESTINATION, default=picked or vol.UNDEFINED):
                        selector.SelectSelector(
                            selector.SelectSelectorConfig(options=options)),
                }),
                description_placeholders=self._journey_placeholders(origin=origin),
                errors=errors,
            )

        if user_input is None:
            return _show(errors)
        destination = user_input[CONF_DESTINATION]
        data = {
            **self._user_inputs,
            CONF_DESTINATION: destination,
            # the picked line's code: the departures hold to that line
            "line": line,
        }
        # from the stations first, they came from the feed: the lines riding
        # between them, either way, that the source lacks are imported
        # first, and the options screen then reads them from the source
        # (ReloadScreens._reopen_after_import). A fresh source lacks them all
        missing = await self._train_lines_missing(origin, destination) if route_id is None else []
        if missing:
            self._user_inputs.update(data)
            self._user_inputs[CONF_DIRECTION] = "0"
            self._user_inputs[CONF_ROUTE] = ALL_TRAINS
            self._import_routes = missing
            self._train_import = True
            _LOGGER.debug("Lines to import for %s -> %s: %s", origin, destination, missing)
            return await self.async_step_importing()
        check_config = await self._check_config(data)
        if check_config == "extracting":
            # the datasource is being unpacked: nothing to correct here, the
            # progress screen waits for it as the other screens do
            self._user_inputs.update(data)
            return await self.async_step_extracting()
        # the arrival was offered from the trips that ride it from the
        # departure, so the journey exists; whether one is due in the next
        # hours is the coordinator's business: a sensor created on a day the
        # trains give way to coaches is still valid
        if check_config:
            _LOGGER.debug(f"CheckConfig: {check_config}")
            errors["base"] = check_config
            return _show(errors, destination)
        self._user_inputs.update(data)
        self._user_inputs[CONF_DIRECTION] = "0"
        self._user_inputs[CONF_ROUTE] = ALL_TRAINS
        _LOGGER.debug(f"UserInputs Destination Train: {self._user_inputs}")
        return await self.async_step_options_train()

    async def async_step_options_train(self, user_input: dict | None = None) -> FlowResult:
        """Stations to get on or off at as well, and the lines.

        A week of works ends trains of a line short of the station (SNCF,
        October 2026: K8+ trains from Les Aubrais, not Orleans), and puts
        others on the line under another code: a sensor getting on at Les
        Aubrais as well keeps them. Each line ticked is a sensor of its
        own: one sensor of several lines had one name, one colour and one
        route drawn for all of them, the next train's (a K6+ from Tours,
        by Les Aubrais, took Orleans off the map). The line picked on the
        route screen is ticked; from the stations first, every one is.
        """
        errors: dict[str, str] = {}
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        picked = self._user_inputs.get("line")
        try:
            between = await self.hass.async_add_executor_job(
                get_train_stations_between, self._pygtfs, origin, destination)
            lines = await self.hass.async_add_executor_job(
                get_train_lines_between, self._pygtfs, origin, destination, between, between)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error reading the options from %s to %s: %s", origin, destination, ex)
            return self.async_abort(reason="no_stops_read")
        if picked and picked not in lines:
            lines = {picked: "", **lines}
        ticked = [picked] if picked else list(lines)
        # the import the arrival screen ran left lines out: said once, here,
        # the first screen after it, even with nothing to choose
        missing = "" if user_input is not None else self._import_missing
        self._import_missing = ""
        if missing:
            errors["base"] = "import_partial"

        def _show(errors: dict[str, str], previous: dict | None = None) -> FlowResult:
            return self.async_show_form(
                step_id="options_train",
                data_schema=_options_schema(between, lines, {"lines": ticked, **(previous or {})}),
                description_placeholders=self._journey_placeholders(
                    origin=origin, destination=destination, missing=missing),
                errors=errors,
            )

        if user_input is None and (between or len(lines) > 1 or missing):
            return _show(errors)
        # nothing to choose, one line and no station in between: as it stands
        user_input = user_input or {"lines": ticked}
        also_at = [s for s in user_input.get(ALSO_AT) or [] if s in between]
        chosen = [line for line in user_input.get("lines") or [] if line in lines]
        ends = stations_of_both_ends(origin, destination, also_at)
        origins, destinations = ends[CONF_ORIGIN_STATIONS], ends[CONF_DESTINATION_STATIONS]
        if lines and not chosen:
            # a line the feed gives no code offers none, and holds to none
            return _show({"base": "no_line_ticked"}, user_input)
        self._train_plans = await self._line_plans(origins, destinations, chosen or [None], picked)
        if not self._train_plans:
            # no train of the lines ticked rides from one end to the other
            return _show({"base": "no_train_between"}, user_input)
        if len(self._train_plans) == 1:
            self._take_plan(self._train_plans[0])
            return await self.async_step_sensor_train()
        return await self.async_step_sensors_train()

    async def _line_plans(self, origins: list[str], destinations: list[str],
                          chosen: list[str | None], picked: str | None) -> list[dict]:
        """The entry of each line ticked, and its return: the stations of
        each end its trains serve, of those ticked, and its name.

        The departure and the arrival picked lead where the line serves
        them; elsewhere the first station it serves stands in: a line ticked
        from Orleans that only leaves from Les Aubrais is a sensor from Les
        Aubrais. The return
        is the same line the other way round, read on its own: a train
        ending at Les Aubrais out may start there back, and a line may run
        one way only under its code (SNCF: K8+ out, P8 back).
        """
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        plans = []
        for line in chosen:
            ons, offs = await self.hass.async_add_executor_job(
                train_line_ends, self._pygtfs, origins, destinations, line)
            if not ons or not offs:
                continue
            start = origin if origin in ons else ons[0]
            end = destination if destination in offs else offs[0]
            # the line picked on the route screen keeps that screen's label,
            # a line with no code included; a line ticked, its code
            label = self._route_label if line == picked else str(line or "")
            plan: dict[str, Any] = {
                "label": label,
                "line": line,
                "lines": [line] if line else [],
                CONF_ORIGIN: start,
                CONF_DESTINATION: end,
                CONF_ORIGIN_STATIONS: ons,
                CONF_DESTINATION_STATIONS: offs,
                CONF_NAME: self._suggested_name(f"{start} → {end}", label),
                "return": {},
            }
            back_ons, back_offs = await self.hass.async_add_executor_job(
                train_line_ends, self._pygtfs, destinations, origins, line)
            if back_ons and back_offs:
                back_start = end if end in back_ons else back_ons[0]
                back_end = start if start in back_offs else back_offs[0]
                # merged over the outward's at creation: what differs
                plan["return"] = {
                    CONF_ORIGIN: back_start,
                    CONF_DESTINATION: back_end,
                    CONF_ORIGIN_STATIONS: back_ons,
                    CONF_DESTINATION_STATIONS: back_offs,
                    CONF_NAME: self._suggested_name(f"{back_start} → {back_end}", label),
                }
            plans.append(plan)
        _LOGGER.debug("Train entries of %s -> %s: %s", origin, destination, plans)
        return plans

    @staticmethod
    def _plan_entry(plan: dict) -> dict:
        """What an entry of the plan holds beyond the source's choices."""
        return {key: plan[key] for key in ("line", "lines", CONF_ORIGIN, CONF_DESTINATION,
                                           CONF_ORIGIN_STATIONS, CONF_DESTINATION_STATIONS)}

    def _take_plan(self, plan: dict) -> None:
        """The one entry of the plan into the flow, named on the sensor screen."""
        self._user_inputs.update(self._plan_entry(plan))
        self._route_label = plan["label"]
        self._return_trip = plan["return"]
        self._return_name = plan["return"].get(CONF_NAME, "")
        _LOGGER.debug(f"UserInputs Options Train: {self._user_inputs}")

    async def async_step_sensor_train(self, user_input: dict | None = None) -> FlowResult:
        """Name the train sensor, suggested from the line and both stations."""
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        trip = f"{origin} → {destination}"
        # the source leads, like the other sensors' names
        suggested = self._suggested_name(trip)
        return await self._name_and_create("sensor_train", user_input, suggested, trip,
                                           add_return=False)

    async def async_step_sensors_train(self, user_input: dict | None = None) -> FlowResult:
        """The sensors of the lines ticked, each named from its line and its
        stations, and their returns when asked for.

        No name to type: one field a sensor is a form nobody fills twice.
        A name already in use is a sensor already there, for that line and
        those stations: it is left as it is, and the others are made.
        """
        errors: dict[str, str] = {}
        plans = self._train_plans
        returns = [plan["return"] for plan in plans if plan["return"]]

        def _show(errors: dict[str, str]) -> FlowResult:
            return self.async_show_form(
                step_id="sensors_train",
                data_schema=vol.Schema({vol.Optional(CONF_ADD_RETURN, default=False):
                                        selector.BooleanSelector()} if returns else {}),
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "sensors": "\n".join(f"- {plan[CONF_NAME]}" for plan in plans),
                    "returns": "\n".join(f"- {back[CONF_NAME]}" for back in returns) or "-",
                },
                errors=errors,
            )

        if user_input is None:
            return _show(errors)
        taken = {e.data.get(CONF_NAME) for e in self.hass.config_entries.async_entries(DOMAIN)}
        created: list[str] = []
        for plan in plans:
            outward = {**self._user_inputs, **self._plan_entry(plan), CONF_NAME: plan[CONF_NAME]}
            wanted = [outward]
            if user_input.get(CONF_ADD_RETURN) and plan["return"]:
                wanted.append({**outward, **plan["return"]})
            for data in wanted:
                if name_in_use(data[CONF_NAME], taken):
                    _LOGGER.debug("Train sensor %s is there already", data[CONF_NAME])
                    continue
                refused = await self._import_entry(data)
                if refused:
                    _LOGGER.warning("The sensor %s was not created: %s", data[CONF_NAME], refused)
                    continue
                created.append(data[CONF_NAME])
                taken.add(data[CONF_NAME])
        if not created:
            errors["base"] = "not_created"
            return _show(errors)
        self._created_name = ", ".join(created)
        return await self.async_step_finished()
