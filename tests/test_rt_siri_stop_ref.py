"""The SIRI request asks for the entry's origin by its whole stop id.

An entry stores its origin as "id: name", and the id stands before the
first ": " (const.id_of). The SIRI branch cut at the first ":" instead, so
a stop id holding one (SNCF's StopPoint:OCE..., IDFM:...) asked the host
for "StopPoint".
"""
from __future__ import annotations

import types

import ha_stub

rt_local = ha_stub.load("rt_local")


def _asked(tmp_path, monkeypatch, origin):
    """The stop id the SIRI converter is handed for an entry of this origin."""
    entry = types.SimpleNamespace(data={"origin": origin}, options={})
    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        config_entries=types.SimpleNamespace(async_get_entry=lambda entry_id: entry))
    registry = types.SimpleNamespace(
        async_get=lambda entity_id: types.SimpleNamespace(config_entry_id="E"))
    monkeypatch.setattr(rt_local.er, "async_get", lambda _hass: registry)
    asked = []
    monkeypatch.setattr(rt_local, "convert_realtime_siri_trips_to_json",
                        lambda url, headers, stop_id: asked.append(stop_id) or {})
    rt_local.get_gtfs_rt(hass, "gtfs2", {
        "url": "https://h/siri", "file": "src", "entity_for_siri": "sensor.trip"})
    return asked


def test_a_stop_id_holding_a_colon_is_asked_whole(tmp_path, monkeypatch):
    assert _asked(tmp_path, monkeypatch, "StopPoint:OCE87581009: Bordeaux Saint-Jean") == [
        "StopPoint:OCE87581009"]


def test_a_plain_stop_id_is_asked_as_before(tmp_path, monkeypatch):
    assert _asked(tmp_path, monkeypatch, "GACEN_20: Gare Centrale") == ["GACEN_20"]
