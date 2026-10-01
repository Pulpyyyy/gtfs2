"""What each service does with a call, once its schema let it through.

update_gtfs refreshes a source that exists from what the source knows of
itself: its own address and key apply. An address the call gives that is
not the source's own is fetched from this once, with the call's key
alone; a key given without one is told so in the log rather than
silently obeyed or refused. The call sets the per-import flags; where the feed
is read from is the source's url's to say, and the schema drops the
extract_from an older call names (test_service_schemas). A source that does not exist yet is created from the
call's fields, and only one that was built gets its datasource entry,
born with the address and key it came from; without an address, it is
the zip in the gtfs2 folder, by its file:// url.

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
services = ha_stub.load("datasource_services")


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

    async def refresh(hass, entry_, *, use_zip=False, flags=None, fetch=None):
        refreshes.append((entry_, use_zip, flags, fetch))
        return True

    monkeypatch.setattr(services, "datasource_entry", lambda hass, file: entry)
    monkeypatch.setattr(services, "refresh_data_for",
                        lambda hass, e: {"url": "https://tao/gtfs.zip", "api_key": "k1"})
    monkeypatch.setattr(services, "async_refresh_source", refresh)
    _, handlers = _handlers()
    with caplog.at_level(logging.WARNING):
        assert _run(handlers["update_gtfs"], _call(
            file="tao", extract_from="zip", api_key="k2", clean_feed_info=True,
            older_field=1)) is True
    # fetched from its url: the zip the call names is not read instead,
    # and a key alone is the source's to hold, which the call is told
    assert refreshes == [(entry, False, {"clean_feed_info": True}, None)]
    warned = [r.getMessage() for r in caplog.records if "differs" in r.getMessage()]
    assert len(warned) == 1 and "api_key" in warned[0]


def test_update_gtfs_fetches_once_from_an_address_given(monkeypatch, caplog):
    entry = types.SimpleNamespace(entry_id="d", data={"file": "tao"})
    refreshes = []

    async def refresh(hass, entry_, *, use_zip=False, flags=None, fetch=None):
        refreshes.append(fetch)
        return True

    monkeypatch.setattr(services, "datasource_entry", lambda hass, file: entry)
    monkeypatch.setattr(services, "refresh_data_for",
                        lambda hass, e: {"url": "https://tao/gtfs.zip", "api_key": "k1"})
    monkeypatch.setattr(services, "async_refresh_source", refresh)
    _, handlers = _handlers()
    with caplog.at_level(logging.WARNING):
        # the host down: the zip already in the folder, and no key with it
        _run(handlers["update_gtfs"], _call(file="tao", url="file:///config/gtfs2/tao.zip"))
        # a mirror that wants a key of its own
        _run(handlers["update_gtfs"], _call(file="tao", url="https://mirror/gtfs.zip",
                                            api_key="m", api_key_location="header"))
    assert refreshes == [
        {"url": "file:///config/gtfs2/tao.zip", "api_key_location": "not_applicable"},
        {"url": "https://mirror/gtfs.zip", "api_key": "m", "api_key_name": "api_key",
         "api_key_location": "header"}]
    # an address of the call's own is no mistake to warn about
    assert not [r for r in caplog.records if "differs" in r.getMessage()]


def test_update_gtfs_is_quiet_when_the_call_repeats_the_source(monkeypatch, caplog):
    refreshes = []

    async def refresh(hass, entry_, *, use_zip=False, flags=None, fetch=None):
        refreshes.append((use_zip, flags, fetch))
        return False

    monkeypatch.setattr(services, "datasource_entry", lambda hass, file: object())
    monkeypatch.setattr(services, "refresh_data_for",
                        lambda hass, e: {"url": "https://tao/gtfs.zip"})
    monkeypatch.setattr(services, "async_refresh_source", refresh)
    _, handlers = _handlers()
    with caplog.at_level(logging.WARNING):
        # the same url and a blank key say nothing new (the "na" an older
        # call sends is the schema's to drop: test_service_schemas)
        assert _run(handlers["update_gtfs"], _call(
            file="tao", url=" https://tao/gtfs.zip ",
            check_source_dates=False)) is False
        _run(handlers["update_gtfs"], _call(file="tao", api_key=" "))
    assert refreshes == [(False, {"check_source_dates": False}, None), (False, {}, None)]
    assert not [r for r in caplog.records if "differs" in r.getMessage()]


def test_update_gtfs_creates_a_source_it_does_not_know(monkeypatch):
    for built in (True, False):
        refreshed, created = [], []

        async def refresh_data(hass, file, data):
            refreshed.append((file, data))
            return built

        async def ensure(hass, file, *, url, api):
            created.append((file, url))

        monkeypatch.setattr(services, "datasource_entry", lambda hass, file: None)
        monkeypatch.setattr(services, "source_zip_url",
                            lambda hass, file: "file:///config/gtfs2/new.zip")
        monkeypatch.setattr(services, "async_refresh_source_data", refresh_data)
        monkeypatch.setattr(services, "async_ensure_datasource_entry", ensure)
        _, handlers = _handlers()
        assert _run(handlers["update_gtfs"], _call(file="new")) is built
        # the fields the service always defaulted, for the legacy import;
        # the url is the zip in the folder, where "na" stood before
        url = "file:///config/gtfs2/new.zip"
        assert refreshed == [("new", {"file": "new", "url": url, "extract_from": "url"})]
        # only a source that was built gets its entry
        assert created == ([("new", url)] if built else [])


def test_a_created_source_keeps_the_address_it_came_from(monkeypatch):
    created = []

    async def refresh_data(hass, file, data):
        return True

    async def ensure(hass, file, *, url, api):
        created.append((file, url, api.get("api_key")))

    monkeypatch.setattr(services, "datasource_entry", lambda hass, file: None)
    monkeypatch.setattr(services, "async_refresh_source_data", refresh_data)
    monkeypatch.setattr(services, "async_ensure_datasource_entry", ensure)
    _, handlers = _handlers()
    _run(handlers["update_gtfs"], _call(file="new", url="https://new/gtfs.zip",
                                        extract_from="zip", api_key="k"))
    assert created == [("new", "https://new/gtfs.zip", "k")]


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
