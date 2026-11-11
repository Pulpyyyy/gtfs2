"""A vehicle running a trip of the board goes on the map, route_id or not.

Some vehicle feeds give every vehicle its trip_id and no route_id. Krakow's
trams on 2026-11-11 at 11:00: 113 vehicles, 113 with a trip_id, none with a
route_id or a direction_id. The journey on line 18 from Poczta Glowna listed
10 trips; two trams ran two of them, and the vehicle file held one, the
next departure's: a vehicle was kept by its trip only when it was that one,
otherwise by its route, which the feed never names.

A fourth tram of line 18 ran the same way, past Poczta Glowna, on a trip
the board no longer lists: the vehicle a rider is on, which the card keeps
until the arrival. Its line is read from the database, by its trip.
"""
from __future__ import annotations

import types

from google.transit import gtfs_realtime_pb2
from sqlalchemy import create_engine, text

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


def _on_the_map(monkeypatch, entities, board, schedule=None):
    monkeypatch.setattr(rt_feed, "get_gtfs_feed_entities", lambda **kw: entities)
    monkeypatch.setattr(vehicles, "update_geojson", lambda me: None)
    me = types.SimpleNamespace(
        _vehicle_position_url="http://feed.invalid/vp", _headers={}, _trip_id=board[0],
        _trip_list=board, _direction="0", _route_id="route_69", _icon="mdi:tram",
        _vehicle_max_age=const.DEFAULT_VEHICLE_MAX_AGE,
        _data={"file": "krakowtram", "schedule": schedule, "next_departure": {"route_short_name": "18"}})
    body = vehicles.get_rt_vehicle_positions(me)
    return sorted(e["properties"]["trip_id"] for e in body)


def _schedule(tmp_path, trips):
    """A database holding trips (trip_id, route_id, direction_id)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'krk.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table trips (trip_id varchar, route_id varchar, "
                          "direction_id integer, trip_headsign varchar)"))
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, stop_sequence integer)"))
        for trip, route, way in trips:
            conn.execute(text("insert into trips values (:t, :r, :d, 'Czerwone Maki P+R')"),
                         {"t": trip, "r": route, "d": way})
    return types.SimpleNamespace(engine=engine)


def test_every_trip_of_the_board_is_on_the_map(monkeypatch):
    board = ["block_97_trip_7_service_3", "block_98_trip_7_service_3", "block_99_trip_7_service_3"]
    entities = _entities(["block_97_trip_7_service_3", "block_98_trip_7_service_3"])
    assert _on_the_map(monkeypatch, entities, board) == [
        "block_97_trip_7_service_3", "block_98_trip_7_service_3"]


def test_a_trip_off_the_board_stays_off(monkeypatch):
    # with no route_id, and no database to tell, nothing says the vehicle
    # runs this line: another line's tram, or this line's other way, is not
    # drawn here
    board = ["block_97_trip_7_service_3"]
    entities = _entities(["block_97_trip_7_service_3", "block_12_trip_3_service_3"])
    assert _on_the_map(monkeypatch, entities, board) == ["block_97_trip_7_service_3"]


def test_the_database_names_the_line_of_a_tram_past_the_stop(tmp_path, monkeypatch):
    board = ["block_97_trip_7_service_3"]
    schedule = _schedule(tmp_path, [
        ("block_97_trip_7_service_3", "route_69", 0),
        ("block_90_trip_6_service_3", "route_69", 0),   # line 18, this way, past the stop
        ("block_91_trip_6_service_3", "route_69", 1),   # line 18, the other way
        ("block_12_trip_3_service_3", "route_31", 0),   # line 1
    ])
    entities = _entities(["block_97_trip_7_service_3", "block_90_trip_6_service_3",
                          "block_91_trip_6_service_3", "block_12_trip_3_service_3",
                          "block_55_trip_2_service_3"])  # a line not imported: no row
    assert _on_the_map(monkeypatch, entities, board, schedule) == [
        "block_90_trip_6_service_3", "block_97_trip_7_service_3"]
    schedule.engine.dispose()
