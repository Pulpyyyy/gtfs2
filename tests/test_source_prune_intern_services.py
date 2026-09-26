"""Which sources the prune and intern services work on, and what they say.

The field is a device picker, so a call usually names the sources by their
device ids; yaml calls and old automations pass datasource names, one bare
string or a list, and an entity id resolves too. A name nobody knows is
reported, not attempted, and a call naming nothing sweeps every source.

Pruning keeps the lines the sensors read. A source a train or local stops
sensor reads whole, and one no sensor reads at all, is skipped with its
reason instead of being emptied. Neither service touches a source whose
refresh is running: the file is about to be swapped for a new one, and
the work would go with it. A dry run only counts, on the live file.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

integration = ha_stub.load("__init__")
source_refresh = ha_stub.load("source_refresh")


def _entry(entry_id, domain="gtfs2", **data):
    return types.SimpleNamespace(entry_id=entry_id, domain=domain, data=data, options={})


class _Hass:
    def __init__(self, entries=()):
        self.data = {}
        self.jobs = []
        entries = list(entries)
        self.config = types.SimpleNamespace(path=lambda *p: "/config/" + "/".join(p))
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda domain: list(entries),
            async_get_entry=lambda entry_id: next(
                (e for e in entries if e.entry_id == entry_id), None))

    async def async_add_executor_job(self, fn, *args):
        self.jobs.append((fn, args))
        return {"file": args[1]}


def _registries(monkeypatch, devices=None, entities=None):
    """The device and entity registries, as the ids a call may carry."""
    devices, entities = devices or {}, entities or {}
    monkeypatch.setattr(integration, "dr", types.SimpleNamespace(
        async_get=lambda hass: types.SimpleNamespace(async_get=devices.get)))
    monkeypatch.setattr(integration, "er", types.SimpleNamespace(
        async_get=lambda hass: types.SimpleNamespace(async_get=entities.get)))


def _prune(hass, **data):
    return asyncio.run(integration.async_prune_datasources(hass, data))


def _intern(hass, **data):
    return asyncio.run(integration.async_intern_datasources(hass, data))


def test_a_call_names_its_sources_every_way(monkeypatch):
    other_domain = _entry("x", domain="zone", file="zou")
    entries = [_entry("d1", file="tao", kind="datasource"),
               _entry("j1", file="sncf", route="K5: Car"), other_domain]
    _registries(monkeypatch,
                devices={"dev-tao": types.SimpleNamespace(config_entries={"d1"}),
                         "dev-zone": types.SimpleNamespace(config_entries={"x"})},
                entities={"sensor.car": types.SimpleNamespace(config_entry_id="j1"),
                          "sensor.orphan": types.SimpleNamespace(config_entry_id=None)})
    hass = _Hass(entries)
    assert integration._wanted_files(hass, ["dev-tao", "sensor.car", "palm"]) == [
        "tao", "sncf", "palm"]
    # one bare string, an empty one, nothing at all
    assert integration._wanted_files(hass, "palm") == ["palm"]
    assert integration._wanted_files(hass, "") == []
    assert integration._wanted_files(hass, None) == []
    # a device or entity that is not a source of ours reads as a plain name
    assert integration._wanted_files(hass, ["dev-zone", "sensor.orphan"]) == [
        "dev-zone", "sensor.orphan"]


def test_prune_keeps_the_lines_read_and_skips_what_it_must(monkeypatch):
    _registries(monkeypatch)
    hass = _Hass([
        _entry("d1", file="tao", kind="datasource"),
        _entry("j1", file="tao", route="R1: Line 1"),
        _entry("j2", file="tao", route="R2"),
        _entry("j3", file="sncf", route="train"),
        _entry("j4", file="palm", device_tracker_id="person.me"),
        _entry("j5", file="zou", route=""),
        _entry("d2", file="idle", kind="datasource")])
    result = _prune(hass)
    assert result == {"pruned": [{"file": "tao"}], "skipped": [
        {"file": "idle", "reason": "no_sensor_reads_it"},
        {"file": "palm", "reason": "whole_feed_in_use"},
        {"file": "sncf", "reason": "whole_feed_in_use"},
        {"file": "zou", "reason": "whole_feed_in_use"}]}
    # on a copy swapped in, with the lines the sensors read
    [(fn, args)] = hass.jobs
    assert fn is integration.on_a_copy
    assert args[1:] == ("tao", integration.prune_gtfs_datasource, {"R1", "R2"}, False)


def test_a_dry_run_only_counts(monkeypatch):
    _registries(monkeypatch)
    hass = _Hass([_entry("j1", file="tao", route="R1: Line 1")])
    assert _prune(hass, dry_run=True) == {"pruned": [{"file": "tao"}], "skipped": []}
    assert _intern(hass, dry_run=True) == {"interned": [{"file": "tao"}]}
    assert [(fn, args[1:]) for fn, args in hass.jobs] == [
        (integration.prune_gtfs_datasource, ("tao", {"R1"}, True)),
        (integration.intern_gtfs_datasource, ("tao", True))]


def test_an_unknown_name_is_reported_not_attempted(monkeypatch):
    _registries(monkeypatch)
    hass = _Hass([_entry("j1", file="tao", route="R1: Line 1")])
    assert _prune(hass, file=["nope"]) == {"pruned": [], "skipped": [], "unknown": ["nope"]}
    assert _intern(hass, file=["nope"]) == {"interned": [], "unknown": ["nope"]}
    assert hass.jobs == []
    # a known one beside it still runs, and the unknown one is still said
    assert _prune(hass, file=["nope", "tao"])["unknown"] == ["nope"]
    assert _intern(hass, file=["tao", "nope"]) == {"interned": [{"file": "tao"}],
                                                   "unknown": ["nope"]}


def test_nothing_runs_on_a_source_being_refreshed(monkeypatch):
    _registries(monkeypatch)

    async def run():
        hass = _Hass([_entry("j1", file="tao", route="R1: Line 1")])
        async with source_refresh.source_lock(hass, "tao"):
            pruned = await integration.async_prune_datasources(hass, {"file": "tao"})
            interned = await integration.async_intern_datasources(hass, {})
        return hass, pruned, interned

    hass, pruned, interned = asyncio.run(run())
    assert pruned == {"pruned": [], "skipped": [{"file": "tao", "reason": "refresh_running"}]}
    assert interned == {"interned": [], "skipped": [{"file": "tao", "reason": "refresh_running"}]}
    assert hass.jobs == []


def test_a_source_the_work_left_alone_is_not_listed(monkeypatch):
    _registries(monkeypatch)
    hass = _Hass([_entry("j1", file="tao", route="R1: Line 1")])

    async def nothing(fn, *args):
        return None

    hass.async_add_executor_job = nothing
    assert _prune(hass) == {"pruned": [], "skipped": []}
    assert _intern(hass) == {"interned": []}
