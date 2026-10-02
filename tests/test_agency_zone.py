"""The zone a feed writes its clocks in, read one way for every reader.

The departure query, the leg file and the realtime window each asked the
agency table on their own: the leg file took the first agency even when it
names no zone, and then fell back on the stop's while the departures read
another agency's. agency_zone is the one rule: the route's agency, else the
first agency that names a zone.
"""
from __future__ import annotations

import types

from sqlalchemy import create_engine, text

import ha_stub

clocks = ha_stub.load("clocks")


def _schedule(tmp_path, agencies, routes):
    engine = create_engine(f"sqlite:///{tmp_path / 'feed.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table agency (agency_id text, agency_timezone text)"))
        conn.execute(text("create table routes (route_id text, agency_id text)"))
        for agency_id, zone in agencies:
            conn.execute(text("insert into agency values (:a, :z)"), {"a": agency_id, "z": zone})
        for route_id, agency_id in routes:
            conn.execute(text("insert into routes values (:r, :a)"), {"r": route_id, "a": agency_id})
    return types.SimpleNamespace(engine=engine)


def _name(zone):
    return str(zone) if zone is not None else None


def test_the_route_s_agency_leads(tmp_path):
    schedule = _schedule(tmp_path, [("A", "Europe/Paris"), ("B", "America/New_York")], [("R", "B")])
    assert _name(clocks.agency_zone(schedule, "R")) == "America/New_York"


def test_a_first_agency_without_a_zone_is_passed_over(tmp_path):
    schedule = _schedule(tmp_path, [("A", ""), ("B", "Europe/Paris")], [("R", "X")])
    assert _name(clocks.agency_zone(schedule, "R")) == "Europe/Paris"
    assert _name(clocks.agency_zone(schedule)) == "Europe/Paris"


def test_a_feed_naming_no_zone_answers_none(tmp_path):
    schedule = _schedule(tmp_path, [("A", "")], [])
    assert clocks.agency_zone(schedule) is None
