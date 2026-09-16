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

from homeassistant import config_entries
from homeassistant import data_entry_flow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_ADD_RETURN,
    CONF_DESTINATION,
    CONF_DIRECTION,
    CONF_NAME,
    CONF_ORIGIN,
    CONF_ROUTE,
    DOMAIN,
    TRANSLATION_DESCRIPTION_PLACEHOLDERS,
)
from .notifications import _async_text
from .stations import get_station_modes, get_train_destination_list, has_train_trip_between

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
            reached = await self.hass.async_add_executor_job(
                get_train_destination_list, self._pygtfs, route_id, origin,
                self._route_label or None)
            mixed = await self.hass.async_add_executor_job(
                get_station_modes, self._pygtfs, route_id)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Error reading the destinations from %s on route %s: %s",
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
            "line": self._route_label,
        }
        check_config = await self._check_config(data)
        # the arrival was offered from the trips that ride it from the
        # departure, so the journey exists; whether one is due in the next
        # hours is the coordinator's business: a sensor created on a day the
        # trains give way to coaches is still valid
        if check_config and check_config != "stop_incorrect":
            _LOGGER.debug(f"CheckConfig: {check_config}")
            errors["base"] = check_config
            return _show(errors, destination)
        self._user_inputs.update(data)
        self._user_inputs[CONF_DIRECTION] = 0
        self._user_inputs[CONF_ROUTE] = "train"
        _LOGGER.debug(f"UserInputs Destination Train: {self._user_inputs}")
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
            # spawned as its own flow, like the outward below
            await self._create_return_trip()
        self._user_inputs.update(user_input)
        # async_create_entry ends the flow, so the sensor is created through a
        # second flow, like the bus sensor. That leaves this one alive to offer
        # what comes next, another journey on the same line among it.
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
