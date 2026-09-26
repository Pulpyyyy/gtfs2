"""The departures service answers only for a journey.

Its entry picker offers every gtfs2 entry, the datasource and local stops
ones among them. Neither has two ends: reading them raised a bare
KeyError, after a schedule had been opened that nothing then closed.
"""
from __future__ import annotations

import types

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


def _run(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    raise RuntimeError("the service awaited something the test does not stand in for")


def test_a_source_or_local_stops_entry_lists_nothing(monkeypatch):
    opened = []
    schedule = types.SimpleNamespace(engine=types.SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(gtfs_helper, "get_gtfs", lambda *a, **k: opened.append(a) or schedule)

    async def job(fn, *args):
        return fn(*args)

    for data in ({"kind": "datasource", "file": "tao", "url": "na"},
                 {"file": "tao", "device_tracker_id": "person.me", "name": "around me"}):
        entry = types.SimpleNamespace(data=data, options={})
        hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(async_get_entry=lambda _id, e=entry: e),
            async_add_executor_job=job)
        got = _run(gtfs_helper.get_route_departures(hass, {"config_entry": "e"}))
        assert (got["today"], got["tomorrow"]) == ([], [])
    assert opened == []
