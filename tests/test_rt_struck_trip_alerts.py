"""A struck trip's alerts leave with it.

When the feed cancels the next departure, the board moves to the one
after it; the alerts had been read for the cancelled one, its trip and
its stop, and stayed on the board.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

refresh_steps = ha_stub.load("refresh_steps")


def test_the_alerts_follow_the_departure_now_shown(monkeypatch):
    read_for = []
    monkeypatch.setattr(refresh_steps, "get_rt_alerts",
                        lambda me: read_for.append(me._trip_id) or {"origin": f"about {me._trip_id}"})
    monkeypatch.setattr(refresh_steps, "get_next_services", lambda me: {})
    monkeypatch.setattr(refresh_steps, "drop_departure_trips",
                        lambda hass, data, struck: {"trip_id": "T2", "origin_stop_id": "S1",
                                                    "next_departures_trip_id": ["T3"]})

    async def job(fn, *args):
        return fn(*args)

    me = types.SimpleNamespace(
        hass=types.SimpleNamespace(async_add_executor_job=job),
        _struck_cancelled={"T1": {"20260922"}}, _struck_skipped={},
        _remember_struck=lambda: None, _get_next_service={},
        _data={"next_departure": {"trip_id": "T1", "next_departures_trip_id": ["T2", "T3"]},
               "departure_rows": [object()], "alert": {"origin": "about T1"}})
    asyncio.run(refresh_steps.drop_struck_trips(
        me, {"origin": "S1: One", "direction": "0"}, False))
    assert read_for == ["T2"]
    assert me._data["alert"] == {"origin": "about T2"}
