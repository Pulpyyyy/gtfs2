"""How the three kinds of entry are set up, unloaded and removed.

A datasource entry runs no coordinator: it forwards to the source's own
platforms, mirrors its realtime settings onto the journey entries on every
edit, and arms the scheduled look at its host, disarmed with the entry. A
journey entry gets the coordinator of its kind, a local stops one when it
follows a person, kept on the entry itself. The first entry set up starts
the walk that gives every source its datasource entry, and only the first.

Unloading a journey entry closes the schedule its coordinator held open,
once its platforms are down. Removing a datasource entry deletes nothing;
removing a journey entry clears its map files and says when it was the
last sensor of a line whose timetable is still in the database.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

integration = ha_stub.load("__init__")


class _Entry:
    def __init__(self, entry_id, options=None, **data):
        self.entry_id = entry_id
        self.data = data
        self.options = options or {}
        self.listeners = []
        self.on_unload = []

    def add_update_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None

    def async_on_unload(self, func):
        self.on_unload.append(func)


class _Hass:
    def __init__(self, entries=(), unloads=True):
        self.data = {}
        self.tasks = []
        self.forwarded = []
        self.unloaded = []
        entries = list(entries)

        async def forward(entry, platforms):
            self.forwarded.append((entry.entry_id, list(platforms)))

        async def unload(entry, platforms):
            self.unloaded.append((entry.entry_id, list(platforms)))
            return unloads

        self.config = types.SimpleNamespace(path=lambda *p: "/config/" + "/".join(p))
        self.config_entries = types.SimpleNamespace(
            async_entries=lambda domain: list(entries),
            async_forward_entry_setups=forward,
            async_unload_platforms=unload)

    def async_create_background_task(self, coro, name):
        # the walk itself is rt_source's to test: only its start is heard
        coro.close()
        self.tasks.append(name)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _quiet_setup(monkeypatch):
    armed = []

    async def bootstrap(hass):
        return None

    class Coordinator:
        def __init__(self, hass, entry):
            self.entry = entry

    class LocalStopCoordinator(Coordinator):
        pass

    monkeypatch.setattr(integration, "async_bootstrap_datasource_entries", bootstrap)
    monkeypatch.setattr(integration, "async_arm_source_check",
                        lambda hass, entry: armed.append(("arm", entry.entry_id)))
    monkeypatch.setattr(integration, "async_disarm_source_check",
                        lambda hass, entry: armed.append(("disarm", entry.entry_id)))
    monkeypatch.setattr(integration, "GTFSUpdateCoordinator", Coordinator)
    monkeypatch.setattr(integration, "GTFSLocalStopUpdateCoordinator", LocalStopCoordinator)
    return armed, Coordinator, LocalStopCoordinator


def test_a_datasource_entry_runs_no_coordinator(monkeypatch):
    armed, _, _ = _quiet_setup(monkeypatch)
    hass = _Hass()
    entry = _Entry("d1", file="tao", kind="datasource")
    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    assert hass.forwarded == [("d1", list(integration.DATASOURCE_PLATFORMS))]
    assert not hasattr(entry, "runtime_data")
    # every edit is mirrored onto the journeys and re-arms the check
    assert entry.listeners == [integration.async_mirror_rt_to_entries,
                               integration.async_rearm_source_check]
    assert armed == [("arm", "d1")]
    # the check goes with the entry
    for func in entry.on_unload:
        func()
    assert armed == [("arm", "d1"), ("disarm", "d1")]


def test_a_journey_entry_gets_the_coordinator_of_its_kind(monkeypatch):
    armed, coordinator, local_stop = _quiet_setup(monkeypatch)
    hass = _Hass()
    for entry, cls in ((_Entry("j1", file="tao", route="R1: Line 1"), coordinator),
                       (_Entry("l1", file="tao", device_tracker_id="person.me"), local_stop)):
        assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
        assert type(entry.runtime_data) is cls and entry.runtime_data.entry is entry
        assert entry.listeners == [integration.update_listener]
    assert hass.forwarded == [("j1", list(integration.PLATFORMS)),
                              ("l1", list(integration.PLATFORMS))]
    assert armed == []


def test_the_sources_are_walked_once_per_start(monkeypatch):
    _quiet_setup(monkeypatch)
    hass = _Hass()
    for entry in (_Entry("d1", file="tao", kind="datasource"),
                  _Entry("j1", file="tao", route="R1"),
                  _Entry("d2", file="sncf", kind="datasource")):
        asyncio.run(integration.async_setup_entry(hass, entry))
    assert hass.tasks == ["gtfs2 datasource bootstrap"]


def test_unloading_a_journey_closes_its_schedule(monkeypatch):
    closed = []
    monkeypatch.setattr(integration, "close_schedule", closed.append)
    schedule = object()
    entry = _Entry("j1", file="tao", route="R1")
    entry.runtime_data = types.SimpleNamespace(_pygtfs=schedule)
    hass = _Hass()
    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert closed == [schedule]
    assert hass.unloaded == [("j1", list(integration.PLATFORMS))]
    # a coordinator that never opened one closes nothing, harmlessly
    entry.runtime_data = types.SimpleNamespace()
    asyncio.run(integration.async_unload_entry(hass, entry))
    assert closed == [schedule, None]


def test_a_failed_unload_keeps_the_schedule_open(monkeypatch):
    closed = []
    monkeypatch.setattr(integration, "close_schedule", closed.append)
    entry = _Entry("j1", file="tao", route="R1")
    entry.runtime_data = types.SimpleNamespace(_pygtfs=object())
    assert asyncio.run(integration.async_unload_entry(_Hass(unloads=False), entry)) is False
    assert closed == []


def test_unloading_a_datasource_takes_its_platforms_only(monkeypatch):
    monkeypatch.setattr(integration, "close_schedule", lambda schedule: 1 / 0)
    hass = _Hass()
    entry = _Entry("d1", file="tao", kind="datasource")
    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert hass.unloaded == [("d1", list(integration.DATASOURCE_PLATFORMS))]


def _removal_hooks(monkeypatch):
    heard = []

    async def geojson(hass, entry):
        heard.append(("geojson", entry.entry_id))

    async def orphaned(hass, entry):
        heard.append(("orphaned", entry.entry_id))

    monkeypatch.setattr(integration, "_remove_entry_geojson", geojson)
    monkeypatch.setattr(integration, "_notify_orphaned_line", orphaned)
    return heard


def test_removing_a_datasource_entry_deletes_nothing(monkeypatch):
    heard = _removal_hooks(monkeypatch)
    asyncio.run(integration.async_remove_entry(_Hass(), _Entry("d1", file="tao", kind="datasource")))
    assert heard == []


def test_removing_a_journey_clears_its_files_then_names_its_line(monkeypatch):
    heard = _removal_hooks(monkeypatch)
    asyncio.run(integration.async_remove_entry(_Hass(), _Entry("j1", file="tao", route="R1")))
    assert heard == [("geojson", "j1"), ("orphaned", "j1")]


def _orphans(monkeypatch, loaded):
    said, looked = [], []

    async def notify(hass, filename, line):
        said.append((filename, line))

    def routes_in(path):
        looked.append(path)
        return loaded

    monkeypatch.setattr(integration, "async_notify_line_orphaned", notify)
    monkeypatch.setattr(integration, "routes_in", routes_in)
    return said, looked


def test_the_last_sensor_of_a_line_names_it(monkeypatch):
    said, looked = _orphans(monkeypatch, {"R1", "R2"})
    gone = _Entry("j1", file="tao", route="R1: Line 1")
    hass = _Hass([gone, _Entry("j2", file="tao", route="R2: Line 2"),
                  _Entry("d1", file="tao", kind="datasource")])
    asyncio.run(integration._notify_orphaned_line(hass, gone))
    assert said == [("tao", "Line 1")]
    assert looked == [integration.real_path("/config/" + integration.DEFAULT_PATH, "tao")]
    # a bare route_id names the line by its id
    said.clear()
    bare = _Entry("j3", file="tao", route="R2")
    asyncio.run(integration._notify_orphaned_line(_Hass([bare]), bare))
    assert said == [("tao", "R2")]


def test_a_line_still_read_or_already_gone_is_not_named(monkeypatch):
    gone = _Entry("j1", file="tao", route="R1: Line 1")
    for others, loaded in (
            # the return sensor still reads the line
            ([_Entry("j2", file="tao", route="R1: Line 1")], {"R1"}),
            # a local stops sensor keeps the source whole
            ([_Entry("l1", file="tao", device_tracker_id="person.me")], {"R1"}),
            # the timetable no longer has the line, or would not say
            ([], {"R2"}),
            ([], set())):
        said, _ = _orphans(monkeypatch, loaded)
        asyncio.run(integration._notify_orphaned_line(_Hass([gone] + others), gone))
        assert said == [], (others, loaded)


def test_an_entry_on_no_line_names_nothing(monkeypatch):
    said, looked = _orphans(monkeypatch, {"R1"})
    for entry in (_Entry("l1", file="tao", route="R1", device_tracker_id="person.me"),
                  _Entry("j1", file="tao"),
                  _Entry("j2", route="R1")):
        asyncio.run(integration._notify_orphaned_line(_Hass([entry]), entry))
    assert said == [] and looked == []
