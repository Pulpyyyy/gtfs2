"""What each service does with a call, once its schema let it through.

update_gtfs refreshes a source that exists from what the source knows of
itself: its own address and key apply, whatever the call says, and a
call that gives others is told so in the log rather than silently
obeyed or refused. The call only picks the kept zip and sets the
per-import flags. A source that does not exist yet is created from the
call's fields, and only one that was built gets its datasource entry,
born with the address and key it came from.

The other services hand their call on, untouched, to the function that
does the work, and give back what it answers: the departures, arrivals,
trip stops and datasource reports are the response an automation reads.
"""
from __future__ import annotations

import asyncio
import logging
import types

import ha_stub

integration = ha_stub.load("__init__")


def _handlers(hass=None):
    handlers = {}

    def register(domain, name, handler, schema=None, **kwargs):
        handlers[name] = handler

    hass = hass or types.SimpleNamespace()
    hass.services = types.SimpleNamespace(register=register)
    assert integration.setup(hass, {}) is True
    return hass, handlers


def _call(**data):
    return types.SimpleNamespace(data=data)


def _run(handler, call):
    result = handler(call)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


def test_update_gtfs_refreshes_a_source_from_its_own_settings(monkeypatch, caplog):
    entry = types.SimpleNamespace(entry_id="d", data={"file": "tao"})
    refreshes = []

    async def refresh(hass, entry_, *, use_zip=False, flags=None):
        refreshes.append((entry_, use_zip, flags))
        return True

    monkeypatch.setattr(integration, "datasource_entry", lambda hass, file: entry)
    monkeypatch.setattr(integration, "refresh_data_for",
                        lambda hass, e: {"url": "https://tao/gtfs.zip", "api_key": "k1"})
    monkeypatch.setattr(integration, "async_refresh_source", refresh)
    _, handlers = _handlers()
    with caplog.at_level(logging.WARNING):
        assert _run(handlers["update_gtfs"], _call(
            file="tao", extract_from="zip", url="https://other/gtfs.zip",
            api_key="k2", clean_feed_info=True, older_field=1)) is True
    assert refreshes == [(entry, True, {"clean_feed_info": True})]
    # the source's own address and key applied: the call is told both differ
    warned = [r.getMessage() for r in caplog.records if "differs" in r.getMessage()]
    assert len(warned) == 2 and "url" in warned[0] and "api_key" in warned[1]


def test_update_gtfs_is_quiet_when_the_call_repeats_the_source(monkeypatch, caplog):
    refreshes = []

    async def refresh(hass, entry_, *, use_zip=False, flags=None):
        refreshes.append((use_zip, flags))
        return False

    monkeypatch.setattr(integration, "datasource_entry", lambda hass, file: object())
    monkeypatch.setattr(integration, "refresh_data_for",
                        lambda hass, e: {"url": "https://tao/gtfs.zip"})
    monkeypatch.setattr(integration, "async_refresh_source", refresh)
    _, handlers = _handlers()
    with caplog.at_level(logging.WARNING):
        # the same url, the "na" placeholder and a blank key say nothing new
        assert _run(handlers["update_gtfs"], _call(
            file="tao", url=" https://tao/gtfs.zip ", api_key="na",
            check_source_dates=False)) is False
        _run(handlers["update_gtfs"], _call(file="tao", api_key=" "))
    assert refreshes == [(False, {"check_source_dates": False}), (False, {})]
    assert not [r for r in caplog.records if "differs" in r.getMessage()]


def test_update_gtfs_creates_a_source_it_does_not_know(monkeypatch):
    for built in (True, False):
        refreshed, created = [], []

        async def refresh_data(hass, file, data):
            refreshed.append((file, data))
            return built

        async def ensure(hass, file, *, url, extract_from, api):
            created.append((file, url, extract_from))

        monkeypatch.setattr(integration, "datasource_entry", lambda hass, file: None)
        monkeypatch.setattr(integration, "async_refresh_source_data", refresh_data)
        monkeypatch.setattr(integration, "async_ensure_datasource_entry", ensure)
        _, handlers = _handlers()
        assert _run(handlers["update_gtfs"], _call(file="new")) is built
        # the fields the service always defaulted, for the legacy import
        assert refreshed == [("new", {"file": "new", "url": "na", "extract_from": "url"})]
        # only a source that was built gets its entry
        assert created == ([("new", "na", "url")] if built else [])


def test_a_created_source_keeps_the_address_it_came_from(monkeypatch):
    created = []

    async def refresh_data(hass, file, data):
        return True

    async def ensure(hass, file, *, url, extract_from, api):
        created.append((file, url, extract_from, api.get("api_key")))

    monkeypatch.setattr(integration, "datasource_entry", lambda hass, file: None)
    monkeypatch.setattr(integration, "async_refresh_source_data", refresh_data)
    monkeypatch.setattr(integration, "async_ensure_datasource_entry", ensure)
    _, handlers = _handlers()
    _run(handlers["update_gtfs"], _call(file="new", url="https://new/gtfs.zip",
                                        extract_from="zip", api_key="k"))
    assert created == [("new", "https://new/gtfs.zip", "zip", "k")]


def test_the_realtime_service_reads_into_the_rt_folder(monkeypatch):
    reads = []
    monkeypatch.setattr(integration, "get_gtfs_rt",
                        lambda hass, path, data: reads.append((hass, path, data)))
    hass, handlers = _handlers()
    data = {"file": "tao", "url": "https://tao/rt", "rt_type": "alerts"}
    assert _run(handlers["update_gtfs_rt_local"], _call(**data)) is True
    assert reads == [(hass, integration.DEFAULT_PATH_RT, data)]


def test_the_other_services_hand_their_call_on(monkeypatch):
    for service, target, answer in (
            ("update_gtfs_local_stops", "update_gtfs_local_stops", True),
            ("extract_departures", "get_route_departures", {"departures": []}),
            ("extract_arrivals", "get_route_arrivals", {"arrivals": []}),
            ("extract_trip_stops", "get_trip_stops", {"stops": []}),
            ("prune_datasource", "async_prune_datasources", {"pruned": []}),
            ("intern_datasource", "async_intern_datasources", {"interned": []})):
        calls = []

        async def work(hass, data, answer=answer):
            calls.append((hass, data))
            return None if answer is True else answer

        monkeypatch.setattr(integration, target, work)
        hass, handlers = _handlers()
        data = {"entity_id": "sensor.bus", "config_entry": "abc"}
        assert _run(handlers[service], _call(**data)) == answer, service
        assert calls == [(hass, data)], service
