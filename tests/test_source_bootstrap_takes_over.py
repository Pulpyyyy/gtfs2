"""The bootstrap gives each source its entry, then its journeys let go.

An install coming from the upstream version has journeys only, each
holding the source's address, key and realtime feeds. The first start
creates the source's datasource entry from them; once it exists, the
copies on the journeys are dropped: the source holds them, and a return
to the upstream version, the only reader of the copies, is not kept. A
journey whose source could not be created keeps its copies, for the next
start to take over.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

ha_stub.install()

source_entries = ha_stub.load("feed.source_entries")


class _Entries:
    """hass.config_entries as the bootstrap uses it, the import flow included."""

    def __init__(self, entries, refuse=()):
        self.entries = entries
        self.refuse = set(refuse)
        self.flow = types.SimpleNamespace(async_init=self._create)

    def async_entries(self, domain=None):
        return list(self.entries)

    def async_update_entry(self, entry, data=None, options=None):
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)
        return True

    async def _create(self, domain, context=None, data=None):
        data = dict(data)
        if data["file"] in self.refuse:
            raise RuntimeError("refused")
        options = data.pop("options", None) or {}
        self.entries.append(_entry(data["file"], data, options))
        return {"type": "create_entry"}


def _entry(title, data, options=None):
    return types.SimpleNamespace(title=title, data=dict(data), options=dict(options or {}),
                                 modified_at=None)


UPSTREAM = {"file": "tao", "url": "https://host/tao.zip", "extract_from": "url",
            "api_key": "s-key", "api_key_name": "token", "api_key_location": "header",
            "route": "R1", "origin": "S1: One", "destination": "S2: Two", "name": "to work"}
REALTIME = {"trip_update_url": "https://host/trips", "real_time": True, "refresh_interval": 5}


def test_the_source_takes_over_and_its_journeys_let_go():
    journey = _entry("to work", UPSTREAM, REALTIME)
    hass = types.SimpleNamespace(config_entries=_Entries([journey]))
    asyncio.run(source_entries.async_bootstrap_datasource_entries(hass, ["tao"]))
    source = source_entries.datasource_entry(hass, "tao")
    assert source.data["url"] == "https://host/tao.zip"
    assert source.data["api_key"] == "s-key"
    assert source.options["trip_update_url"] == "https://host/trips"
    assert journey.data == {"file": "tao", "route": "R1", "origin": "S1: One",
                            "destination": "S2: Two", "name": "to work"}
    assert journey.options == {"refresh_interval": 5}


def test_a_journey_whose_source_could_not_be_made_keeps_its_copies():
    journey = _entry("to work", UPSTREAM, REALTIME)
    hass = types.SimpleNamespace(config_entries=_Entries([journey], refuse={"tao"}))
    asyncio.run(source_entries.async_bootstrap_datasource_entries(hass, ["tao"]))
    assert source_entries.datasource_entry(hass, "tao") is None
    assert journey.data == UPSTREAM
    assert journey.options == REALTIME
