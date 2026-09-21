"""When the coordinator writes a line's route file, and when it keeps it.

The route file is drawn from the fullest trip and from the shape read out of
the zip. On IDFM shapes.txt is 131 MB to scan for one line, and at every
restart every entry read it again: the coordinator knew nothing of the file
the last run left. A file newer than the zip, drawing the trip picked, is
now kept, and a file that has to be written is written off the refresh.
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
exports_mod = sys.modules["gtfs2_under_test.exports"]

ROUTE, DIRECTION, TRIP = "IDFM:C01374", "1", "T4-29"


def _files(tmp_path, trip=TRIP, file_newer=True):
    """A zip and, unless trip is None, the route file drawing trip."""
    zip_path = tmp_path / "gtfs2" / "IDFM.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.write_bytes(b"zip")
    file = tmp_path / "www" / "gtfs2" / exports_mod.route_geojson_name(ROUTE, DIRECTION)
    if trip is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"type": "FeatureCollection", "properties": {"trip_id": trip}}))
        zip_time = os.path.getmtime(zip_path)
        os.utime(file, (zip_time + (10 if file_newer else -10),) * 2)
    return str(zip_path), str(file)


def test_the_trip_a_route_file_draws(tmp_path):
    zip_path, file = _files(tmp_path)
    assert exports_mod._drawn_trip(zip_path, file) == TRIP


def test_a_route_file_older_than_its_zip_draws_nothing_worth_keeping(tmp_path):
    zip_path, file = _files(tmp_path, file_newer=False)
    assert exports_mod._drawn_trip(zip_path, file) is None


def test_no_route_file_or_an_unreadable_one(tmp_path):
    zip_path, file = _files(tmp_path, trip=None)
    assert exports_mod._drawn_trip(zip_path, file) is None
    zip_path, file = _files(tmp_path)
    with open(file, "w") as handle:
        handle.write("not json")
    assert exports_mod._drawn_trip(zip_path, file) is None


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
    assert _run(tmp_path, monkeypatch) == ([], [])


def test_a_new_zip_or_another_trip_writes_it_in_the_background(tmp_path, monkeypatch):
    _files(tmp_path, file_newer=False)
    written, started = _run(tmp_path, monkeypatch)
    assert written == [TRIP] and len(started) == 1
    _files(tmp_path, trip="an older trip")
    written, started = _run(tmp_path, monkeypatch)
    assert written == [TRIP] and len(started) == 1


def test_the_trip_drawn_is_picked_once_per_database(tmp_path, monkeypatch):
    _files(tmp_path)
    picked = []
    monkeypatch.setattr(exports_mod, "get_representative_trip",
                        lambda *args: picked.append(args[1:]) or TRIP)

    async def run(me, edition, origin="A: a"):
        me._pygtfs_edition = edition
        await exports_mod.export_route_shape(me, {"route": ROUTE, "direction": DIRECTION,
                                                  "origin": origin, "destination": "B: b"})

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
        me._data = {"schedule": object(), "gtfs_dir": "gtfs2", "file": "IDFM",
                    "next_departure": {"route_id": ROUTE, "trip_direction_id": DIRECTION}}
        await run(me, "1:1:1")
        await run(me, "1:1:1")          # same database, same stops: kept
        await run(me, "2:2:2")          # a refresh swapped a new one in
        await run(me, "2:2:2", "C: c")  # another stop asked
        await run(me, None)             # no edition known: always picked

    asyncio.run(main())
    assert len(picked) == 4
