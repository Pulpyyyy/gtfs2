"""The train screens of the config flow: arrival station and sensor.

A train journey is configured by station name. After the departure
station (async_step_stops_train, in config_flow with the other stop
screens) come the arrival station, offered among the ones a train really
reaches from there, and the sensor screen that names the entry. Mixed in
ConfigFlow; every method reads and writes the flow's own state (self).
"""
# mixin: The two screens after the departure station of a train journey.
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_DESTINATION,
    CONF_DIRECTION,
    CONF_FILE,
    CONF_NAME,
    CONF_ORIGIN,
    CONF_ROUTE,
)
from .notifications import _async_text
from .stations import get_line_code, get_station_modes, get_train_destination_list, has_train_trip_between

_LOGGER = logging.getLogger(__name__)


def _station_label(name, modes, words):
    """A station as the picker shows it. On a line that mixes trains and
    coaches every station says which of them call there, "Orléans (train,
    coach)", so a coach station reads as one; elsewhere the plain name."""
    if not modes:
        return name
    return f"{name} ({', '.join(words[m] for m in ('train', 'coach') if m in modes)})"


class TrainScreens:
    """The two screens after the departure station of a train journey."""

    async def _station_options(self, names, modes):
        """Picker options for station names. On a line that mixes trains and
        coaches each one says which of them it is for; the value stays the
        plain name, which is what the queries match."""
        words = {mode: await _async_text(self.hass, f"mode_{mode}", mode)
                 for mode in ("train", "coach")}
        return [selector.SelectOptionDict(
            value=name, label=_station_label(name, modes.get(name), words))
            for name in names]

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

        def _show(errors, picked=None):
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
        line = self._route_label
        trip = f"{origin} → {destination}"
        # the source leads, like the other sensors' names
        source = self._user_inputs.get(CONF_FILE)
        suggested = " ".join(filter(None, (source, line, trip)))
        if self._return_trip is None:
            # trains rarely run one way only, but check before offering.
            # A train sensor covers the station pair, not one line, so the
            # return wears the same label as the outward.
            back = f"{destination} → {origin}"
            self._return_name = " ".join(filter(None, (source, line, back)))
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
