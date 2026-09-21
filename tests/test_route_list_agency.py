"""The line list of one operator, whatever its agency_id holds.

The agency and route type the flow hands in are the user's pick, and
were written into the query: an agency_id holding a quote broke the
list. They are bound now.
"""
from __future__ import annotations

import types

from sqlalchemy import create_engine, text

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


def _schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table agency (agency_id varchar, agency_name varchar)"))
        conn.execute(text("create table routes (route_id varchar, agency_id varchar, route_type integer, "
                          "route_short_name varchar, route_long_name varchar)"))
        conn.execute(text("create table trips (trip_id varchar, route_id varchar, direction_id integer)"))
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, stop_sequence integer)"))
        conn.execute(text("insert into agency values ('L''Autocar', 'L''Autocar'), ('TAO', 'TAO')"))
        conn.execute(text("insert into routes values ('R1', 'L''Autocar', 3, '1', 'Gare - Lac'), "
                          "('R2', 'TAO', 3, '2', 'Gare - Stade'), ('R3', 'TAO', 0, 'A', 'Tram A')"))
        conn.execute(text("insert into trips values ('T1', 'R1', 0), ('T2', 'R2', 0), ('T3', 'R3', 0)"))
    return types.SimpleNamespace(engine=engine)


def _ids(routes):
    return sorted(value.split("##")[1] for value in routes)


def test_an_agency_id_with_a_quote(tmp_path):
    routes = gtfs_helper.get_route_list(
        _schedule(tmp_path), {"agency": "L'Autocar: L'Autocar", "route_type": "99", "file": "src"},
        gtfs_dir=str(tmp_path))
    assert _ids(routes) == ["R1"]


def test_every_agency_and_one_route_type(tmp_path):
    routes = gtfs_helper.get_route_list(
        _schedule(tmp_path), {"agency": "0: ALL", "route_type": "0", "file": "src"},
        gtfs_dir=str(tmp_path))
    assert _ids(routes) == ["R3"]


def test_the_count_is_the_list_length(tmp_path):
    # the route screen shows this number beside the list it offers
    schedule = _schedule(tmp_path)
    for agency in ("0: ALL", "TAO: TAO", "L'Autocar: L'Autocar"):
        for route_type in ("99", "3", "0"):
            data = {"agency": agency, "route_type": route_type, "file": "src"}
            assert gtfs_helper.get_route_count(schedule, data) == len(
                gtfs_helper.get_route_list(schedule, data)), data
