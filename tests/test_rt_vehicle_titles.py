"""A vehicle on the map is titled after where its own trip goes.

The title used the entry's destination: where the rider gets off, not
where the vehicle goes, and two entries on the same line wrote the same
file with titles of their own in turn. The trip's headsign names it
now, or its last stop when the headsign is empty or a code.
"""
from __future__ import annotations

import types

from sqlalchemy import create_engine, text

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")


def _schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'veh.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table trips (trip_id varchar, route_id varchar, trip_headsign varchar)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, stop_sequence integer)"))
        conn.execute(text("insert into stops values ('S1', 'Gare'), ('S2', 'Lac'), ('S3', 'Stade')"))
        conn.execute(text("insert into trips values ('T1', 'R1', 'Lac'), ('T2', 'R1', '44930')"))
        for trip, stop, seq in (("T1", "S1", 1), ("T1", "S2", 2), ("T2", "S1", 1), ("T2", "S3", 2)):
            conn.execute(text("insert into stop_times values (:t, :s, :q)"), {"t": trip, "s": stop, "q": seq})
    return types.SimpleNamespace(engine=engine)


def _vehicle(trip, vehicle_id):
    return {"vehicle": {"trip": {"trip_id": trip, "route_id": "R1", "direction_id": "0"},
                        "position": {"latitude": 47.9, "longitude": 1.9},
                        "vehicle": {"id": vehicle_id, "label": ""}}}


def test_each_vehicle_titled_after_its_trip(tmp_path, monkeypatch):
    schedule = _schedule(tmp_path)
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities",
                        lambda **kw: [_vehicle("T1", "101"), _vehicle("T2", "102")])
    monkeypatch.setattr(gtfs_rt_helper, "update_geojson", lambda me: None)
    me = types.SimpleNamespace(
        _vehicle_position_url="http://feed.invalid/vp", _headers={}, _trip_id="T1",
        _trip_list=["T1", "T2"], _direction="0", _route_id="R1", _icon="mdi:bus",
        _data={"file": "src", "schedule": schedule,
               "next_departure": {"route_short_name": "N1"}},
        # the rider's destination, which the title used to show
        config_entry=types.SimpleNamespace(data={"destination": "S9: Mairie"}))
    body = gtfs_rt_helper.get_rt_vehicle_positions(me)
    titles = sorted(e["properties"]["title"] for e in body)
    assert titles == ["N1 → Lac 101_bus", "N1 → Stade 102_bus"]
    schedule.engine.dispose()


def test_the_database_direction_places_the_vehicle(tmp_path, monkeypatch):
    # the import repaired T1 to direction 0 and T2 to 1; the feed still
    # carries the provider's, the other way round
    engine = create_engine(f"sqlite:///{tmp_path / 'dir.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table trips (trip_id varchar, route_id varchar, "
                          "direction_id integer, trip_headsign varchar)"))
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, stop_sequence integer)"))
        conn.execute(text("insert into trips values ('T1', 'R1', 0, 'Lac'), ('T2', 'R1', 1, 'Gare')"))
    schedule = types.SimpleNamespace(engine=engine)
    feed = [_vehicle("T1", "101"), _vehicle("T2", "102")]
    feed[0]["vehicle"]["trip"]["direction_id"] = "1"
    feed[1]["vehicle"]["trip"]["direction_id"] = "0"
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities", lambda **kw: feed)
    monkeypatch.setattr(gtfs_rt_helper, "update_geojson", lambda me: None)
    me = types.SimpleNamespace(
        _vehicle_position_url="http://feed.invalid/vp", _headers={}, _trip_id="T9",
        _trip_list=[], _direction="0", _route_id="R1", _icon="mdi:bus",
        _data={"file": "src", "schedule": schedule, "next_departure": {"route_short_name": "N1"}})
    body = gtfs_rt_helper.get_rt_vehicle_positions(me)
    assert [e["properties"]["trip_id"] for e in body] == ["T1"]
    assert body[0]["properties"]["direction_id"] == "0"
    engine.dispose()
