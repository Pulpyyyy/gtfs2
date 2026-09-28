"""The files a refresh writes for a map card, and when it writes them.

The route file, the line drawn from its fullest trip with its shape
(export_route_shape); the timetable, every departure over the next service
days (export_timetable); the leg file, the ride of the next departure timed
stop by stop (export_leg). The writers themselves are in geojson.py, leg.py
and timetable.py; what is here decides when a file is written again, and
keeps the slow ones off the refresh. Called from GTFSUpdateCoordinator._async_update_data, the
coordinator handed in keeps what was written last. When an entry is
removed, remove_entry_geojson takes its files away with it.
"""
from __future__ import annotations

from datetime import timedelta
import glob
import json
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .const import DEFAULT_PATH, DEFAULT_PATH_GEOJSON, DOMAIN, id_of
from .gtfs_db import feed_zip, real_path
from .gtfs_helper import shown_ends, train_entry_routes
from .geojson import write_route_file, route_geojson_name, get_representative_trip, vehicle_positions_name
from .leg import write_leg_file, leg_geojson_name, leg_geojson_pattern, owns_leg_file
from .timetable import write_timetable_file, timetable_name

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


def _what_changed(previous, key, parts):
    """Which parts of an export key moved since the file was written, for
    the log line that says why it is written again."""
    changed = [part for part, old, new in zip(parts, previous, key) if old != new]
    return ", ".join(changed) + " changed" if changed else "written again"


# what the parts of each export key are, in the words of the log
_ROUTE_KEY_PARTS = ("the line", "the trip", "the zip", "the database")
_TIMETABLE_KEY_PARTS = ("the service day", "the zip", "the database")


def _route_write_reason(present, previous, drawn, export_key):
    """Why the route file is written again, for the log."""
    if not present:
        return "there is no file"
    if previous is None:
        # the first look since the start: what the file on disk says
        return (f"the file draws trip {drawn}" if drawn else
                "the file is older than the zip or the database, or unreadable")
    return _what_changed(previous, export_key, _ROUTE_KEY_PARTS)


def _drawn_trip(zip_path, file, db_path):
    """The trip a route file already draws, when it is at least as new as
    the zip and the database it was drawn from; None when there is no such
    file, when either was replaced since, or when the file cannot be read.
    What spares a restart the reading of the line's shape again: on IDFM
    shapes.txt is 131 MB to scan for one line, and eight entries did it at
    once.

    The database counts as much as the zip: the trip's stops and its
    shape_id come from it. A refresh adopts the zip first and builds the
    database after, half an hour on IDFM, and a file written in between
    named a shape of the old edition and took its points from the new one,
    where IDFM had given that number to another line: metro 6 drawn along
    metro 9 on 2026-09-27, with the stops in their right place."""
    try:
        written = os.path.getmtime(file)
        if written < os.path.getmtime(zip_path) or written < os.path.getmtime(db_path):
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
    on a Sunday. Rewritten only when that trip changes or the zip or the
    database is replaced, that is when the feed does, which is what makes
    it cheap enough to sit on every static refresh. The zip counts because
    the line's polyline is read from it (see write_route_file): a new
    edition that ships shapes.txt where the last did not, or moves a
    shape, must reach the map even when the trip drawn keeps its id. The
    database counts because the stops and the shape_id are read from it.

    A file already there, newer than the zip and the database and drawing the same trip,
    is kept: a restart knows nothing of what the last run wrote, and
    read the shape again for every entry. When it has to be written, it
    is written in the background: the sensor waits for this refresh, and
    a large shapes.txt held the sensor platform past Home Assistant's
    minute at startup.
    """
    route_id, direction, origin_id, destination_id = shown_ends(
        data, coordinator._data.get("next_departure") or {})
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
    # picking it reads every trip of the line, 0.3 s for Orleans line A, and
    # it ran on every refresh of every entry: the pick only changes with
    # the stops asked and the database, so it is kept until one of them does
    pick = (route_id, direction, origin_id, destination_id,
            coordinator._pygtfs_edition)
    if pick[-1] is not None and pick == coordinator._representative_pick:
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
    # rewritten when the trip changes, when the zip or the database does,
    # and when the file is gone: a folder cleaned by hand must not leave
    # the map without its line until the next restart
    file = os.path.join(coordinator.hass.config.path(DEFAULT_PATH_GEOJSON), route_geojson_name(route_id, direction))
    gtfs_dir = coordinator.hass.config.path(coordinator._data["gtfs_dir"])
    source = str(coordinator._data["file"])
    zip_path, db_path = feed_zip(gtfs_dir, source), real_path(gtfs_dir, source)
    edition, present = await coordinator.hass.async_add_executor_job(_route_export_state, zip_path, file)
    # the database edition too: a rebuild that keeps the trip id and the
    # zip still changes the shape_id the file names (see _drawn_trip)
    export_key = (f"{route_id}_{direction}", trip_id, edition, pick[-1])
    previous = coordinator._route_export_trip
    if export_key == previous and present:
        return
    drawn = None
    if present:
        drawn = await coordinator.hass.async_add_executor_job(_drawn_trip, zip_path, file, db_path)
        if drawn == trip_id:
            # written by an earlier run, and still the line of this zip
            coordinator._route_export_trip = export_key
            return
    if coordinator._route_task is not None and not coordinator._route_task.done():
        # one writing at a time: the next refresh looks again
        return
    _LOGGER.info("Writing the route file %s for trip %s: %s", os.path.basename(file),
                 trip_id, _route_write_reason(present, previous, drawn, export_key))
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

    It changes with the service day, the zip and the database, and with
    nothing else: rewritten on the first static refresh of a new day, when
    the zip or the database is replaced, and when the file is gone, never
    on the refreshes in between. The attribute is set once the file is written, unlike
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
    zip_path = feed_zip(coordinator.hass.config.path(coordinator._data["gtfs_dir"]), str(coordinator._data["file"]))
    edition, present = await coordinator.hass.async_add_executor_job(_route_export_state, zip_path, file)
    # the database edition too: the runs are read from it, and a refresh
    # adopts the zip first and builds the database after, so a file
    # written in between listed the old edition's runs until the next day
    export_key = (today, edition, coordinator._pygtfs_edition)
    previous = coordinator._timetable_export
    if export_key == previous and present:
        # written already: named again, the refresh built a fresh _data
        coordinator._data["timetable_file"] = name
        return
    if coordinator._timetable_task is not None and not coordinator._timetable_task.done():
        # one writing at a time: the one under way names the file itself
        return
    if previous is None:
        # every entry writes it at every start: not news
        _LOGGER.debug("Writing the timetable %s, the first since the start", name)
    else:
        _LOGGER.info("Writing the timetable %s: %s", name,
                     "there is no file" if not present else
                     _what_changed(previous, export_key, _TIMETABLE_KEY_PARTS))
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
    route_id, direction, _origin, _destination = shown_ends(
        data, coordinator._data.get("next_departure") or {})
    coordinator._data["leg_geojson_file"] = leg_geojson_name(route_id, direction, data["name"])
    try:
        await coordinator.hass.async_add_executor_job(write_leg_file, coordinator.hass, coordinator._data, feed_entities)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.exception("Error writing leg geojson: %s", ex)


async def remove_entry_geojson(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the geojson files an entry leaves behind on disk.

    Home Assistant clears the entity registry of a removed entry by itself,
    right after this callback, but nothing knows about the files: the map
    export writes www/gtfs2/<route>_<direction>.json and its _route.json
    companion, and they would stay there for good.

    Both are named after the route and the direction rather than the entry,
    so two entries on the same line share them: only remove them when no
    other entry still needs them.
    """
    # www/gtfs2, where the export writes them, not the datasource folder
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    # the leg file is this entry's own, nobody else writes or reads it; found
    # by its entry part, the line part being the departure's, not the entry's
    leg_owner = entry.data.get("name")
    # the timetable is the entry's own too, named after it alone
    names = [timetable_name(entry.data["name"])] if entry.data.get("name") else []
    route = id_of(entry.data.get("route"))
    if route == "train":
        names += await _train_line_files(hass, entry)
    elif route:
        names += _line_files(hass, entry, route)
    # a disk walk: the glob and the removals run in the executor, never on the loop
    await hass.async_add_executor_job(_remove_geojson_files, geojson_dir, leg_owner, names)


def _other_entries(hass, entry):
    return [e for e in hass.config_entries.async_entries(DOMAIN) if e.entry_id != entry.entry_id]


async def _train_line_files(hass, entry):
    """The map files of the lines a train entry rode that no other entry
    still reads."""
    # a train entry's departures ride whatever line serves its two
    # stations, and each wrote its files under that line: read back
    # which lines those are, the database is still there
    routes = await hass.async_add_executor_job(
        train_entry_routes, hass.config.path(DEFAULT_PATH), entry.data)
    others = _other_entries(hass, entry)
    # another train entry on this source may ride any of them
    train_beside = any(e.data.get("route") == "train" and e.data.get("file") == entry.data.get("file")
                       for e in others)
    names = []
    for route_id in routes:
        if train_beside or any(id_of(e.data.get("route")) == route_id for e in others):
            continue
        for d in ("0", "1", "None"):
            names += [vehicle_positions_name(route_id, d), route_geojson_name(route_id, d)]
    return names


def _line_files(hass, entry, route):
    """The map files of an entry's line that no other entry still reads in
    that direction, those named before the ids were sanitised included."""
    # an entry set up without a direction wrote its files under the
    # direction of the departures it followed, either one, or under "none"
    # when the feed has no direction_id at all
    direction = entry.data.get("direction")
    directions = [str(direction)] if direction is not None else ["0", "1", "None"]
    others = _other_entries(hass, entry)
    names = []
    for d in directions:
        still_used = any(
            id_of(e.data.get("route")) == route
            and (e.data.get("direction") is None or str(e.data.get("direction")) == d)
            for e in others
        )
        if still_used:
            _LOGGER.debug("Keeping geojson for route %s direction %s, another entry uses it",
                          route, d)
            continue
        names += [vehicle_positions_name(route, d), route_geojson_name(route, d)]
        # the files written before the ids were sanitised carry the raw name and
        # nothing else would ever remove them; an id that is not a plain file name
        # never wrote in this directory, so it is not looked for there
        legacy = f"{route}_{d}"
        if os.path.basename(legacy) == legacy and ".." not in legacy:
            names += [legacy + ".json", legacy + "_route.json"]
    return names


def _remove_geojson_files(geojson_dir, leg_owner, names):
    """Delete the leg files of the entry named leg_owner and the named files
    under geojson_dir, logging each removal. Blocking file work, made for
    the executor."""
    paths = [path for pattern in (leg_geojson_pattern(leg_owner) if leg_owner else ())
             for path in glob.glob(os.path.join(geojson_dir, pattern))
             # the glob can reach another entry's file, see owns_leg_file
             if owns_leg_file(path, leg_owner)]
    paths += [os.path.join(geojson_dir, name) for name in dict.fromkeys(names)]
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
            _LOGGER.info("Removed %s", path)
        except OSError as ex:
            _LOGGER.warning("Could not remove %s: %s", path, ex)
