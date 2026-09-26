"""A kept zip that cannot be built does not hold the source for ever.

A source that fetched an edition and did not build it is rebuilt from
its kept zip at the next check, before the host is asked. When that
build is refused (the edition lacks a line a sensor reads, or it is
broken) the check stopped there, every night, and the host was never
asked again: the publisher's corrected edition never came in.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

source_refresh = ha_stub.load("source_refresh")


def _check(monkeypatch, built):
    calls = []

    async def rebuild(hass, entry, *, use_zip=False, flags=None):
        calls.append(("rebuild", use_zip))
        return built

    def probe(data, zip_path):
        calls.append(("probe",))
        return {"result": source_refresh.PROBE_UNCHANGED}

    monkeypatch.setattr(source_refresh, "rebuild_pending", lambda hass, file: True)
    monkeypatch.setattr(source_refresh, "async_refresh_source", rebuild)
    monkeypatch.setattr(source_refresh, "refresh_data_for", lambda hass, entry: {"file": "src"})
    monkeypatch.setattr(source_refresh, "probe_source", probe)
    # the record of a look, kept for the catch-up, when there is one
    monkeypatch.setattr(source_refresh, "note_checked", lambda zip_path: None, raising=False)
    monkeypatch.setattr(source_refresh, "_zip_path", lambda hass, file: "src.zip")
    monkeypatch.setattr(source_refresh, "async_dispatcher_send", lambda *a: None)

    async def job(fn, *args):
        return fn(*args)

    hass = types.SimpleNamespace(data={}, async_add_executor_job=job)
    entry = types.SimpleNamespace(
        data={"file": "src", "extract_from": "url", "url": "https://h/src.zip"},
        options={"static_refresh_mode": source_refresh.STATIC_REFRESH_AUTO})
    asyncio.run(source_refresh.async_check_source(hass, entry))
    return calls


def test_a_refused_rebuild_asks_the_host(monkeypatch):
    assert _check(monkeypatch, built=False)[:2] == [("rebuild", True), ("probe",)]


def test_a_rebuild_that_went_through_ends_the_check(monkeypatch):
    assert _check(monkeypatch, built=True) == [("rebuild", True)]
