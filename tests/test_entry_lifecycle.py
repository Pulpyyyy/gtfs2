"""What __init__ does when an entry changes or goes, and the source switch.

Removing a journey entry removes the map files it wrote, unless another
entry still reads them: a bus entry's by its line and direction, a train
entry's by the lines its departures rode, read back from the database.
An options change gives each coordinator back its own pace, the local
stops one included. The realtime switch of a source writes its state in
the entry's options, where every coordinator reads it.
"""
from __future__ import annotations

import asyncio
import datetime
import types

import ha_stub

integration = ha_stub.load("__init__")
switch = ha_stub.load("switch")
coordinator_mod = ha_stub.load("coordinator")


class _Hass:
    def __init__(self, root, entries=()):
        self._root = root
        self.data = {}
        self.config = types.SimpleNamespace(path=lambda *p: str(root.joinpath(*p)))
        self.config_entries = types.SimpleNamespace(async_entries=lambda domain: list(entries))

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _entry(entry_id, **data):
    return types.SimpleNamespace(entry_id=entry_id, data=data, options={})


def _files(root, names):
    folder = root / "www" / "gtfs2"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("{}")
    return folder


def _line_files(route, direction):
    return [integration.vehicle_positions_name(route, direction),
            integration.route_geojson_name(route, direction)]


def test_a_bus_entry_takes_its_files_unless_another_reads_them(tmp_path):
    gone = _entry("e1", name="Home to work", route="R1: Line 1", direction="0")
    other = _entry("e2", name="Work to home", route="R2: Line 2", direction="1")
    same_line = _entry("e3", name="Other stop", route="R2: Line 2", direction="1")
    folder = _files(tmp_path, _line_files("R1", "0") + _line_files("R2", "1")
                    + [integration.timetable_name("Home to work")])
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [gone, other, same_line]), gone))
    left = sorted(p.name for p in folder.iterdir())
    assert left == sorted(_line_files("R2", "1"))
    # the second entry on R2 goes: the first still reads its files
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [other, same_line]), same_line))
    assert sorted(p.name for p in folder.iterdir()) == left


def test_a_train_entry_takes_the_files_of_the_lines_it_rode(tmp_path, monkeypatch):
    train = _entry("t1", name="Paris to Lyon", route="train", file="sncf",
                   origin="Paris", destination="Lyon")
    bus = _entry("b1", name="Bus", route="K5: Car", direction="0", file="sncf")
    monkeypatch.setattr(integration, "train_entry_routes", lambda gtfs_dir, data: ["K4", "K5"])
    folder = _files(tmp_path, _line_files("K4", "0") + _line_files("K4", "1")
                    + _line_files("K5", "0"))
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [train, bus]), train))
    # K4 went with it, K5 stays for the bus entry that reads it
    assert sorted(p.name for p in folder.iterdir()) == sorted(_line_files("K5", "0"))


def test_a_train_entry_leaves_the_files_another_train_entry_may_ride(tmp_path, monkeypatch):
    train = _entry("t1", name="Paris to Lyon", route="train", file="sncf")
    other = _entry("t2", name="Lyon to Paris", route="train", file="sncf")
    monkeypatch.setattr(integration, "train_entry_routes", lambda gtfs_dir, data: ["K4"])
    folder = _files(tmp_path, _line_files("K4", "0"))
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [train, other]), train))
    assert sorted(p.name for p in folder.iterdir()) == sorted(_line_files("K4", "0"))


def test_an_options_change_gives_each_coordinator_its_pace():
    async def refresh():
        return None

    for cls, options, minutes in (
            (coordinator_mod.GTFSUpdateCoordinator, {}, 1),
            (coordinator_mod.GTFSLocalStopUpdateCoordinator, {}, 15),
            (coordinator_mod.GTFSLocalStopUpdateCoordinator, {"local_stop_refresh_interval": 5}, 5)):
        coordinator = object.__new__(cls)
        coordinator.data = {"gtfs_updated_at": "then"}
        coordinator.async_request_refresh = refresh
        entry = types.SimpleNamespace(entry_id="e", data={}, options=options,
                                      runtime_data=coordinator)
        asyncio.run(integration.update_listener(None, entry))
        assert coordinator.update_interval == datetime.timedelta(minutes=minutes), cls
        # the answer the old options gave is read again, now
        assert "gtfs_updated_at" not in coordinator.data


def test_an_options_change_before_any_answer_still_refreshes():
    refreshed = []

    async def refresh():
        refreshed.append(True)

    coordinator = object.__new__(coordinator_mod.GTFSUpdateCoordinator)
    coordinator.data = None
    coordinator.async_request_refresh = refresh
    entry = types.SimpleNamespace(entry_id="e", data={}, options={}, runtime_data=coordinator)
    assert asyncio.run(integration.update_listener(None, entry)) is True
    assert refreshed == [True]


def test_an_entry_on_no_line_takes_only_its_timetable(tmp_path):
    gone = _entry("l1", name="Around me", device_tracker_id="person.me")
    kept = _line_files("R1", "0")
    folder = _files(tmp_path, kept + [integration.timetable_name("Around me")])
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [gone]), gone))
    assert sorted(p.name for p in folder.iterdir()) == sorted(kept)


def test_an_entry_with_no_direction_takes_either_one_and_the_old_names(tmp_path):
    """The files written before the ids were sanitised carry the raw id;
    an id that is not a plain file name never wrote in this directory."""
    gone = _entry("e1", name="Bus", route="R 1: Line 1")
    other = _entry("e2", name="Bus back", route="R 1: Line 1", direction="1")
    folder = _files(tmp_path, _line_files("R 1", "0") + _line_files("R 1", "1")
                    + _line_files("R 1", "None") + ["R 1_0.json", "R 1_0_route.json"])
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [gone, other]), gone))
    # direction 1 stays for the entry that reads it
    assert sorted(p.name for p in folder.iterdir()) == sorted(_line_files("R 1", "1"))
    unsafe = _entry("e3", name="Odd", route="a/b")
    (folder / "a").mkdir()
    (folder / "a" / "b_0.json").write_text("{}")
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [unsafe]), unsafe))
    assert (folder / "a" / "b_0.json").exists()


def test_a_file_that_will_not_go_is_said_and_the_rest_goes(tmp_path, caplog):
    gone = _entry("e1", name="Bus", route="R1", direction="0")
    positions, route_file = _line_files("R1", "0")
    folder = _files(tmp_path, [route_file])
    # a directory under the file's name: os.remove refuses it
    (folder / positions).mkdir()
    asyncio.run(integration._remove_entry_geojson(_Hass(tmp_path, [gone]), gone))
    assert [p.name for p in folder.iterdir()] == [positions]
    assert "Could not remove" in caplog.text


def test_the_realtime_switch_writes_the_entry_options():
    updates = []
    entry = types.SimpleNamespace(entry_id="d", data={"file": "tao", "kind": "datasource"},
                                  options={"static_refresh_mode": "auto"})

    def update_entry(e, options):
        updates.append(options)
        e.options = options

    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_update_entry=update_entry))
    entity = switch.GTFSDatasourceRTSwitch(hass, entry)
    entity.async_write_ha_state = lambda: None
    assert entity.is_on
    asyncio.run(entity.async_turn_off())
    assert not entity.is_on
    asyncio.run(entity.async_turn_on())
    assert updates == [{"static_refresh_mode": "auto", "rt_enabled": False},
                       {"static_refresh_mode": "auto", "rt_enabled": True}]
    added = []
    for e in (_entry("j", file="tao"), entry):
        asyncio.run(switch.async_setup_entry(hass, e, added.extend))
    assert [s._attr_unique_id for s in added] == ["gtfs2_datasource_rt_enabled_tao"]
