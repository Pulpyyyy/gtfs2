"""The end of a journey's setup, and what follows one.

Once the line is picked the fork settles the direction from the stops
rather than asking for it, names the sensor and creates it through an
import flow, offers the mirror journey, and on the closing screen the
next thing to do: another journey on the same line, another source, an
optimise. The helpers read a stop option back into its id and its plain
name. Mixed in ConfigFlow; every method reads and writes the flow's own
state (self).
"""
# mixin: From the direction to the closing screen: sensor, mirror journey, same line, finish, import.
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from sqlalchemy.exc import SQLAlchemyError

from .const import (
    CONF_ADD_RETURN,
    CONF_AGENCY,
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    CONF_DESTINATION,
    CONF_DIRECTION,
    CONF_EXTRACT_FROM,
    CONF_FILE,
    CONF_KIND,
    CONF_LOOP_DIRECTION,
    CONF_NAME,
    CONF_ORIGIN,
    CONF_ROUTE,
    CONF_ROUTE_TYPE,
    CONF_URL,
    DOMAIN,
    ENTRY_KIND_DATASOURCE,
    TRANSLATION_DESCRIPTION_PLACEHOLDERS,
    id_of,
)
from .geojson import name_in_use
from .rt_source import datasource_unique_id
from .source_refresh import source_zip_url
from .pair_direction import get_direction_labels, get_pair_direction, has_trip_between

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)

# a screen of the flow as another one calls it, async_step_<id>(user_input),
# or a helper that ends on one: what a mixin declares of the screens it
# hands over to
type _Step = Callable[..., Coroutine[Any, Any, FlowResult]]


def _stop_name(entry: str) -> str:
    """The readable part of a "stop_id: Name (sequence)" entry.

    Ids carry colons of their own but never ": ", which names do ("A28:
    Kala's (East Bound)"): cut at the first one, as id_of does.
    """
    return entry.split(": ", 1)[-1].rsplit(" (", 1)[0].strip()


def _base_name(entry: str) -> str:
    """_stop_name without the flow's own " #n" disambiguation suffix.

    The suffixed name is what the pickers and the by-name matching need;
    a sensor name is for reading, so the suffix goes.
    """
    return re.sub(r" #\d+$", "", _stop_name(entry))


class JourneyScreens:
    """From the direction to the closing screen: sensor, mirror journey, same line, finish, import."""

    # what these screens use of the flow they are mixed in (ConfigFlow)
    hass: HomeAssistant
    _pygtfs: Schedule | str | None
    _user_inputs: dict
    _route_label: str
    _route_shown: str
    _return_trip: dict | None
    _return_name: str
    _created_name: str
    _line: dict
    async_show_form: Callable[..., FlowResult]
    async_show_menu: Callable[..., FlowResult]
    async_abort: Callable[..., FlowResult]
    async_create_entry: Callable[..., FlowResult]
    async_set_unique_id: Callable[..., Coroutine[Any, Any, object]]
    _abort_if_unique_id_configured: Callable[..., None]
    async_step_stops: _Step
    async_step_stops_train: _Step

    def _journey_placeholders(self, **extra: str) -> dict[str, str]:
        """The line picked so far, recalled at the top of the screens that
        pick the stops."""
        return {
            **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
            "route": self._route_shown or self._route_label or str(self._user_inputs.get(CONF_ROUTE, "")),
            **extra,
        }

    async def async_step_direction(self, user_input: dict | None = None) -> FlowResult:
        """Settle the direction before the stops, without asking for it.

        The rider picks where they are and where they go, and the order of
        the stops on a trip says which way that is; direction_id could not
        say it, a feed files trips under either label. The one pair a loop
        leaves open is settled once the destination is known, and the entry
        keeps that rotation alone.

        Not asked on a rail line either: the train path reads both directions
        (the stations by name, the arrivals from the departure, the departures
        matched by name with no direction). GTFS route_type 2 is rail: those
        feeds rarely have usable stop ids, so they are matched on station
        names instead of picked from a list.
        """
        if self._user_inputs.get(CONF_ROUTE_TYPE) == "2":
            self._user_inputs[CONF_DIRECTION] = "0"
            self._keep_line()
            return await self.async_step_stops_train()
        self._user_inputs[CONF_DIRECTION] = None
        self._keep_line()
        _LOGGER.debug(f"UserInputs Direction: {self._user_inputs}")
        return await self.async_step_stops()

    def _keep_line(self) -> None:
        """Remember the line and direction picked: another journey on the
        same line starts from there, offered once this one is created."""
        self._line = {
            "inputs": dict(self._user_inputs),
            "route_label": self._route_label,
            "route_shown": self._route_shown,
        }

    async def async_step_sensor(self, user_input: dict | None = None) -> FlowResult:
        """Name the sensor, now that both stops are known."""
        origin = self._user_inputs.get(CONF_ORIGIN, "")
        destination = self._user_inputs.get(CONF_DESTINATION, "")
        trip = await self._trip_name(origin, destination,
                                     self._user_inputs.get(CONF_LOOP_DIRECTION))
        if self._return_trip is None:
            await self._find_return_trip(origin, destination)
        return await self._name_and_create("sensor", user_input, self._suggested_name(trip),
                                           trip, add_return=True)

    async def _trip_name(self, origin: str, destination: str,
                         loop_direction: str | int | None) -> str:
        """origin → destination by their base names. A circular line reads
        the same at both ends, and the return's plain ends would collide
        with the outward sensor's name: its rotation is named by where it
        heads first."""
        trip = f"{_base_name(origin)} → {_base_name(destination)}"
        if _base_name(origin) == _base_name(destination) and loop_direction is not None:
            labels = await self.hass.async_add_executor_job(
                get_direction_labels, self._pygtfs, self._user_inputs[CONF_ROUTE])
            trip = labels.get(str(loop_direction), "") or trip
        return trip

    def _suggested_name(self, trip: str) -> str:
        """The name offered for a sensor of this trip: source, line, trip."""
        # the source leads, so the entity id tells line 1 of one network
        # from line 1 of another: sensor.gtfs_idfm_14_...
        return " ".join(filter(None, (self._user_inputs.get(CONF_FILE), self._route_label, trip)))

    async def _name_and_create(self, step_id: str, user_input: dict | None, suggested: str,
                               trip: str, add_return: bool) -> FlowResult:
        """The naming screen of a journey, bus or train: the name, and the
        way back when there is one (ticked by default when add_return), then
        the sensor created and the closing screen."""
        errors: dict[str, str] = {}

        def _show(errors: dict[str, str], previous: dict | None = None) -> FlowResult:
            previous = previous or {}
            fields: dict[vol.Marker, Any] = {
                vol.Required(
                    CONF_NAME, default=previous.get(CONF_NAME, suggested)
                ): str,
                **({vol.Optional(
                    CONF_ADD_RETURN, default=add_return
                ): selector.BooleanSelector()} if self._return_trip else {}),
            }
            return self.async_show_form(
                step_id=step_id,
                data_schema=vol.Schema(fields),
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
        taken = {e.data.get(CONF_NAME) for e in self.hass.config_entries.async_entries(DOMAIN)}
        if name_in_use(user_input[CONF_NAME], taken):
            errors["base"] = "name_taken"
            return _show(errors, user_input)
        # the return's own name, checked the same way and before anything is
        # created: it was never looked at, so a taken one was dropped with a
        # warning in the log the rider never read, after the rider had asked
        # for it
        return_name = (self._return_trip or {}).get(CONF_NAME)
        if (add_return and return_name is not None
                and name_in_use(return_name, taken | {user_input[CONF_NAME]})):
            errors["base"] = "return_name_taken"
            return _show(errors, user_input)
        self._user_inputs.update(user_input)
        _LOGGER.debug(f"UserInputs Sensor: {self._user_inputs}")
        # the arrival was offered from the trips that ride it from the
        # departure, so the journey exists; whether a bus is due right now is
        # the coordinator's business: a sensor created in the evening, or on
        # a day the line does not run, is still valid
        # async_create_entry ends the flow, so the sensor is created through a
        # second flow, the same way the return journey already is. That leaves
        # this one alive to offer what comes next.
        # The second flow can still refuse - a unique_id taken between the
        # check above and here, or an import step that aborts - and announcing
        # a sensor that was never created would send the user looking for it
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
        # the return only once the journey itself exists: created first, as it
        # was, a refused journey left its return behind on its own
        if add_return:
            await self._create_return_trip()
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
                menu_options=(["same_line"] if self._line else [])
                + ["start_end", "optimise", "finish"],
                description_placeholders={
                    **TRANSLATION_DESCRIPTION_PLACEHOLDERS,
                    "name": self._created_name,
                },
            )
        return await self.async_step_finish()

    async def async_step_same_line(self, user_input: dict | None = None) -> FlowResult:
        """Another journey on the line just used: the source, the line and
        the direction stay, and the departure screen opens.

        A train line's replacement coaches can leave from a coach station the
        feed names on its own, "Paris-Austerlitz Routiere" beside "Paris
        Austerlitz" on the SNCF K8+: a journey of its own reads them, picked
        from that station.
        """
        self._reset_for_next_journey()
        self._user_inputs.update(self._line["inputs"])
        # the previous journey's loop rotation belongs to its own pair
        self._user_inputs.pop(CONF_LOOP_DIRECTION, None)
        self._user_inputs[CONF_DIRECTION] = None
        self._route_label = self._line["route_label"]
        self._route_shown = self._line.get("route_shown", "")
        if self._user_inputs.get(CONF_ROUTE_TYPE) == "2":
            return await self.async_step_stops_train()
        return await self.async_step_stops()

    def _reset_for_next_journey(self) -> None:
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
        self._route_shown = ""
        self._return_trip = None
        self._return_name = ""

    async def async_step_finish(self, user_input: dict | None = None) -> FlowResult:
        """Leave the flow without doing anything else."""
        return self.async_abort(
            reason="finished",
            description_placeholders=TRANSLATION_DESCRIPTION_PLACEHOLDERS,
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
            await self.async_set_unique_id(datasource_unique_id(import_data[CONF_FILE]))
            self._abort_if_unique_id_configured()
            if not import_data.get(CONF_URL):
                # a zip in the folder no entry knew of: fetched from it, by
                # its file:// url, as a hosted source is by its own
                import_data[CONF_URL] = source_zip_url(self.hass, import_data[CONF_FILE])
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

    async def _find_return_trip(self, origin: str, destination: str) -> None:
        """Look for the same journey the other way round.

        Both directions of a line share one route_id, so the mirror is the
        same route with direction_id flipped. The stops are matched on name,
        not id: a terminus often has one stop per platform, so the id differs
        between directions even though the stop is the same place.
        """
        self._return_trip = {}
        route = self._user_inputs[CONF_ROUTE]
        try:
            exists = await self.hass.async_add_executor_job(
                has_trip_between, self._pygtfs, route,
                id_of(destination), id_of(origin))
            loop_direction = await self.hass.async_add_executor_job(
                get_pair_direction, self._pygtfs, route,
                id_of(destination), id_of(origin)) if exists else None
        except SQLAlchemyError as ex:
            _LOGGER.debug("No return journey for %s: %s", route, ex)
            return
        if not exists:
            _LOGGER.debug("Return journey: no trip runs it")
            return
        trip = await self._trip_name(destination, origin, loop_direction)
        self._return_name = self._suggested_name(trip)
        # only what differs: this runs when the screen opens, before the
        # options on it are answered, so the rest is merged at creation time
        self._return_trip = {
            CONF_LOOP_DIRECTION: loop_direction,
            CONF_ORIGIN: destination,
            CONF_DESTINATION: origin,
            CONF_NAME: self._return_name,
        }

    async def _create_return_trip(self) -> None:
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
