"""What the departures service says past its two days.

With nothing today and tomorrow, {"today": [], "tomorrow": []} said the
same for a line that resumes on Friday, a line suspended and a feed that
ran out. As the timetable file does, the answer carries next, the first
departure after the two days, and until, the last service day the feed
publishes.
"""
from __future__ import annotations

import csv
import datetime
import io
import types
import zipfile
from unittest.mock import patch

import pygtfs
from freezegun import freeze_time

import ha_stub

ha_stub.install()
import homeassistant.util.dt as dt_util  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")

ZONE = "Europe/Paris"
TABLES = {
    "agency.txt": [["agency_id", "agency_name", "agency_url", "agency_timezone"],
                   ["A", "Agency", "https://example.org", ZONE]],
    "routes.txt": [["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
                   ["R1", "A", "1", "One", "3"]],
    "stops.txt": [["stop_id", "stop_name", "stop_lat", "stop_lon"],
                  ["SA", "Alpha", "45.0", "5.0"], ["SB", "Bravo", "45.1", "5.1"]],
    # Monday 21, Tuesday 22 and Friday 25 September 2026 only
    "calendar_dates.txt": [["service_id", "date", "exception_type"],
                           ["MON", "20260921", "1"], ["TUE", "20260922", "1"],
                           ["FRI", "20260925", "1"]],
    "trips.txt": [["route_id", "service_id", "trip_id", "direction_id"],
                  ["R1", "MON", "T_MON", "0"], ["R1", "TUE", "T_TUE_LATE", "0"],
                  ["R1", "FRI", "T_FRI", "0"]],
    "stop_times.txt": [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                       ["T_MON", "12:00:00", "12:00:00", "SA", "1"],
                       ["T_MON", "12:10:00", "12:10:00", "SB", "2"],
                       # Tuesday's service, leaving Wednesday at 00:30
                       ["T_TUE_LATE", "24:30:00", "24:30:00", "SA", "1"],
                       ["T_TUE_LATE", "24:40:00", "24:40:00", "SB", "2"],
                       ["T_FRI", "08:00:00", "08:00:00", "SA", "1"],
                       ["T_FRI", "08:10:00", "08:10:00", "SB", "2"]],
}


def _feed(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    with zipfile.ZipFile(gtfs_dir / "fixture.zip", "w") as zout:
        for name, rows in TABLES.items():
            out = io.StringIO()
            csv.writer(out, lineterminator="\n").writerows(rows)
            zout.writestr(name, out.getvalue())
    schedule = pygtfs.Schedule(str(gtfs_dir / "fixture.sqlite"))
    pygtfs.append_feed(schedule, str(gtfs_dir / "fixture.zip"))
    return schedule


def _call(tmp_path, schedule, at):
    entry = types.SimpleNamespace(options={}, data={
        "file": "fixture", "name": "one", "route_type": "3",
        "origin": "SA: Alpha", "destination": "SB: Bravo", "route": "R1: One"})

    async def job(fn, *args):
        return fn(*args)

    hass = types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p)), time_zone=ZONE),
        config_entries=types.SimpleNamespace(async_get_entry=lambda _id: entry),
        async_add_executor_job=job)
    dt_util.set_default_time_zone(dt_util.get_time_zone(ZONE))
    with freeze_time(at), patch.object(gtfs_helper, "get_gtfs", return_value=schedule), \
            patch.object(schedule.engine, "dispose", lambda: None):
        coro = gtfs_helper.get_route_departures(hass, {"config_entry": "e"})
        try:
            coro.send(None)
        except StopIteration as done:
            return done.value
    raise RuntimeError("the service awaited something the test does not stand in for")


def _utc(local):
    return datetime.datetime.fromisoformat(local).replace(
        tzinfo=dt_util.get_time_zone(ZONE)).astimezone(datetime.timezone.utc).isoformat()


def test_next_and_until(tmp_path):
    schedule = _feed(tmp_path)
    # Monday morning: today's run, nothing listed tomorrow as a calendar
    # day but Tuesday's service leaves Wednesday 00:30, which is next
    got = _call(tmp_path, schedule, "2026-09-21T07:00:00+02:00")
    assert got["today"] == [_utc("2026-09-21T12:00:00")]
    assert got["tomorrow"] == []
    assert got["next"] == _utc("2026-09-23T00:30:00")
    assert got["until"] == "2026-09-25"
    # Wednesday: nothing today or tomorrow, the line resumes on Friday
    got = _call(tmp_path, schedule, "2026-09-23T10:00:00+02:00")
    assert (got["today"], got["tomorrow"]) == ([], [])
    assert got["next"] == _utc("2026-09-25T08:00:00")
    # past the feed: nothing is known after its last day
    got = _call(tmp_path, schedule, "2026-09-27T10:00:00+02:00")
    assert (got["today"], got["tomorrow"], got["next"]) == ([], [], None)
    assert got["until"] == "2026-09-25"
    schedule.engine.dispose()


def test_an_unknown_entry_says_nothing_is_known(tmp_path):
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(async_get_entry=lambda _id: None))
    coro = gtfs_helper.get_route_departures(hass, {"config_entry": "gone"})
    try:
        coro.send(None)
    except StopIteration as done:
        assert done.value == {"today": [], "tomorrow": [], "next": None, "until": None}
