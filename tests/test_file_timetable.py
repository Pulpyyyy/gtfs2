"""The timetable file an entry writes for a card chaining journeys.

timetable_doc turns the rows of the departure query over a window of
service days into the file's content. Read here on rows shaped like the
query's, without a database: the days listed even when empty, a run past
midnight kept on its service day with its real date, the times given in
the line's zone, and what the file says past its window.
"""
from __future__ import annotations

import datetime

import ha_stub

ha_stub.install()
import homeassistant.util.dt as dt_util  # noqa: E402

geojson = ha_stub.load("geojson")
PARIS = dt_util.get_time_zone("Europe/Paris")
GENERATED = datetime.datetime(2026, 9, 19, 4, 0, tzinfo=PARIS)
DAYS = ["2026-09-19", "2026-09-20", "2026-09-21"]


def row(trip, day, dep, arr):
    """A row as the departure query hands it back: the service day, and the
    departure and arrival as naive local datetimes."""
    return {"trip_id": trip, "origin_depart_date": day,
            "origin_depart_dt": dep, "dest_arrival_dt": arr}


def test_every_day_is_listed_even_empty():
    doc = geojson.timetable_doc("Metro 5", [], DAYS, PARIS, generated=GENERATED)
    assert [d["service_date"] for d in doc["days"]] == DAYS
    assert all(d["departures"] == [] for d in doc["days"])
    assert doc["next"] is None and doc["until"] is None


def test_times_carry_their_zone_and_their_real_date():
    rows = [
        row("t2", "2026-09-19", "2026-09-19 15:18:19", "2026-09-19 15:22:50"),
        # past midnight: still the 19th's service, leaving on the 20th
        row("t9", "2026-09-19", "2026-09-20 00:52:00", "2026-09-20 00:58:00"),
        row("t1", "2026-09-19", "2026-09-19 06:00:00", "2026-09-19 06:05:00"),
    ]
    doc = geojson.timetable_doc("Metro 5", rows, DAYS, PARIS, generated=GENERATED)
    first = doc["days"][0]
    assert first["service_date"] == "2026-09-19"
    assert [d["trip_id"] for d in first["departures"]] == ["t1", "t2", "t9"]
    assert first["departures"][2] == {"trip_id": "t9", "dep": "2026-09-20T00:52:00+02:00",
                                      "arr": "2026-09-20T00:58:00+02:00"}
    assert doc["timezone"] == "Europe/Paris"


def test_last_nights_runs_get_a_day_of_their_own():
    rows = [row("n1", "2026-09-18", "2026-09-19 01:30:00", "2026-09-19 01:40:00")]
    doc = geojson.timetable_doc("N01", rows, DAYS, PARIS, generated=GENERATED)
    assert [d["service_date"] for d in doc["days"]] == ["2026-09-18"] + DAYS
    assert doc["days"][0]["departures"][0]["dep"] == "2026-09-19T01:30:00+02:00"


def test_past_the_window():
    doc = geojson.timetable_doc("22", [], DAYS, PARIS, next_departure="2026-11-03T06:12:00+01:00",
                                until="2026-12-12", generated=GENERATED)
    assert doc["next"] == "2026-11-03T06:12:00+01:00"
    assert doc["until"] == "2026-12-12"


def test_a_row_without_a_time_is_left_out():
    rows = [row("t1", "2026-09-19", None, None), row("t2", "", "2026-09-19 06:00:00", None)]
    doc = geojson.timetable_doc("x", rows, DAYS, PARIS, generated=GENERATED)
    assert all(d["departures"] == [] for d in doc["days"])


def test_the_name_is_the_entrys_own():
    assert geojson.timetable_name("Métro 5 → Place d'Italie") == "timetable_metro_5_place_d_italie.json"


# --- the writing runs off the refresh ----------------------------------------

def test_the_refresh_does_not_wait_for_the_timetable(tmp_path, monkeypatch):
    """At startup every entry writes its timetable at once, which held the
    sensor platform past Home Assistant's minute: the refresh hands the
    writing to a background task, one per entry at a time, and the task
    names the file on the sensor once it is written."""
    import asyncio
    import types

    import sys

    coordinator_mod = ha_stub.load("coordinator")
    exports_mod = sys.modules[coordinator_mod.__name__.rsplit(".", 1)[0] + ".exports"]
    written, started, updates = [], [], []

    def write(hass, data, today, zip_path):
        written.append(data["name"])

    monkeypatch.setattr(exports_mod, "write_timetable_file", write)

    async def run():
        loop = asyncio.get_running_loop()

        async def executor(fn, *args):
            return fn(*args)

        def background(coro, name):
            task = loop.create_task(coro)
            started.append(name)
            return task

        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
            async_add_executor_job=executor, async_create_background_task=background)
        me = object.__new__(coordinator_mod.GTFSUpdateCoordinator)
        me.hass = hass
        me._timetable_export = None
        me._timetable_task = None
        me._data = {"schedule": object(), "gtfs_dir": "gtfs2", "file": "feed", "name": "Métro 4"}
        me.async_update_listeners = lambda: updates.append(dict(me._data))
        data = {"name": "Métro 4"}
        await exports_mod.export_timetable(me, data)
        # handed off: nothing written yet, nothing named yet
        assert written == [] and "timetable_file" not in me._data
        # a second refresh while it runs starts no second writing
        await exports_mod.export_timetable(me, data)
        assert len(started) == 1
        await me._timetable_task
        assert written == ["Métro 4"]
        assert me._data["timetable_file"] == "timetable_metro_4.json"
        assert updates and updates[-1]["timetable_file"] == "timetable_metro_4.json"

    asyncio.run(run())
