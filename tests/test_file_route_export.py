"""When the coordinator writes a line's route file, and when it keeps it.

The route file is drawn from the fullest trip and from the shape read out of
the zip. On IDFM shapes.txt is 131 MB to scan for one line, and at every
restart every entry read it again: the coordinator knew nothing of the file
the last run left. A file newer than the zip, drawing the trip picked, is
now kept, and the file is looked at and written off the refresh, the
pick of the trip included: at startup 18 entries picking at once held
their sensors up to 13.5 s each.
Newer than the database too: a file written between the zip's adoption and
the database's rebuild drew metro 6 along metro 9 on IDFM.
"""
from __future__ import annotations

import asyncio
import json
import os
import types

import ha_stub

ha_stub.install()
coordinator_mod = ha_stub.load("coordinator")
import sys  # noqa: E402
exports_mod = sys.modules["gtfs2_under_test.domain.exports"]

ROUTE, DIRECTION, TRIP = "IDFM:C01374", "1", "T4-29"


def _files(tmp_path, trip=TRIP, file_newer=True, db_newer=False):
    """A zip, its database and, unless trip is None, the route file
    drawing trip. db_newer: the database was rebuilt after the file was
    written, the zip was not."""
    zip_path = tmp_path / "gtfs2" / "IDFM.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"zip")
    db_path = tmp_path / "gtfs2" / "IDFM.sqlite"
    db_path.write_bytes(b"db")
    zip_time = os.path.getmtime(zip_path)
    os.utime(db_path, (zip_time,) * 2)
    file = tmp_path / "www" / "gtfs2" / exports_mod.route_geojson_name(ROUTE, DIRECTION)
    if trip is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"type": "FeatureCollection", "properties": {"trip_id": trip}}))
        os.utime(file, (zip_time + (10 if file_newer else -10),) * 2)
    if db_newer:
        os.utime(db_path, (zip_time + 20,) * 2)
    return str(zip_path), str(file), str(db_path)


def test_the_trip_a_route_file_draws(tmp_path):
    zip_path, file, db_path = _files(tmp_path)
    assert exports_mod._drawn_trip(zip_path, file, db_path) == TRIP


def test_a_route_file_older_than_its_zip_draws_nothing_worth_keeping(tmp_path):
    zip_path, file, db_path = _files(tmp_path, file_newer=False)
    assert exports_mod._drawn_trip(zip_path, file, db_path) is None


def test_a_route_file_older_than_its_database_draws_nothing_worth_keeping(tmp_path):
    # the IDFM case of 2026-09-27: the zip adopted at 12:40, the file
    # written then from the old database, the database rebuilt at 13:13.
    # The trip kept its id, the zip did not move, and the file named a
    # shape_id the new edition gives to another line
    zip_path, file, db_path = _files(tmp_path, db_newer=True)
    assert exports_mod._drawn_trip(zip_path, file, db_path) is None


def test_no_route_file_or_an_unreadable_one(tmp_path):
    zip_path, file, db_path = _files(tmp_path, trip=None)
    assert exports_mod._drawn_trip(zip_path, file, db_path) is None
    zip_path, file, db_path = _files(tmp_path)
    with open(file, "w") as handle:
        handle.write("not json")
    assert exports_mod._drawn_trip(zip_path, file, db_path) is None


def _run(tmp_path, monkeypatch):
    """Export the route of an entry the way a refresh does: (written, started)."""
    written, started = [], []
    monkeypatch.setattr(exports_mod, "get_representative_trip", lambda *args: TRIP)
    monkeypatch.setattr(exports_mod, "write_route_file",
                        lambda hass, data, route_id, direction, trip_id: written.append(trip_id))

    async def run():
        loop = asyncio.get_running_loop()

        async def executor(fn, *args):
            return fn(*args)

        def background(coro, name):
            started.append(name)
            return loop.create_task(coro)

        me = object.__new__(coordinator_mod.GTFSUpdateCoordinator)
        me.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_add_executor_job=executor, async_create_background_task=background)
        me._route_export_trip = None
        me._route_task = None
        me._pygtfs_edition = None
        me._representative_pick = me._representative_trip = None
        me._data = {"schedule": object(), "gtfs_dir": "gtfs2", "file": "IDFM",
                    "next_departure": {"route_id": ROUTE, "trip_direction_id": DIRECTION}}
        await exports_mod.export_route_shape(me, {"route": ROUTE, "direction": DIRECTION,
                                                  "origin": "A: a", "destination": "B: b"})
        # the refresh did not wait for the writing
        assert written == []
        if me._route_task is not None:
            await me._route_task
        assert me._data["route_geojson_file"] == exports_mod.route_geojson_name(ROUTE, DIRECTION)

    asyncio.run(run())
    return written, started


def test_a_restart_keeps_the_route_file_of_this_zip(tmp_path, monkeypatch):
    _files(tmp_path)
    written, started = _run(tmp_path, monkeypatch)
    # looked at in the background, and kept
    assert written == [] and len(started) == 1


def test_a_new_zip_or_another_trip_writes_it_in_the_background(tmp_path, monkeypatch):
    _files(tmp_path, file_newer=False)
    written, started = _run(tmp_path, monkeypatch)
    assert written == [TRIP] and len(started) == 1
    _files(tmp_path, trip="an older trip")
    written, started = _run(tmp_path, monkeypatch)
    assert written == [TRIP] and len(started) == 1


def test_a_rebuilt_database_writes_it_again_for_the_same_trip_and_zip(tmp_path, monkeypatch, caplog):
    _files(tmp_path, db_newer=True)
    with caplog.at_level("INFO"):
        written, started = _run(tmp_path, monkeypatch)
    assert written == [TRIP] and len(started) == 1
    assert any("older than the zip or the database" in r.getMessage() for r in caplog.records)


def test_a_database_swapped_under_a_running_entry_writes_it_again(tmp_path, monkeypatch, caplog):
    # the same entry, the same trip, the same zip: only the database
    # edition moved, and the key it wrote under says so
    zip_path, file, db_path = _files(tmp_path)
    written = []
    monkeypatch.setattr(exports_mod, "get_representative_trip", lambda *args: TRIP)
    monkeypatch.setattr(exports_mod, "write_route_file",
                        lambda hass, data, route_id, direction, trip_id: written.append(trip_id))

    async def main():
        async def executor(fn, *args):
            return fn(*args)

        me = object.__new__(coordinator_mod.GTFSUpdateCoordinator)
        me.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_add_executor_job=executor,
            async_create_background_task=lambda coro, name: asyncio.get_running_loop().create_task(coro))
        me._route_export_trip = None
        me._route_task = None
        me._pygtfs_edition = None
        me._representative_pick = me._representative_trip = None
        me._data = {"schedule": object(), "gtfs_dir": "gtfs2", "file": "IDFM",
                    "next_departure": {"route_id": ROUTE, "trip_direction_id": DIRECTION}}

        async def refresh(edition):
            me._pygtfs_edition = edition
            await exports_mod.export_route_shape(me, {"route": ROUTE, "direction": DIRECTION,
                                                      "origin": "A: a", "destination": "B: b"})
            if me._route_task is not None:
                await me._route_task

        # the file on disk is newer than both: kept, under the old edition
        await refresh("1:1:1")
        assert written == []
        await refresh("1:1:1")
        assert written == []
        # the rebuild swaps a new database in, newer than the file
        os.utime(db_path, (os.path.getmtime(file) + 10,) * 2)
        await refresh("2:2:2")
        assert written == [TRIP]
        await refresh("2:2:2")
        assert written == [TRIP]

    with caplog.at_level("INFO"):
        asyncio.run(main())
    # one line, the one that says why
    told = [r.getMessage() for r in caplog.records if "Writing the route file" in r.getMessage()]
    assert len(told) == 1 and told[0].endswith("the database changed")


def test_the_trip_drawn_is_picked_once_per_database(tmp_path, monkeypatch):
    _files(tmp_path)
    picked = []
    monkeypatch.setattr(exports_mod, "get_representative_trip",
                        lambda *args: picked.append(args[1:]) or TRIP)

    async def run(me, edition, origin="A: a"):
        me._pygtfs_edition = edition
        await exports_mod.export_route_shape(me, {"route": ROUTE, "direction": DIRECTION,
                                                  "origin": origin, "destination": "B: b"})
        if me._route_task is not None:
            await me._route_task

    async def main():
        async def executor(fn, *args):
            return fn(*args)

        me = object.__new__(coordinator_mod.GTFSUpdateCoordinator)
        me.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_add_executor_job=executor,
            async_create_background_task=lambda coro, name: asyncio.get_running_loop().create_task(coro))
        me._route_export_trip = None
        me._route_task = None
        me._pygtfs_edition = None
        me._representative_pick = me._representative_trip = None
        me._data = {"schedule": object(), "gtfs_dir": "gtfs2", "file": "IDFM",
                    "next_departure": {"route_id": ROUTE, "trip_direction_id": DIRECTION}}
        await run(me, "1:1:1")
        await run(me, "1:1:1")          # same database, same stops: kept
        await run(me, "2:2:2")          # a refresh swapped a new one in
        await run(me, "2:2:2", "C: c")  # another stop asked
        await run(me, None)             # no edition known: always picked

    asyncio.run(main())
    assert len(picked) == 4


def test_a_slow_pick_does_not_hold_the_refresh(tmp_path, monkeypatch):
    # the pick reads every trip of the line: at startup, with every entry
    # picking at once, it took seconds, and the sensor waited for it
    _files(tmp_path, file_newer=False)
    release = __import__("threading").Event()
    written = []
    monkeypatch.setattr(exports_mod, "get_representative_trip", lambda *args: release.wait(5) and TRIP)
    monkeypatch.setattr(exports_mod, "write_route_file",
                        lambda hass, data, route_id, direction, trip_id: written.append(trip_id))

    async def main():
        loop = asyncio.get_running_loop()

        async def executor(fn, *args):
            return await loop.run_in_executor(None, fn, *args)

        me = object.__new__(coordinator_mod.GTFSUpdateCoordinator)
        me.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_add_executor_job=executor,
            async_create_background_task=lambda coro, name: loop.create_task(coro))
        me._route_export_trip = None
        me._route_task = None
        me._pygtfs_edition = None
        me._representative_pick = me._representative_trip = None
        me._data = {"schedule": object(), "gtfs_dir": "gtfs2", "file": "IDFM",
                    "next_departure": {"route_id": ROUTE, "trip_direction_id": DIRECTION}}
        await asyncio.wait_for(exports_mod.export_route_shape(
            me, {"route": ROUTE, "direction": DIRECTION, "origin": "A: a", "destination": "B: b"}), 1)
        # the refresh is over, the pick still running
        assert me._representative_trip is None and not me._route_task.done()
        release.set()
        await me._route_task
        assert me._representative_trip == TRIP and written == [TRIP]

    asyncio.run(main())
