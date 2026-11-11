"""A vehicle running a trip of the board goes on the map, route_id or not.

Some vehicle feeds give every vehicle its trip_id and no route_id. Krakow's
trams on 2026-11-11 at 11:00: 113 vehicles, 113 with a trip_id, none with a
route_id or a direction_id. The journey on line 18 from Poczta Glowna listed
10 trips; two trams ran two of them, and the vehicle file held one, the
next departure's: a vehicle was kept by its trip only when it was that one,
otherwise by its route, which the feed never names.
"""
from __future__ import annotations

import types

from google.transit import gtfs_realtime_pb2

import ha_stub

vehicles = ha_stub.load("domain.vehicles")
rt_feed = ha_stub.load("feed.rt_feed")
const = ha_stub.load("const")


def _entities(trips):
    """A vehicle feed with one vehicle per trip, no route_id, no direction_id."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for i, trip in enumerate(trips):
        entity = feed.entity.add(id=f"v{i}")
        entity.vehicle.trip.trip_id = trip
        entity.vehicle.vehicle.id = f"{800 + i}"
        entity.vehicle.position.latitude = 50.06
        entity.vehicle.position.longitude = 19.94
    return rt_feed.convert_gtfs_realtime_positions_to_json(feed.SerializeToString())["entity"]


def _on_the_map(monkeypatch, entities, board):
    monkeypatch.setattr(rt_feed, "get_gtfs_feed_entities", lambda **kw: entities)
    monkeypatch.setattr(vehicles, "update_geojson", lambda me: None)
    me = types.SimpleNamespace(
        _vehicle_position_url="http://feed.invalid/vp", _headers={}, _trip_id=board[0],
        _trip_list=board, _direction="0", _route_id="route_69", _icon="mdi:tram",
        _vehicle_max_age=const.DEFAULT_VEHICLE_MAX_AGE,
        _data={"file": "krakowtram", "schedule": None, "next_departure": {"route_short_name": "18"}})
    body = vehicles.get_rt_vehicle_positions(me)
    return sorted(e["properties"]["trip_id"] for e in body)


def test_every_trip_of_the_board_is_on_the_map(monkeypatch):
    board = ["block_97_trip_7_service_3", "block_98_trip_7_service_3", "block_99_trip_7_service_3"]
    entities = _entities(["block_97_trip_7_service_3", "block_98_trip_7_service_3"])
    assert _on_the_map(monkeypatch, entities, board) == [
        "block_97_trip_7_service_3", "block_98_trip_7_service_3"]


def test_a_trip_off_the_board_stays_off(monkeypatch):
    # with no route_id, nothing says the vehicle runs this line: another
    # line's tram, or this line's other way, is not drawn here
    board = ["block_97_trip_7_service_3"]
    entities = _entities(["block_97_trip_7_service_3", "block_12_trip_3_service_3"])
    assert _on_the_map(monkeypatch, entities, board) == ["block_97_trip_7_service_3"]
