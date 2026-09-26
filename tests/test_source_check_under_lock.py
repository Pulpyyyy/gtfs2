"""The scheduled check downloads under the source's lock.

Every writer of a source takes its lock; the check's download did not,
though it writes the same zip.new a refresh stages into and can last half
an hour. A refresh started meanwhile wrote the file under it.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

source_refresh = ha_stub.load("source_refresh")


def test_the_download_holds_the_lock(monkeypatch):
    held = []

    async def job(fn, *args):
        return fn(*args)

    hass = types.SimpleNamespace(data={}, async_add_executor_job=job)

    def fetch_if_new(data, zip_path, *rest):
        held.append(source_refresh.source_lock(hass, "src").locked())
        return False

    monkeypatch.setattr(source_refresh, "rebuild_pending", lambda hass, file: False, raising=False)
    monkeypatch.setattr(source_refresh, "refresh_data_for", lambda hass, entry: {"file": "src"})
    monkeypatch.setattr(source_refresh, "probe_source",
                        lambda data, zip_path: {"result": source_refresh.PROBE_UNKNOWN})
    monkeypatch.setattr(source_refresh, "note_checked", lambda zip_path: None, raising=False)
    monkeypatch.setattr(source_refresh, "_carry_validators", lambda hass, file: None, raising=False)
    monkeypatch.setattr(source_refresh, "_zip_path", lambda hass, file: "src.zip")
    monkeypatch.setattr(source_refresh, "fetch_if_new", fetch_if_new)
    monkeypatch.setattr(source_refresh, "async_dispatcher_send", lambda *a: None)
    entry = types.SimpleNamespace(
        data={"file": "src", "extract_from": "url", "url": "https://h/src.zip"},
        options={"static_refresh_mode": source_refresh.STATIC_REFRESH_AUTO})
    asyncio.run(source_refresh.async_check_source(hass, entry))
    assert held == [True]
    assert not source_refresh.source_lock(hass, "src").locked()
