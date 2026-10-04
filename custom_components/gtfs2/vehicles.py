"""The vehicles of a journey for a map card: read from the vehicle
positions feed, kept to the entity's route and way, titled with where
each one goes, and written as the GeoJSON file the card draws
(get_rt_vehicle_positions).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import binascii
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text as sql_text

from .geojson import map_file, vehicle_positions_name, write_json_file
from .line_ends import _names_a_place
from .rt_feed import FeedEntities, _read_feed, _same_route

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

    from .coordinator import GTFSUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


# a vehicle's feature and what its title is made of:
# (feature, trip_id, vehicle id, crc, direction)
type _Title = tuple[dict[str, Any], str, str, str, str]


def _trip_destinations(schedule: Schedule | str | None, trip_ids: Iterable[str]) -> dict[str, str]:
    """{trip_id: where it goes}: its headsign, or its last stop when the
    headsign is empty or a code (a train number, a mission code). Read for
    the vehicles on the map, which each go where their own trip goes."""
    trip_ids = sorted({str(t) for t in trip_ids if t})
    if not trip_ids or schedule is None or isinstance(schedule, str):
        return {}
    sql = """
    SELECT t.trip_id, t.trip_headsign,
           (SELECT s.stop_name FROM stop_times st
            INNER JOIN stops s ON s.stop_id = st.stop_id
            WHERE st.trip_id = t.trip_id
            ORDER BY st.stop_sequence DESC LIMIT 1) AS last_stop
    FROM trips t WHERE t.trip_id IN (SELECT value FROM json_each(:trips))
    """
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(sql_text(sql), {"trips": json.dumps(trip_ids)}).fetchall()
    except SQLAlchemyError as ex:
        _LOGGER.debug("Could not read where the vehicles go: %s", ex)
        return {}
    found = {}
    for trip_id, headsign, last_stop in rows:
        headsign = str(headsign or "").strip()
        where = headsign if _names_a_place(headsign) else str(last_stop or "").strip()
        if where:
            found[str(trip_id)] = where
    return found


def _trip_directions(schedule: Schedule | str | None, trip_ids: Iterable[str]) -> dict[str, str]:
    """{trip_id: direction_id} as the database has them, repairs included."""
    trip_ids = sorted({str(t) for t in trip_ids if t})
    if not trip_ids or schedule is None or isinstance(schedule, str):
        return {}
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                sql_text("SELECT trip_id, direction_id FROM trips "
                         "WHERE trip_id IN (SELECT value FROM json_each(:trips))"),
                {"trips": json.dumps(trip_ids)}).fetchall()
    except SQLAlchemyError as ex:
        _LOGGER.debug("Could not read the directions of the vehicles' trips: %s", ex)
        return {}
    return {str(trip): str(direction) for trip, direction in rows if direction is not None}


def _left_standing(vehicle: Mapping[str, Any], max_age: int, now: float) -> bool:
    """Whether a vehicle's position is older than max_age minutes.

    A position without a timestamp, absent or 0, is kept: a feed served as
    json may not give one, and a strict rule would empty its map. So is
    every position when max_age is 0.
    """
    try:
        stamp = int(vehicle.get("timestamp") or 0)
    except (TypeError, ValueError):
        return False
    return bool(max_age and stamp and now - stamp > max_age * 60)


def _vehicle_way(vehicle: Mapping[str, Any], route_id: str, trip_id: str, direction: str,
                 board: set[str], static_direction: Mapping[str, str]) -> tuple[str | int | None, bool]:
    """(the direction the vehicle is seen on, whether it goes on this map).

    The database's direction for its trip first, see get_rt_vehicle_positions,
    the feed's otherwise. On the map: the trip of the next departure, or a
    vehicle of the line going this way; one whose feed names no direction
    is placed by its trip, one of the board's, rather than on whichever map
    0 happens to be.
    """
    trip = str(vehicle["trip"]["trip_id"])
    seen = static_direction.get(trip, vehicle["trip"].get("direction_id"))
    on_this_way = trip in board if seen is None else str(direction) == str(seen)
    wanted = trip == str(trip_id) or (_same_route(route_id, vehicle["trip"]["route_id"])
                                      and on_this_way)
    return seen, wanted


def _vehicle_feature(vehicle: Mapping[str, Any], route_id: str, seen: str | int | None,
                     direction: str) -> tuple[dict[str, Any], _Title]:
    """A vehicle's point on the map, and what its title is made of later:
    (feature, (feature, trip_id, vehicle id, crc, direction)).

    The marker id is built from the vehicle's id or label when it has one,
    the trip's crc otherwise, which keeps geo_json_events from creating an
    entity per trip. The direction is the one of the map it lands on when
    the feed names none, so the id keeps the digit the registry cleanup
    reads.
    """
    trip_id = vehicle["trip"]["trip_id"]
    crc = str(binascii.crc32(trip_id.encode("utf8")))[-3:]
    ids = vehicle.get("vehicle", {})
    veh = str(ids.get("id", "") or ids.get("label", "")).strip()
    way = str(seen if seen is not None else direction)
    coordinates = [vehicle["position"]["longitude"], vehicle["position"]["latitude"]]
    feature = {
        "geometry": {"coordinates": coordinates, "type": "Point"},
        "properties": {
            "id": f"{route_id}_{way}_{veh or crc}",
            # written once every vehicle is known, see get_rt_vehicle_positions
            "title": "",
            "trip_id": trip_id,
            "route_id": str(route_id),
            "direction_id": seen if seen is not None else way,
            "vehicle_id": vehicle["vehicle"]["id"],
            "vehicle_label": vehicle["vehicle"]["label"],
            trip_id: coordinates,
        },
        "type": "Feature",
    }
    return feature, (feature, str(trip_id), veh, crc, way)


def marker_ids(route_id: str) -> re.Pattern[str]:
    """The registry unique_ids of a route's vehicle markers. geo_json_events
    keys a marker <its entry id>_<properties.id>: the id _vehicle_feature
    writes, route_way_vehicle (or the trip's crc), and route(direction)crc,
    the one written before it, which installs still hold."""
    route = re.escape(str(route_id))
    return re.compile(rf"(?:^|_){route}(?:_[^_]*_[^_]+|\(\d+\)\d{{1,3}})$")


def _candidate_trips(feed_entities: FeedEntities, board: set[str], route_id: str) -> list[str]:
    """The trips of the vehicles that may be this entry's: listed on its
    board, or on its line."""
    return [e["vehicle"]["trip"]["trip_id"] for e in feed_entities
            if e["vehicle"]["trip"]["trip_id"]
            and (str(e["vehicle"]["trip"]["trip_id"]) in board
                 or _same_route(route_id, e["vehicle"]["trip"]["route_id"]))]


def _title_vehicles(self: GTFSUpdateCoordinator, schedule: Schedule | str | None, titles: list[_Title]) -> None:
    """Title each vehicle kept on the map."""
    # each vehicle titled after where its own trip goes, read for all of
    # them at once: the entry's destination was used, which is where the
    # rider gets off, not where the vehicle goes, and two entries on the
    # same line wrote the same file with titles of their own in turn
    line = str((self._data.get("next_departure") or {}).get("route_short_name") or "").strip()
    going = _trip_destinations(schedule, [trip for _e, trip, _v, _c, _d in titles])
    icon = self._icon.split(':')[1]
    for element, trip, veh, crc, direction in titles:
        where = going.get(trip)
        if line and where:
            element["properties"]["title"] = line + " → " + where + " " + (veh or crc) + "_" + icon
        else:
            element["properties"]["title"] = str(self._route_id) + "(" + direction + ")" + crc + "_" + icon


def get_rt_vehicle_positions(self: GTFSUpdateCoordinator) -> list[dict[str, Any]]:
    if not self._vehicle_position_url:
        # read only for a source with a vehicle feed (the coordinator's _read_realtime)
        return []
    feed_entities = _read_feed(self, self._vehicle_position_url, "vehicle_positions")
    geojson_body: list[dict[str, Any]] = []
    titles = []
    if feed_entities is None:
        # a failed fetch returns None: iterating it raises, and the caller's
        # broad except then abandons the whole realtime block, so a hiccup on
        # vehicle-positions used to take the departure times down with it.
        # The file is left as it was: the last known positions beat none at
        # all while a host has a hiccup
        _LOGGER.debug("No proper RT feed entities for vehicle positions")
        return geojson_body
    if not feed_entities:
        # a feed that answers with nothing means the vehicles are off the
        # road, which is an answer: written as such, the map empties. Left
        # alone, the last buses of the evening sat on it all night
        _LOGGER.debug("The vehicle feed is empty, taking the vehicles off the map")
    # the direction each candidate's trip has in the database: the import
    # repairs direction_id where the provider mixed its trips up (80 trips
    # of four GVB trams), and the vehicle feed still carries the provider's,
    # which put those vehicles on the other direction's map
    board = {str(t) for t in self._trip_list}
    max_age = self._vehicle_max_age
    now = time.time()
    schedule = self._data.get("schedule")
    static_direction = _trip_directions(
        schedule, _candidate_trips(feed_entities, board, self._route_id))
    for entity in feed_entities:
        vehicle = entity["vehicle"]
        if not vehicle["trip"]["trip_id"] or _left_standing(vehicle, max_age, now):
            # Vehicle is not in service; nor is one whose position is older
            # than the source's limit: some feeds keep publishing the
            # vehicles gone back to the depot under their last trip, stacked
            # at the terminus among the ones still running
            continue
        seen, wanted = _vehicle_way(vehicle, self._route_id, self._trip_id,
                                    self._direction, board, static_direction)
        if not wanted:
            continue
        _LOGGER.debug("Found vehicle on route with attributes: %s", vehicle)
        feature, title = _vehicle_feature(vehicle, self._route_id, seen, self._direction)
        geojson_body.append(feature)
        titles.append(title)

    _title_vehicles(self, schedule, titles)

    self.geojson = {"features": geojson_body, "type": "FeatureCollection"}
    _LOGGER.debug("Vehicle geojson: %s", json.dumps(self.geojson))
    update_geojson(self)
    return geojson_body


def update_geojson(self: GTFSUpdateCoordinator) -> None:
    file = map_file(self.hass, vehicle_positions_name(self._route_id, self._direction))
    _LOGGER.debug("Creating geojson file: %s", file)
    write_json_file(file, self.geojson)
