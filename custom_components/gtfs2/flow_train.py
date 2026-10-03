"""The train screens of the config flow: departure station, arrival station and sensor.

A train journey is configured by station name. The departure station
comes first, then the arrival station, offered among the ones a train
really reaches from there, and the sensor screen that names the entry.
Mixed in ConfigFlow; every method reads and writes the flow's own state
(self).
"""
# mixin: The three screens of a train journey: departure station, arrival station, sensor.
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DESTINATION,
    CONF_DIRECTION,
    CONF_NAME,
    CONF_ORIGIN,
    CONF_ROUTE,
    DEFAULT_PATH,
)
from .datasource import check_datasource_index, get_gtfs
from .flow_journey import _Step
from .gtfs_db import close_schedule
from .notifications import _async_text
from .stations import (
    get_line_code,
    get_station_list,
    get_station_modes,
    get_train_destination_list,
    has_train_trip_between,
)

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


def _station_label(name: str, modes: set[str] | None, words: dict[str, str]) -> str:
    """A station as the picker shows it. On a line that mixes trains and
    coaches every station says which of them call there, "Orléans (train,
    coach)", so a coach station reads as one; elsewhere the plain name."""
    if not modes:
        return name
    return f"{name} ({', '.join(words[m] for m in ('train', 'coach') if m in modes)})"


class TrainScreens:
    """The three screens of a train journey: departure station, arrival station, sensor."""

    # what these screens use of the flow they are mixed in (ConfigFlow)
    hass: HomeAssistant
    _pygtfs: Schedule | str | None
    _user_inputs: dict
    _stops_error: str | None
    _return_trip: dict | None
    _return_name: str
    async_show_form: Callable[..., FlowResult]
    async_abort: Callable[..., FlowResult]
    _import_missing: str
    async_step_extracting: _Step
    _journey_placeholders: Callable[..., dict[str, str]]
    _suggested_name: Callable[[str], str]
    _name_and_create: _Step

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
        route_id = self._user_inputs.get(CONF_ROUTE)
        try:
            # the line's own code, not the label shown for it: see get_line_code
            line = await self.hass.async_add_executor_job(get_line_code, self._pygtfs, route_id)
            reached = await self.hass.async_add_executor_job(
                get_train_destination_list, self._pygtfs, route_id, origin, line)
            mixed = await self.hass.async_add_executor_job(
                get_station_modes, self._pygtfs, route_id)
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
        self._user_inputs[CONF_ROUTE] = "train"
        _LOGGER.debug(f"UserInputs Destination Train: {self._user_inputs}")
        return await self.async_step_sensor_train()

    async def async_step_sensor_train(self, user_input: dict | None = None) -> FlowResult:
        """Name the train sensor, suggested from the line and both stations."""
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        # the outward keeps the line picked in the flow; the return may run
        # under its own code (SNCF: K8+ out, P8 back), so its line is read
        # from the schedule for that very direction
        trip = f"{origin} → {destination}"
        # the source leads, like the other sensors' names
        suggested = self._suggested_name(trip)
        if self._return_trip is None:
            # trains rarely run one way only, but check before offering.
            # A train sensor covers the station pair, not one line, so the
            # return wears the same label as the outward.
            self._return_name = self._suggested_name(f"{destination} → {origin}")
            exists = await self.hass.async_add_executor_job(
                has_train_trip_between, self._pygtfs, destination, origin,
                self._user_inputs.get("line"),
            )
            self._return_trip = {
                CONF_ORIGIN: destination,
                CONF_DESTINATION: origin,
                CONF_NAME: self._return_name,
            } if exists else {}
        return await self._name_and_create("sensor_train", user_input, suggested, trip,
                                           add_return=False)
