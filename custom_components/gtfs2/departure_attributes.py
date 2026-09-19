"""The attributes this fork adds to the departure sensor.

GTFSDepartureSensor._update_attrs builds the sensor's attributes; each
function here fills one group of the fork's own into that dict, and removes
it when there is nothing to say: when the line next runs (next_service_info),
the map files a card draws from (map_files), the lists that go with the next
departures (next_departure_lists), the alert stack and its kind
(alert_details) and the trips behind the realtime departures
(realtime_trips). Every function takes the attributes dict and the values it
reads, nothing of the entity itself.
"""
from __future__ import annotations

from datetime import date, timedelta

import homeassistant.util.dt as dt_util

from .const import (
    ATTR_INFO,
    ATTR_NEXT_RT_TRIPS,
    ATTR_NEXT_SERVICE_DATE,
    ATTR_NEXT_SERVICE_IN_DAYS,
    ATTR_RT_CANCELLED,
    ATTR_RT_SKIPPED,
    TIME_STR_FORMAT,
)


def next_service_info(attributes, state, next_service, offset):
    """When the line next runs, as a date, a count of days and a sentence.

    state is the sensor's next departure or None, next_service the date the
    coordinator found past today, offset the sensor's minutes offset.
    """
    if state is None:
        # Three situations, and the user needs to tell them apart. In
        # order of how much is known:
        #
        #   nothing scheduled at all   no date to give, only say so
        #   next one is days away      name the date
        #   departures today           not this branch, the state is set
        #
        # The query reaches past today, so a next departure tomorrow is
        # already carried by the state and never lands here.
        #
        # So: whenever a next date exists it is published, and the wording
        # follows how far off it is. "no more departures" is kept for the
        # only case where it is the whole truth.
        delta = None
        if next_service:
            # How far off that is, so a card can say "tomorrow" or "Monday"
            # without re-deriving it: the offset already applies to what
            # counts as today here.
            try:
                today = (dt_util.now() + timedelta(
                    minutes=offset or 0)).date()
                delta = (date.fromisoformat(next_service) - today).days
            except (TypeError, ValueError):
                delta = None
        if next_service:
            attributes[ATTR_NEXT_SERVICE_DATE] = next_service
            attributes[ATTR_NEXT_SERVICE_IN_DAYS] = delta
            if delta == 0:
                # today, but every departure is behind us
                attributes[ATTR_INFO] = "No more departures today"
            else:
                # the query already reaches past today, so a next date
                # with nothing to show is worth naming whatever it is
                attributes[ATTR_INFO] = f"No departures until {next_service}"
        else:
            if ATTR_NEXT_SERVICE_DATE in attributes:
                del attributes[ATTR_NEXT_SERVICE_DATE]
            # nothing found within the search horizon: this line has no
            # scheduled service left at all. -1 rather than a missing key,
            # so a card can tell "never again" apart from "running now":
            # both would otherwise be the absence of an attribute.
            attributes[ATTR_NEXT_SERVICE_IN_DAYS] = -1
            attributes[ATTR_INFO] = "No scheduled departures"
    else:
        # There is a departure, but is it today's? The query reaches past
        # today, so the state can carry tomorrow's first departure. The line is resting today all the same, and a
        # card needs the machine-readable date for its badge, not only
        # the sentence below: publish the same two attributes as when
        # there is nothing to show at all, derived from the departure
        # itself. Deleting them here was what kept a badge blank on a
        # line whose next trip is tomorrow morning.
        delta = None
        if state:
            try:
                today = (dt_util.now() + timedelta(
                    minutes=offset or 0)).date()
                delta = (dt_util.as_local(state).date() - today).days
            except (TypeError, ValueError):
                delta = None
        if delta is not None and delta > 0:
            shown = dt_util.as_local(state)
            attributes[ATTR_NEXT_SERVICE_DATE] = shown.date().isoformat()
            attributes[ATTR_NEXT_SERVICE_IN_DAYS] = delta
            attributes[ATTR_INFO] = (
                f"Next departures tomorrow at {shown.strftime(TIME_STR_FORMAT)}"
                if delta == 1
                else f"No departures until {shown.date().isoformat()}")
        else:
            for k in (ATTR_NEXT_SERVICE_DATE, ATTR_NEXT_SERVICE_IN_DAYS):
                if k in attributes:
                    del attributes[k]
            if ATTR_INFO in attributes:
                del attributes[ATTR_INFO]


def next_departure_lists(attributes, departure, listed):
    """The fork's lists beside next_departures: durations, the stop each one
    leaves from, the route type of each. listed is the next_departures list,
    empty lists when there is none."""
    # Add next departures durations, in minutes
    attributes["next_departures_durations"] = []
    if listed:
        attributes["next_departures_durations"] = departure[
            "next_departures_durations"][:10]

    # Add the stop each next departure leaves from: a place can be served
    # from two of its records in turn (a terminus's quays)
    attributes["next_departures_origin_stop_id"] = []
    if listed:
        attributes["next_departures_origin_stop_id"] = departure.get(
            "next_departures_origin_stop_id", [])[:10]
    # Add next departures route types: a rail line may list a coach
    attributes["next_departures_route_types"] = []
    if listed:
        attributes["next_departures_route_types"] = departure.get(
            "next_departures_route_types", [])[:10]


def map_files(attributes, data):
    """The drawn line and the timed ride, exported with or without realtime;
    data is the coordinator's."""
    if data.get("route_geojson_file", None):
        attributes["route_geojson_file"] = data["route_geojson_file"]
    if data.get("leg_geojson_file", None):
        attributes["leg_geojson_file"] = data["leg_geojson_file"]
    if data.get("timetable_file", None):
        attributes["timetable_file"] = data["timetable_file"]
    if data.get("vehicle_positions_file", None):
        attributes["vehicle_positions_file"] = data["vehicle_positions_file"]


def alert_details(attributes, alert):
    """The alert stack and its kind, beside the two sentences upstream
    publishes; alert is the coordinator's alert dict."""
    # The whole stack behind those two sentences, worst first: a journey can
    # be under a cancellation and a works notice at the same time, and the
    # strings can only say one of them. Each item carries its text and, when
    # the feed states them, its cause and effect. Written only when there is
    # something to say, and removed when there is not.
    for key in ("origin_stop_alerts", "destination_stop_alerts"):
        value = alert.get(key, None)
        if value:
            attributes[key] = value
        elif key in attributes:
            del attributes[key]

    # What kind of alert, in the feed's own vocabulary: a cause out of
    # twelve and an effect out of eleven. A card can draw roadworks from
    # CONSTRUCTION; it cannot draw them from a free sentence. Written only
    # when the feed says so, and removed when it stops saying so, so the
    # attribute's presence is itself the answer to "is there one".
    for key in ("alert_cause", "alert_effect"):
        value = alert.get(key, None)
        if value:
            attributes[key] = value
        elif key in attributes:
            del attributes[key]


def realtime_trips(attributes, departure_rt):
    """The trip behind each realtime departure, and what the feed struck
    out: a cancelled trip is no longer in the departure lists, its id is
    here for a card to say so."""
    for key, attr in ((ATTR_NEXT_RT_TRIPS, "next_departures_realtime_trips"),
                      (ATTR_RT_CANCELLED, "cancelled_trips_realtime"),
                      (ATTR_RT_SKIPPED, "skipped_trips_realtime")):
        if key in departure_rt:
            attributes[attr] = departure_rt[key]
