"""Two steps of the fork's refresh, called from GTFSUpdateCoordinator.

When the timetable has nothing left to show, next_service_date_for looks
ahead for the next day the journey runs at all. When the realtime feed
struck a listed trip, cancelled or skipping the origin, drop_struck_trips
reads the departures again without it and refreshes the realtime
attributes for the departure now shown. Both run inside
_async_update_data, after the static and the realtime readings.
"""
from __future__ import annotations

from datetime import timedelta
from functools import partial
import logging

import homeassistant.util.dt as dt_util

from .const import ATTR_RT_CANCELLED, ATTR_RT_SKIPPED
from .gtfs_helper import drop_departure_trips, get_next_service_date
from .gtfs_rt_helper import get_next_services, merge_struck

_LOGGER = logging.getLogger(__name__)


async def next_service_date_for(hass, schedule, data, offset):
    """The next day this journey runs, as YYYY-MM-DD, or None.

    The search starts today, not tomorrow. A line can run today
    with every departure already behind us, and that is not the
    same thing as a line resting for days: the sensor tells the
    two apart by whether the date it gets back is today's.
    """
    try:
        # async_add_executor_job takes positional arguments only:
        # the keywords ride in a partial, or the call raises and
        # the date is lost on every refresh
        return await hass.async_add_executor_job(partial(
            get_next_service_date, schedule,
            data["origin"].split(": ")[0], data["destination"].split(": ")[0],
            (dt_util.now() + timedelta(
                minutes=offset or 0)).strftime("%Y-%m-%d"),
            data["route_type"],
            line=data.get("line"),
            origin_names=data.get("origin_stations"),
            dest_names=data.get("destination_stations"),
            # the line and, at a loop's terminus, the way round the
            # departures themselves are held to
            route=(data.get("route") or "").split(": ")[0] or None,
            direction=data.get("loop_direction"),
        ))
    except Exception as ex:  # pylint: disable=broad-except
        # only enriches an attribute: never fail the update over it
        _LOGGER.warning("Could not get next service date: %s", ex)
        return None


async def drop_struck_trips(coordinator, data, run_static):
    """Read the departures again without the trips the feed struck.

    A trip the feed cancelled, or that skips the origin, is
    no departure: the board moves on to the next one rather
    than showing the struck one as on time. The departures
    are read again from the rows of the last static refresh
    without those trips, and the realtime attributes follow
    the departure now shown.
    What was struck is remembered until the next static
    refresh: once dropped, a trip is no longer in the list
    the feed is matched against, and the attribute would
    forget it a minute later.
    """
    if run_static:
        coordinator._struck_cancelled, coordinator._struck_skipped = {}, {}
    coordinator._remember_struck()
    struck = merge_struck(coordinator._struck_skipped, coordinator._struck_cancelled)
    departure = coordinator._data.get("next_departure") or {}
    listed = {str(t) for t in departure.get("next_departures_trip_id") or []}
    listed.add(str(departure.get("trip_id")))
    if struck and listed & set(struck) and coordinator._data.get("departure_rows"):
        _LOGGER.debug("GTFS RT: the feed struck %s out of the listed trips, reading the departures again", sorted(listed & set(struck)))
        coordinator._data["next_departure"] = await coordinator.hass.async_add_executor_job(
            drop_departure_trips, coordinator.hass, coordinator._data, struck)
        departure = coordinator._data["next_departure"] or {}
        coordinator._stop_id = departure.get("origin_stop_id", data["origin"]).split(": ")[0]
        coordinator._stop_sequence = departure.get("origin_stop_sequence", None)
        coordinator._trip_id = departure.get("trip_id", None) or "no_trip_information"
        coordinator._trip_short_name = departure.get("trip_short_name", None)
        coordinator._direction = str(departure.get("trip_direction_id", data["direction"]))
        coordinator._trip_list = departure.get("next_departures_trip_id", [])[:10]
        coordinator._get_next_service = await coordinator.hass.async_add_executor_job(get_next_services, coordinator)
        coordinator._remember_struck()
        coordinator._data["next_departure_realtime_attr"] = coordinator._get_next_service
        coordinator._data["next_departure_realtime_attr"]["gtfs_rt_updated_at"] = dt_util.utcnow()
    # the trips struck since the last static refresh, whichever
    # reading turned them up
    coordinator._get_next_service[ATTR_RT_CANCELLED] = sorted(coordinator._struck_cancelled)
    coordinator._get_next_service[ATTR_RT_SKIPPED] = sorted(coordinator._struck_skipped)
