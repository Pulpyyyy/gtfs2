"""The files a refresh writes for a map card, and when it writes them.

The route file, the line drawn from its fullest trip with its shape
(export_route_shape); the timetable, every departure over the next service
days (export_timetable); the leg file, the ride of the next departure timed
stop by stop (export_leg). The writers themselves are in geojson.py; what is
here decides when a file is written again, and keeps the slow ones off the
refresh. Called from GTFSUpdateCoordinator._async_update_data, the
coordinator handed in keeps what was written last.
"""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import os

import homeassistant.util.dt as dt_util

from .const import DEFAULT_PATH_GEOJSON
from .geojson import (
    write_route_file, write_leg_file, write_timetable_file, route_geojson_name,
    leg_geojson_name, timetable_name, get_representative_trip,
)

_LOGGER = logging.getLogger(__name__)


def _route_export_state(zip_path, file):
    """What decides whether the route file is written again: the edition of
    the zip it is drawn from (size and modification time, changed by a
    refresh and by an import that rewrites the zip in place) and whether
    the file is still there. Two stats, made for the executor."""
    try:
        stat = os.stat(zip_path)
        edition = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        edition = ""
    return edition, os.path.exists(file)


def _drawn_trip(zip_path, file):
    """The trip a route file already draws, when it is at least as new as
    the zip it was drawn from; None when there is no such file, when the
    zip was replaced since, or when the file cannot be read. What spares a
    restart the reading of the line's shape again: on IDFM shapes.txt is
    131 MB to scan for one line, and eight entries did it at once."""
    try:
        if os.path.getmtime(file) < os.path.getmtime(zip_path):
            return None
        with open(file, encoding="utf-8") as handle:
            return (json.load(handle).get("properties") or {}).get("trip_id")
    except (OSError, ValueError, AttributeError):
        return None


async def export_route_shape(coordinator, data) -> None:
    """Write the geojson of the line the sensor rides.

    Shape and stops are read from the schedule, so this owes nothing to
    realtime: an entry without a vehicle feed, or with realtime switched
    off entirely, still gets its line drawn on a map card. Nor does it owe
    anything to there being a departure today: the line drawn is the
    fullest trip that calls at the sensor's stops, the same at night and
    on a Sunday. Rewritten only when that trip changes or the zip is
    replaced, that is when the feed does, which is what makes it cheap
    enough to sit on every static refresh. The zip counts because the
    line's polyline is read from it (see write_route_file): a new
    edition that ships shapes.txt where the last did not, or moves a
    shape, must reach the map even when the trip drawn keeps its id.

    A file already there, newer than the zip and drawing the same trip,
    is kept: a restart knows nothing of what the last run wrote, and
    read the shape again for every entry. When it has to be written, it
    is written in the background: the sensor waits for this refresh, and
    a large shapes.txt held the sensor platform past Home Assistant's
    minute at startup.
    """
    departure = coordinator._data.get("next_departure") or {}
    route_id = departure.get("route_id", None) or (data.get("route") or "").split(": ")[0]
    direction = str(departure.get("trip_direction_id", data.get("direction")))
    # the file is named from the route and the direction, both known even
    # once the last departure of the day is behind us: the attribute stays
    # put so a card keeps its route through the evening, and it is named
    # before the first write rather than a refresh later
    if route_id and direction not in ("None", ""):
        coordinator._data["route_geojson_file"] = route_geojson_name(route_id, direction)
    if not route_id:
        return
    # the trip drawn has to be one the sensor rides, or a card places its
    # stops where nothing it lists calls: the stops of the next
    # departure, the entry's once the last one of the day is gone
    origin_id = departure.get("origin_stop_id") or (data.get("origin") or "").split(": ")[0]
    destination_id = departure.get("destination_stop_id") or (data.get("destination") or "").split(": ")[0]
    # picking it reads every trip of the line, 0.3 s for Orleans line A, and
    # it ran on every refresh of every entry: the pick only changes with
    # the stops asked and the database, so it is kept until one of them does
    pick = (route_id, direction, origin_id, destination_id,
            getattr(coordinator, "_pygtfs_edition", None))
    if pick[-1] is not None and pick == getattr(coordinator, "_representative_pick", None):
        trip_id = coordinator._representative_trip
    else:
        try:
            trip_id = await coordinator.hass.async_add_executor_job(
                get_representative_trip, coordinator._data["schedule"], route_id, direction,
                origin_id, destination_id)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.exception("Error picking the trip to draw route %s: %s", route_id, ex)
            return
        coordinator._representative_pick, coordinator._representative_trip = pick, trip_id
    if not trip_id:
        return
    # rewritten when the trip changes, when the zip does, and when the
    # file is gone: a folder cleaned by hand must not leave the map
    # without its line until the next restart
    file = os.path.join(coordinator.hass.config.path(DEFAULT_PATH_GEOJSON), route_geojson_name(route_id, direction))
    zip_path = os.path.join(coordinator.hass.config.path(coordinator._data["gtfs_dir"]), str(coordinator._data["file"]) + ".zip")
    edition, present = await coordinator.hass.async_add_executor_job(_route_export_state, zip_path, file)
    export_key = f"{route_id}_{direction}:{trip_id}:{edition}"
    if export_key == coordinator._route_export_trip and present:
        return
    if present and await coordinator.hass.async_add_executor_job(_drawn_trip, zip_path, file) == trip_id:
        # written by an earlier run, and still the line of this zip
        coordinator._route_export_trip = export_key
        return
    if coordinator._route_task is not None and not coordinator._route_task.done():
        # one writing at a time: the next refresh looks again
        return
    coordinator._route_id = route_id
    coordinator._direction = direction
    coordinator._route_task = coordinator.hass.async_create_background_task(
        _write_route(coordinator, coordinator._data, route_id, direction, trip_id, export_key),
        f"gtfs2 route {route_id} {direction}")


async def _write_route(coordinator, source, route_id, direction, trip_id, export_key) -> None:
    """Write the route file off the refresh (see export_route_shape)."""
    try:
        await coordinator.hass.async_add_executor_job(write_route_file, coordinator.hass, source, route_id, direction, trip_id)
        coordinator._route_export_trip = export_key
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Error writing route geojson: %s", ex)


async def export_timetable(coordinator, data) -> None:
    """Write the timetable file: every departure of the entry over the
    service day under way and the two after it (see write_timetable_file).

    It changes with the service day and with the zip, and with nothing
    else: rewritten on the first static refresh of a new day, when the
    zip is replaced, and when the file is gone, never on the refreshes
    in between. The attribute is set once the file is written, unlike
    the leg file's: a card reads the sensor first and this file only
    past it, and one that is not there is better not named at all.

    The writing itself runs in the background: three days of departures
    and the next service day are a few reads more than the sensor's own,
    and at startup every entry does them at once, which held the sensor
    platform past Home Assistant's minute. The sensor comes up on its
    departures, and takes the attribute as soon as the file is there.
    """
    schedule = coordinator._data.get("schedule")
    # a sentinel string or None when the datasource is unusable, as the
    # departures read it: nothing to write the days from
    if schedule is None or isinstance(schedule, str):
        return
    name = timetable_name(data["name"])
    today = (dt_util.now() + timedelta(minutes=coordinator._data.get("offset", 0) or 0)).strftime("%Y-%m-%d")
    file = os.path.join(coordinator.hass.config.path(DEFAULT_PATH_GEOJSON), name)
    zip_path = os.path.join(coordinator.hass.config.path(coordinator._data["gtfs_dir"]), str(coordinator._data["file"]) + ".zip")
    edition, present = await coordinator.hass.async_add_executor_job(_route_export_state, zip_path, file)
    export_key = f"{today}:{edition}"
    if export_key == coordinator._timetable_export and present:
        # written already: named again, the refresh built a fresh _data
        coordinator._data["timetable_file"] = name
        return
    if coordinator._timetable_task is not None and not coordinator._timetable_task.done():
        # one writing at a time: the one under way names the file itself
        return
    coordinator._timetable_task = coordinator.hass.async_create_background_task(
        _write_timetable(coordinator, coordinator._data, name, today, zip_path, export_key),
        f"gtfs2 timetable {name}")


async def _write_timetable(coordinator, source, name, today, zip_path, export_key) -> None:
    """Write the timetable file off the refresh, then name it on the
    sensor without waiting for the next refresh."""
    try:
        await coordinator.hass.async_add_executor_job(write_timetable_file, coordinator.hass, source, today, zip_path)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Error writing the timetable file: %s", ex)
        return
    coordinator._timetable_export = export_key
    # the refresh under way when the task started may have been
    # replaced since: the attribute goes on the data the sensor reads now
    coordinator._data["timetable_file"] = name
    coordinator.async_update_listeners()


async def export_leg(coordinator, data, feed_entities) -> None:
    """Write the leg file: the ride of the next departure, and the clocks
    of every listed departure at every stop, realtime included when the
    trip updates of this refresh carry it.

    Named after the entry, not the line: it is this sensor's ride. The
    attribute is set before the write, so a card can name the file even
    when the first write fails.
    """
    departure = coordinator._data.get("next_departure") or {}
    route_id = str(departure.get("route_id") or (data.get("route") or "").split(": ")[0])
    direction = str(departure.get("trip_direction_id", data.get("direction")))
    coordinator._data["leg_geojson_file"] = leg_geojson_name(route_id, direction, data["name"])
    try:
        await coordinator.hass.async_add_executor_job(write_leg_file, coordinator.hass, coordinator._data, feed_entities)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Error writing leg geojson: %s", ex)
