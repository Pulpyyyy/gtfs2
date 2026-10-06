"""A trip the board gets on elsewhere is struck when it skips that stop.

The feed is matched at the stop of the departure shown. A board listing
runs from two stops (a place served from two quays, an entry getting on
at more stops) kept the runs that skip the other stop until one became
the departure shown, and the board read again after a drop only struck
them a minute later: the first state of a tram A sensor listed trips
the feed had struck (field test of 2026-10-06).
"""
from __future__ import annotations

import asyncio
import datetime
import types

from freezegun import freeze_time

import ha_stub

realtime = ha_stub.load("domain.realtime")
coordinator = ha_stub.load("coordinator")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 6, 10, 30, tzinfo=UTC)
SHOWN = datetime.datetime(2026, 10, 6, 10, 48, tzinfo=UTC)


def _update(stop_id, relationship=None):
    update = {"stop_id": stop_id, "arrival": {"delay": 0, "time": 0}, "departure": {"delay": 0, "time": 0}}
    if relationship:
        update["schedule_relationship"] = relationship
    return update


def _skipped(feed):
    """What one reading struck, the board getting on HERE at S1 and FAR at S2."""
    departure = {"trip_id": "HERE", "departure_time": SHOWN,
                 "next_departures_trip_id": ["HERE", "FAR", "PAST"],
                 "next_departures_origin_stop_id": ["S1", "S2", "S2"],
                 "next_departures": [SHOWN] * 3}
    me = types.SimpleNamespace(
        _data={"file": "src", "next_departure": departure, "schedule": None},
        _rt_group="route", _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://feed.invalid/rt",
        _route_id="R1", _trip_id="HERE", _trip_short_name="", _direction="0",
        _stop_id="S1", _stop_sequence=None, _trip_list=["HERE", "FAR", "PAST"])
    entities = [{"id": trip, "trip_update": {"trip": {"trip_id": trip, "route_id": "R1", "direction_id": "0"},
                                             "stop_time_update": updates}}
                for trip, updates in feed.items()]
    with freeze_time(NOW):
        realtime.get_rt_route_trip_statuses(me, entities)
    return set(me._rt_skipped)


def test_a_run_skipping_the_stop_the_board_gets_on_it_at_is_struck():
    assert _skipped({"HERE": [_update("S1")],
                     "FAR": [_update("S2", "SKIPPED"), _update("S3")],
                     # skips a stop after the one it is boarded at: it runs
                     "PAST": [_update("S2"), _update("S3", "SKIPPED")]}) == {"FAR"}


def test_what_the_board_read_again_turns_up_is_struck_in_the_same_cycle(monkeypatch):
    """The first drop moves the board on; the reading that follows strikes
    the next one too, and it goes at once."""
    # what each fold of the readings adds: the cycle's own, then the one
    # after the first drop
    readings = iter([{}, {"T2": {None}}, {}])
    boards = {"T1": {"trip_id": "T2", "next_departures_trip_id": ["T2", "T3"]},
              "T2": {"trip_id": "T3", "next_departures_trip_id": ["T3"]}}
    dropped = []

    def drop(hass, data, struck):
        dropped.append(sorted(struck))
        return boards[data["next_departure"]["trip_id"]]

    def remember():
        me._struck_skipped.update(next(readings, {}))

    async def job(fn, *args):
        return fn(*args)

    monkeypatch.setattr(coordinator, "drop_departure_trips", drop)
    monkeypatch.setattr(coordinator, "get_next_services", lambda me: {})
    monkeypatch.setattr(coordinator, "get_rt_alerts", lambda me: {})
    me = types.SimpleNamespace(
        hass=types.SimpleNamespace(async_add_executor_job=job),
        _struck_cancelled={"T1": {None}}, _struck_skipped={},
        _remember_struck=lambda: None, _get_next_service={},
        _data={"next_departure": {"trip_id": "T1", "next_departures_trip_id": ["T1", "T2", "T3"]},
               "departure_rows": [object()], "alert": {}})
    me._remember_struck = remember
    me._follow_departure = types.MethodType(coordinator.GTFSUpdateCoordinator._follow_departure, me)
    asyncio.run(coordinator.drop_struck_trips(
        me, {"origin": "S1: One", "direction": "0", "route": "R1"}, False))
    assert dropped == [["T1"], ["T1", "T2"]]
    assert me._data["next_departure"]["trip_id"] == "T3"
