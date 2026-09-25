"""A vehicle whose position has not moved on for too long leaves the map.

Some feeds keep publishing the vehicles gone back to the depot, under the
id of their last trip. Seen on TAO's tram A one Friday at 21:53: 18
vehicles in the feed, 7 stamped two minutes before, 11 from 18 minutes to
2 h 51 before, all at the La Source terminus on trips of 17:57 to 20:43.
The map showed 18 trams where 7 ran. A position older than the source's
limit (vehicle_max_age, 10 minutes unless the source says otherwise) is
left out, as a vehicle with no trip already was. A position without a
timestamp, absent or 0, stays: a feed served as json may give none, and a
strict rule would empty its map.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time
from google.transit import gtfs_realtime_pb2

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

NOW = datetime.datetime(2026, 9, 25, 19, 53, tzinfo=datetime.timezone.utc)
FRESH = [2] * 7
STALE = [18, 25, 40, 55, 70, 90, 110, 130, 150, 160, 171]


def _feed_bytes(ages, extra=()):
    """A protobuf vehicle feed: one vehicle per age in minutes, and those in
    extra with no timestamp at all."""
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    for i, age in enumerate(ages):
        entity = feed.entity.add(id=f"v{i}")
        entity.vehicle.trip.trip_id = f"T{i}"
        entity.vehicle.trip.route_id = "A"
        entity.vehicle.trip.direction_id = 0
        entity.vehicle.vehicle.id = f"{i}"
        entity.vehicle.position.latitude = 47.837
        entity.vehicle.position.longitude = 1.9176
        entity.vehicle.timestamp = int(NOW.timestamp()) - age * 60
    for name in extra:
        entity = feed.entity.add(id=name)
        entity.vehicle.trip.trip_id = name
        entity.vehicle.trip.route_id = "A"
        entity.vehicle.trip.direction_id = 0
        entity.vehicle.vehicle.id = name
    return feed.SerializeToString()


def _on_the_map(monkeypatch, entities, **context):
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities", lambda **kw: entities)
    monkeypatch.setattr(gtfs_rt_helper, "update_geojson", lambda me: None)
    me = types.SimpleNamespace(
        _vehicle_position_url="http://feed.invalid/vp", _headers={}, _trip_id="T0",
        _trip_list=[], _direction="0", _route_id="A", _icon="mdi:tram",
        _data={"file": "tao", "schedule": None, "next_departure": {"route_short_name": "A"}},
        **context)
    with freeze_time(NOW):
        body = gtfs_rt_helper.get_rt_vehicle_positions(me)
    return sorted(e["properties"]["trip_id"] for e in body)


def _entities(ages, extra=()):
    return gtfs_rt_helper.convert_gtfs_realtime_positions_to_json(_feed_bytes(ages, extra))["entity"]


def test_the_vehicles_back_at_the_depot_leave_the_map(monkeypatch):
    kept = _on_the_map(monkeypatch, _entities(FRESH + STALE), _vehicle_max_age=10)
    assert kept == sorted(f"T{i}" for i in range(7))


def test_ten_minutes_unless_the_source_says_otherwise(monkeypatch):
    assert len(_on_the_map(monkeypatch, _entities(FRESH + STALE))) == 7
    # a source whose vehicles report every half hour keeps them longer
    assert len(_on_the_map(monkeypatch, _entities(FRESH + STALE), _vehicle_max_age=60)) == 11


def test_0_keeps_every_vehicle(monkeypatch):
    assert len(_on_the_map(monkeypatch, _entities(FRESH + STALE), _vehicle_max_age=0)) == 18


def test_a_position_without_a_timestamp_stays(monkeypatch):
    # protobuf reads an absent timestamp as 0; a json feed may leave the key
    # out, or write the int64 as text
    entities = _entities([], extra=["U1"])
    entities.append({"vehicle": {"trip": {"trip_id": "U2", "route_id": "A", "direction_id": 0},
                                 "position": {"latitude": 47.9, "longitude": 1.9},
                                 "vehicle": {"id": "U2", "label": ""}}})
    entities.append({"vehicle": {"trip": {"trip_id": "U3", "route_id": "A", "direction_id": 0},
                                 "position": {"latitude": 47.9, "longitude": 1.9},
                                 "vehicle": {"id": "U3", "label": ""},
                                 "timestamp": str(int(NOW.timestamp()) - 3600)}})
    assert _on_the_map(monkeypatch, entities, _vehicle_max_age=10) == ["U1", "U2"]
