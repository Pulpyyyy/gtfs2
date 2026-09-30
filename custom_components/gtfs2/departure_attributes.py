"""The attributes of the departure sensor, by group.

GTFSDepartureSensor._update_attrs builds the sensor's attributes; each
function here fills one group into that dict. Upstream's: the departure's
times (departure_times), the agency and the two ends
(station_attributes), the line and the trip (route_and_trip_attributes),
the calls at both ends (stop_time_attributes), the next departures
(next_departure_attributes) and the realtime (realtime_attributes). The
fork's own, removed when there is nothing to say: when the line next runs
(next_service_info), the map files a card draws from (map_files), the
lists that go with the next departures (next_departure_lists), the alert
stack and its kind (alert_details) and the trips behind the realtime
departures (realtime_trips). Every function takes the attributes dict and
the values it reads, nothing of the entity itself.
"""
from __future__ import annotations

from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.util import slugify
import homeassistant.util.dt as dt_util

from .const import (
    id_of,
    ATTR_ARRIVAL,
    ATTR_BICYCLE,
    ATTR_DAY,
    ATTR_DROP_OFF_DESTINATION,
    ATTR_DROP_OFF_ORIGIN,
    ATTR_FIRST,
    ATTR_INFO,
    ATTR_INFO_RT,
    ATTR_LAST,
    ATTR_LOCATION_DESTINATION,
    ATTR_LOCATION_ORIGIN,
    ATTR_NEXT_RT,
    ATTR_NEXT_RT_DELAYS,
    ATTR_NEXT_RT_TRIPS,
    ATTR_NEXT_SERVICE_DATE,
    ATTR_NEXT_SERVICE_IN_DAYS,
    ATTR_PICKUP_DESTINATION,
    ATTR_PICKUP_ORIGIN,
    ATTR_ROUTE_TYPE,
    ATTR_RT_CANCELLED,
    ATTR_RT_SKIPPED,
    ATTR_RT_UPDATED_AT,
    ATTR_TIMEPOINT_DESTINATION,
    ATTR_TIMEPOINT_ORIGIN,
    ATTR_TIMEZONE_DESTINATION,
    ATTR_TIMEZONE_ORIGIN,
    ATTR_WHEELCHAIR,
    ATTR_WHEELCHAIR_DESTINATION,
    ATTR_WHEELCHAIR_ORIGIN,
    BICYCLE_ALLOWED_DEFAULT,
    BICYCLE_ALLOWED_OPTIONS,
    DROP_OFF_TYPE_DEFAULT,
    DROP_OFF_TYPE_OPTIONS,
    LOCATION_TYPE_DEFAULT,
    LOCATION_TYPE_OPTIONS,
    PICKUP_TYPE_DEFAULT,
    PICKUP_TYPE_OPTIONS,
    ROUTE_TYPE_OPTIONS,
    TIMEPOINT_DEFAULT,
    TIMEPOINT_OPTIONS,
    TIME_STR_FORMAT,
    WHEELCHAIR_ACCESS_DEFAULT,
    WHEELCHAIR_ACCESS_OPTIONS,
    WHEELCHAIR_BOARDING_DEFAULT,
    WHEELCHAIR_BOARDING_OPTIONS,
)

_LOGGER = logging.getLogger(__name__)


def departure_records(schedule, data):
    """The rows the sensor describes its departure with, read in one go.

    The two stops, the trip, the route and its agency, which the sensor
    used to fetch itself from inside its state callback: that runs on the
    event loop, and four or five SQLite reads a sensor a minute there hold
    everything else up, the whole of Home Assistant waiting on a database
    a prune or a swap may be holding. The coordinator runs this in the
    executor and the sensor reads the answer.

    Returns {"origin", "destination", "trip", "route", "agency"}, each the
    pygtfs record or None; a train entry names its ends by station, so its
    two stops are the names as the entry holds them.
    """
    records = {"origin": None, "destination": None, "trip": None,
               "route": None, "agency": None}
    if schedule is None or isinstance(schedule, str) or data.get("extracting"):
        return records
    origin = id_of(data.get("origin"))
    destination = id_of(data.get("destination"))
    departure = data.get("next_departure") or {}
    if data.get("route_type") == "2":
        records["origin"], records["destination"] = origin, destination
    else:
        # the record the vehicle really calls at first, often the pole
        # across the road from the entry's own, then the entry's
        for key, own, called in (("origin", origin, departure.get("origin_stop_id")),
                                 ("destination", destination, departure.get("destination_stop_id"))):
            stops = (schedule.stops_by_id(called) if called else None) or schedule.stops_by_id(own)
            records[key] = stops[0] if stops else None
    if departure:
        trips = schedule.trips_by_id(departure.get("trip_id"))
        records["trip"] = trips[0] if trips else None
        route_id = departure.get("route_id")
    else:
        # the line of the entry itself, so a card can still name and colour
        # it on a day it does not run
        route_id = id_of(data.get("route"))
    if route_id:
        routes = schedule.routes_by_id(route_id)
        records["route"] = routes[0] if routes else None
    if records["route"] is not None:
        agencies = schedule.agencies_by_id(records["route"].agency_id)
        # False, not None: the agency was looked for and is not there
        records["agency"] = agencies[0] if agencies else False
    return records


def _days_from_today(day, offset):
    """How many days a date lies after today, the sensor's minutes offset
    applied to what counts as today."""
    return (day - (dt_util.now() + timedelta(minutes=offset or 0)).date()).days


def _resting_info(attributes, next_service, offset):
    """next_service_info with no departure to show.

    Three situations, and the user needs to tell them apart. In order of
    how much is known:

      nothing scheduled at all   no date to give, only say so
      next one is days away      name the date
      departures today           not this branch, the state is set

    The query reaches past today, so a next departure tomorrow is already
    carried by the state and never lands here.

    So: whenever a next date exists it is published, and the wording
    follows how far off it is. "no more departures" is kept for the only
    case where it is the whole truth.
    """
    if not next_service:
        attributes.pop(ATTR_NEXT_SERVICE_DATE, None)
        # nothing found within the search horizon: this line has no
        # scheduled service left at all. -1 rather than a missing key,
        # so a card can tell "never again" apart from "running now":
        # both would otherwise be the absence of an attribute.
        attributes[ATTR_NEXT_SERVICE_IN_DAYS] = -1
        attributes[ATTR_INFO] = "No scheduled departures"
        return
    # How far off that is, so a card can say "tomorrow" or "Monday"
    # without re-deriving it: the offset already applies to what counts
    # as today here.
    try:
        delta = _days_from_today(date.fromisoformat(next_service), offset)
    except (TypeError, ValueError):
        delta = None
    attributes[ATTR_NEXT_SERVICE_DATE] = next_service
    attributes[ATTR_NEXT_SERVICE_IN_DAYS] = delta
    # today, but every departure is behind us; otherwise the query
    # already reaches past today, so a next date with nothing to show is
    # worth naming whatever it is
    attributes[ATTR_INFO] = ("No more departures today" if delta == 0
                             else f"No departures until {next_service}")


def _departure_day_info(attributes, state, offset):
    """next_service_info with a departure to show.

    There is a departure, but is it today's? The query reaches past today,
    so the state can carry tomorrow's first departure. The line is resting
    today all the same, and a card needs the machine-readable date for its
    badge, not only the sentence below: publish the same two attributes as
    when there is nothing to show at all, derived from the departure
    itself. Deleting them here was what kept a badge blank on a line whose
    next trip is tomorrow morning.
    """
    delta = None
    if state:
        try:
            delta = _days_from_today(dt_util.as_local(state).date(), offset)
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
        for k in (ATTR_NEXT_SERVICE_DATE, ATTR_NEXT_SERVICE_IN_DAYS, ATTR_INFO):
            attributes.pop(k, None)


def next_service_info(attributes, state, next_service, offset):
    """When the line next runs, as a date, a count of days and a sentence.

    state is the sensor's next departure or None, next_service the date the
    coordinator found past today, offset the sensor's minutes offset.
    """
    if state is None:
        _resting_info(attributes, next_service, offset)
    else:
        _departure_day_info(attributes, state, offset)


_FORK_DEPARTURE_LISTS = (
    # next departures durations, in minutes
    "next_departures_durations",
    # the stop each next departure leaves from: a place can be served
    # from two of its records in turn (a terminus's quays)
    "next_departures_origin_stop_id",
    # next departures route types: a rail line may list a coach
    "next_departures_route_types",
)


def next_departure_lists(attributes, departure, listed):
    """The fork's lists beside next_departures: durations, the stop each one
    leaves from, the route type of each. listed is the next_departures list,
    empty lists when there is none."""
    for key in _FORK_DEPARTURE_LISTS:
        attributes[key] = departure.get(key, [])[:10] if listed else []


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
    # the feed states them, its cause and effect, and the names of the
    # journey's stops it is addressed to. Written only when there is
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


def departure_times(attributes, departure):
    """When the departure arrives, how long it rides, and whether it is
    the day's first or last."""
    if not departure:
        return
    attributes[ATTR_ARRIVAL] = dt_util.as_utc(
        departure.get("arrival_time")
    ).isoformat()
    # theoretical journey time in minutes, arrival minus departure
    attributes["duration"] = departure.get("duration")
    attributes[ATTR_DAY] = departure["day"]
    for key in (ATTR_FIRST, ATTR_LAST):
        if departure[key] is not None:
            attributes[key] = departure[key]


def station_attributes(attributes, departure, agency, origin, destination, route_type):
    """The agency and the two ends, as the feed describes them."""
    if agency:
        append_keys(attributes, dict_for_table(agency), "Agency")
    if route_type == "2":
        # a train names its ends by station, not by stop record: what
        # the departure itself says of them
        attributes["origin_station_stop_name"] = departure.get("origin_stop_name", None)
        attributes["origin_station_stop_id"] = departure.get("origin_stop_id", None)
        attributes["origin_station_stop_sequence"] = departure.get("origin_stop_sequence", None)
        attributes["destination_station_stop_name"] = departure.get("destination_stop_name", None)
        attributes["destination_station_stop_id"] = departure.get("destination_stop_id", None)
        return
    _end_attributes(attributes, origin, "Origin Station",
                         ATTR_LOCATION_ORIGIN, ATTR_WHEELCHAIR_ORIGIN)
    _end_attributes(attributes, destination, "Destination Station",
                         ATTR_LOCATION_DESTINATION, ATTR_WHEELCHAIR_DESTINATION)


def _end_attributes(attributes, stop, prefix, location_key, wheelchair_key):
    """One end's stop record, its kind of place and its access."""
    if not stop:
        return
    append_keys(attributes, dict_for_table(stop), prefix)
    attributes[location_key] = LOCATION_TYPE_OPTIONS.get(
        stop.location_type, LOCATION_TYPE_DEFAULT
    )
    attributes[wheelchair_key] = WHEELCHAIR_BOARDING_OPTIONS.get(
        stop.wheelchair_boarding, WHEELCHAIR_BOARDING_DEFAULT
    )


def route_and_trip_attributes(attributes, route, trip):
    """The line and the trip the departure rides."""
    if route:
        append_keys(attributes, dict_for_table(route), "Route")
        attributes[ATTR_ROUTE_TYPE] = ROUTE_TYPE_OPTIONS[
            route.route_type
        ]
    if trip:
        append_keys(attributes, dict_for_table(trip), "Trip")
        attributes[ATTR_BICYCLE] = BICYCLE_ALLOWED_OPTIONS.get(
            trip.bikes_allowed, BICYCLE_ALLOWED_DEFAULT
        )
        attributes[ATTR_WHEELCHAIR] = WHEELCHAIR_ACCESS_OPTIONS.get(
            trip.wheelchair_accessible, WHEELCHAIR_ACCESS_DEFAULT
        )


def stop_time_attributes(attributes, departure):
    """The trip's call at each end: how a rider gets on and off there,
    whether the time is exact, and the zone it is written in."""
    if not departure:
        return
    for end, drop_off, pickup, timepoint, zone in (
            ("origin", ATTR_DROP_OFF_ORIGIN, ATTR_PICKUP_ORIGIN,
             ATTR_TIMEPOINT_ORIGIN, ATTR_TIMEZONE_ORIGIN),
            ("destination", ATTR_DROP_OFF_DESTINATION, ATTR_PICKUP_DESTINATION,
             ATTR_TIMEPOINT_DESTINATION, ATTR_TIMEZONE_DESTINATION)):
        stop_time = departure[f"{end}_stop_time"]
        append_keys(attributes, stop_time, f"{end}_stop")
        attributes[drop_off] = DROP_OFF_TYPE_OPTIONS.get(
            stop_time["Drop Off Type"], DROP_OFF_TYPE_DEFAULT
        )
        attributes[pickup] = PICKUP_TYPE_OPTIONS.get(
            stop_time["Pickup Type"], PICKUP_TYPE_DEFAULT
        )
        attributes[timepoint] = TIMEPOINT_OPTIONS.get(
            stop_time["Timepoint"], TIMEPOINT_DEFAULT
        )
        attributes[zone] = departure.get(f"{end}_stop_timezone", None)


# the lists a departure carries, by the attribute that shows them, ten
# departures at most
_NEXT_DEPARTURE_LISTS = (
    ("next_departures", "next_departures"),
    ("next_departures_lines", "next_departures_lines"),
    ("next_departures_headsign", "next_departures_headsign"),
    ("next_departures_trips", "next_departures_trip_id"),
    ("next_departures_destination_arrival_times", "next_departures_destination_arrival_times"),
)


def next_departure_attributes(attributes, departure, next_departures):
    for attribute, key in _NEXT_DEPARTURE_LISTS:
        attributes[attribute] = (
            departure[key][:10] if next_departures else [])


def realtime_attributes(attributes, departure_rt):
    """What the realtime feed says of the next departures, or that it
    said nothing."""
    if not departure_rt:
        _LOGGER.debug("No next departure realtime attributes")
        attributes[ATTR_INFO_RT] = "No realtime information"
        return
    _LOGGER.debug("next dep realtime attr: %s", departure_rt)
    # Add next departure realtime to the right level, only if populated
    if "gtfs_rt_updated_at" not in departure_rt:
        return
    attributes["gtfs_rt_updated_at"] = departure_rt[ATTR_RT_UPDATED_AT]
    if departure_rt.get(ATTR_NEXT_RT, None):
        attributes["next_departure_realtime"] = departure_rt[ATTR_NEXT_RT][0]
        attributes["next_departures_realtime"] = departure_rt[ATTR_NEXT_RT]
    else:
        attributes["next_departure_realtime"] = '-'
        attributes["next_departures_realtime"] = '-'
    if departure_rt.get(ATTR_NEXT_RT_DELAYS, None):
        attributes["next_delay_realtime"] = departure_rt[ATTR_NEXT_RT_DELAYS][0]
        attributes["next_delays_realtime"] = departure_rt[ATTR_NEXT_RT_DELAYS]
    else:
        attributes["next_delay_realtime"] = '-'
        attributes["next_delays_realtime"] = '-'
    realtime_trips(attributes, departure_rt)


def dict_for_table(resource: Any) -> dict:
    """Return a dictionary for the SQLAlchemy resource given."""
    _dict = {}
    for column in resource.__table__.columns:
        value = getattr(resource, column.name)
        # a column the feed left empty stays None, which append_keys
        # leaves out: made text, it came out as an attribute "None"
        _dict[column.name] = None if value is None else str(value)
    return _dict


def append_keys(attributes, resource: dict, prefix: str | None = None) -> None:
    """Properly format key val pairs to append to attributes."""
    for attr, val in resource.items():
        if val == "" or val is None or attr == "feed_id":
            continue
        key = attr
        if prefix and not key.startswith(prefix):
            key = f"{prefix} {key}"
        key = slugify(key)
        attributes[key] = val
