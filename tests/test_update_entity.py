"""The update entity of a source: its rebuild in progress, whatever started
it, and its versions read as labels.

Home Assistant reads in_progress only from an entity that declares
UpdateEntityFeature.PROGRESS, and otherwise shows a rebuild only while it
runs its own install: the night check and the update service rebuilt the
source with the entity saying nothing. The entity declares it, follows the
source's lock, and is told when a rebuild starts as well as when it ends.

Its versions are a Last-Modified date, an etag or a hash prefix. Home
Assistant orders them with AwesomeVersion once they differ, which reads an
all-digit hash as a number: a new feed whose hash was smaller read as up
to date. Any other version is the newer one.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

update = ha_stub.load("update")
source_refresh = ha_stub.load("source_refresh")


class _Hass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _entity(hass):
    entry = types.SimpleNamespace(data={"file": "src", "kind": "datasource"},
                                  options={}, entry_id="e1")
    return update.GTFSSourceUpdateEntity(hass, entry)


def test_the_entity_declares_its_progress():
    features = update.GTFSSourceUpdateEntity._attr_supported_features
    assert features & update.UpdateEntityFeature.PROGRESS
    assert features & update.UpdateEntityFeature.INSTALL


def test_in_progress_follows_the_source_lock():
    async def run():
        hass = _Hass()
        entity = _entity(hass)
        assert entity.in_progress is False
        lock = source_refresh.source_lock(hass, "src")
        async with lock:
            # held by a rebuild the entity did not start
            assert entity.in_progress is True
        assert entity.in_progress is False
    asyncio.run(run())


def test_a_rebuild_is_told_when_it_starts(monkeypatch):
    """The signal goes out with the lock held, so the entity writes a state
    that reads in progress, and once more after it is released."""
    hass = _Hass()
    seen = []

    def send(hass_, signal):
        seen.append((signal, source_refresh.source_lock(hass, "src").locked()))

    async def notify(*args):
        return None

    monkeypatch.setattr(source_refresh, "async_dispatcher_send", send)
    monkeypatch.setattr(source_refresh, "async_notify_refresh", notify)
    monkeypatch.setattr(source_refresh, "_lines_read", lambda hass, file: [])
    monkeypatch.setattr(source_refresh, "_reads_whole_feed", lambda hass, file: False)
    monkeypatch.setattr(source_refresh, "refresh_source", lambda hass, path, data: True)
    assert asyncio.run(source_refresh.async_refresh_source_data(hass, "src", {"file": "src"}))
    signal = source_refresh.SIGNAL_SOURCE_REFRESH.format("src")
    assert seen == [(signal, True), (signal, False)]


def test_any_other_version_is_newer():
    entity = _entity(_Hass())
    for latest, installed in [
            ("098765432109", "123456789012"),     # all-digit hash prefixes
            ("1726800000", "1726900000"),         # a numeric etag, smaller
            ("Sat, 20 Sep 2026 19:30:00 GMT", "Fri, 19 Sep 2026 19:30:00 GMT"),
            ("5a0f00c1d2e3", "66e1f2a31b2c")]:
        assert entity.version_is_newer(latest, installed), (latest, installed)
