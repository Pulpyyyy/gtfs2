"""The button that refreshes a source whatever its versions say.

Home Assistant refuses an update install when installed and latest agree,
which is always the case with the checks off, so the update entity could
not refresh such a source. The button runs the same swap rebuild: from
the kept zip when it is ahead of the database, as the install does, and
it says so when a rebuild is already running or the refresh failed.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import ha_stub

button = ha_stub.load("button")
source_refresh = ha_stub.load("source_refresh")
HomeAssistantError = button.HomeAssistantError


class _Hass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _button(hass):
    entry = types.SimpleNamespace(data={"file": "src", "kind": "datasource"},
                                  options={}, entry_id="e1")
    return button.GTFSSourceRefreshButton(hass, entry), entry


def test_a_press_refreshes_from_the_zip_only_when_it_is_ahead(monkeypatch):
    for pending in (False, True):
        calls = []

        async def refresh(hass, entry, *, use_zip=False):
            calls.append((entry, use_zip))
            return True

        monkeypatch.setattr(button, "async_refresh_source", refresh)
        monkeypatch.setattr(button, "rebuild_pending", lambda hass, file: pending)
        hass = _Hass()
        entity, entry = _button(hass)
        asyncio.run(entity.async_press())
        assert calls == [(entry, pending)]


def test_a_failed_refresh_says_so(monkeypatch):
    async def refresh(hass, entry, *, use_zip=False):
        return False

    monkeypatch.setattr(button, "async_refresh_source", refresh)
    monkeypatch.setattr(button, "rebuild_pending", lambda hass, file: False)
    entity, _ = _button(_Hass())
    with pytest.raises(HomeAssistantError, match="failed"):
        asyncio.run(entity.async_press())


def test_a_press_during_a_rebuild_starts_nothing(monkeypatch):
    calls = []

    async def refresh(hass, entry, *, use_zip=False):
        calls.append(entry)
        return True

    monkeypatch.setattr(button, "async_refresh_source", refresh)

    async def run():
        hass = _Hass()
        entity, _ = _button(hass)
        async with source_refresh.source_lock(hass, "src"):
            with pytest.raises(HomeAssistantError, match="already running"):
                await entity.async_press()
    asyncio.run(run())
    assert calls == []


def test_one_button_per_datasource_only():
    added = []
    journey = types.SimpleNamespace(data={"file": "src"}, options={}, entry_id="j1")
    datasource = types.SimpleNamespace(data={"file": "src", "kind": "datasource"},
                                       options={}, entry_id="d1")
    for entry in (journey, datasource):
        asyncio.run(button.async_setup_entry(_Hass(), entry, added.extend))
    assert [b._file for b in added] == ["src"]
    assert added[0]._attr_unique_id == "gtfs2_source_refresh_src"
