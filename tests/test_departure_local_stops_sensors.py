"""A local stops entry gets a sensor for every stop served around it.

The sensors were made from the stops with a departure in the window at
setup: an entry added in the evening, after the last bus, or at a stop
only served at some hours, got no sensor for them, and none came later
until the entry was reloaded. "Location based stops never worked"
(vingerha/gtfs2#208). Every stop within the radius that a trip calls at
gets its sensor; one with nothing coming lists nothing until it has.
"""
from __future__ import annotations

import asyncio
import types

import feed_db
import ha_stub

ha_stub.install()

sensor = ha_stub.load("sensor")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    # S1 and S2 called at by the line, D a depot no trip calls at, all close
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\n"
                  "S2,Two,47.0005,1.0005\nD,Depot,47.0002,1.0002\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,10,Ten,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T,06:00:00,06:00:00,S1,1\nT,06:10:00,06:10:00,S2,2\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}


async def _prepared():
    # the data below is what async_prepare leaves
    return None


def _setup(tmp_path):
    """The platform set up for a local stops entry: (sensors added, the
    refreshes handed to a background task, none of them run)."""
    schedule = feed_db.build(tmp_path, FEED)
    tracker = types.SimpleNamespace(attributes={"latitude": 47.0002, "longitude": 1.0002})

    async def job(fn, *args):
        return fn(*args)

    async def refresh():
        raise AssertionError("the platform waited for the departures")

    hass = types.SimpleNamespace(states=types.SimpleNamespace(get=lambda e: tracker),
                                 async_add_executor_job=job)
    # the evening: nothing in the window, at any stop
    coordinator = types.SimpleNamespace(
        data={"extracting": False, "local_stops_next_departures": [], "schedule": schedule,
              "radius": 200, "device_tracker_id": "person.me", "name": "around me",
              "file": "src", "offset": 0},
        async_prepare=_prepared,
        async_refresh=refresh,
        async_add_listener=lambda *a, **k: (lambda: None))
    background = []

    def in_background(hass, coro, name):
        background.append(name)
        coro.close()

    entry = types.SimpleNamespace(data={"device_tracker_id": "person.me", "file": "src"},
                                  runtime_data=coordinator, title="around me",
                                  async_create_background_task=in_background)
    added = []
    try:
        asyncio.run(sensor.async_setup_entry(
            hass, entry, lambda entities, update=False: added.extend(entities)))
    finally:
        schedule.engine.dispose()
    return added, background


def test_every_stop_served_gets_its_sensor_with_nothing_coming(tmp_path):
    added, _background = _setup(tmp_path)
    assert sorted(s._stop["stop_id"] for s in added) == ["S1", "S2"]
    assert all(s._attributes["next_departures_lines"] == {} for s in added)


def test_the_sensors_are_made_before_the_departures_are_read(tmp_path):
    # waiting for the first reading held the platform past Home Assistant's
    # ten seconds at startup: the reading now follows, in the background
    added, background = _setup(tmp_path)
    assert len(added) == 2
    assert background == ["gtfs2 first refresh around me"]
