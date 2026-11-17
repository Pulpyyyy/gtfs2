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
real_er = sensor.er

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


class _Registry:
    """The entity registry as the platform reads it: entries held by their
    config entry, removed by entity id."""

    def __init__(self, entries):
        self.entries = {e.entity_id: e for e in entries}

    def async_remove(self, entity_id):
        del self.entries[entity_id]


def _registered(entry_id, unique_id):
    return types.SimpleNamespace(domain="sensor", config_entry_id=entry_id, unique_id=unique_id,
                                 entity_id="sensor." + unique_id.lower().replace(".", "_"))


def _setup(tmp_path, registered=(), position=(47.0002, 1.0002), entry_id="me"):
    """The platform set up for a local stops entry: (sensors added, the
    refreshes handed to a background task, none of them run), and the
    registry it left when it held `registered` before."""
    schedule = feed_db.build(tmp_path, FEED)
    latitude, longitude = position
    tracker = types.SimpleNamespace(attributes={"latitude": latitude, "longitude": longitude})
    registry = _Registry(registered)
    sensor.er = types.SimpleNamespace(
        async_get=lambda hass: registry,
        async_entries_for_config_entry=lambda reg, entry_id: [
            e for e in list(reg.entries.values()) if e.config_entry_id == entry_id])

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

    entry = types.SimpleNamespace(entry_id=entry_id, data={"device_tracker_id": "person.me", "file": "src"},
                                  runtime_data=coordinator, title="around me",
                                  async_create_background_task=in_background)
    coordinator.config_entry = entry
    added = []
    try:
        asyncio.run(sensor.async_setup_entry(
            hass, entry, lambda entities, update=False: added.extend(entities)))
    finally:
        schedule.engine.dispose()
        sensor.er = real_er
    return added, background, registry


def test_every_stop_served_gets_its_sensor_with_nothing_coming(tmp_path):
    added, _background, _registry = _setup(tmp_path)
    assert sorted(s._stop["stop_id"] for s in added) == ["S1", "S2"]
    assert all(s._attributes["next_departures_lines"] == {} for s in added)


def test_the_sensors_are_made_before_the_departures_are_read(tmp_path):
    # waiting for the first reading held the platform past Home Assistant's
    # ten seconds at startup: the reading now follows, in the background
    added, background, _registry = _setup(tmp_path)
    assert len(added) == 2
    assert background == ["gtfs2 first refresh around me"]


def test_a_reload_elsewhere_takes_out_the_stops_left_behind(tmp_path):
    # every reload away from a stop left its sensor in the registry,
    # unavailable: hundreds in a city for one person
    left = _registered("me", "me_FAR_local_stop_person.me")
    kept = _registered("me", "me_S1_local_stop_person.me")
    _added, _background, registry = _setup(tmp_path, [left, kept])
    assert sorted(registry.entries) == [kept.entity_id]


def test_another_persons_sensors_are_left_alone(tmp_path):
    # each person followed has their own entry: one entry's reload never
    # reads the others', at the same stop or far away
    same_stop = _registered("other", "other_S1_local_stop_person.other")
    far = _registered("other", "other_FAR_local_stop_person.other")
    _added, _background, registry = _setup(tmp_path, [same_stop, far])
    assert sorted(registry.entries) == sorted([same_stop.entity_id, far.entity_id])


def test_a_tracker_without_a_position_keeps_every_sensor(tmp_path):
    # no stop is found around nowhere: that is not a move away from them
    left = _registered("me", "me_FAR_local_stop_person.me")
    added, _background, registry = _setup(tmp_path, [left], position=(None, None))
    assert added == []
    assert list(registry.entries) == [left.entity_id]


def test_two_entries_following_one_person_each_get_their_sensor_at_a_stop(tmp_path):
    # the stop and the person alone made the same unique_id for both, two
    # imports of one feed for one: Home Assistant refused the second
    # entry's sensor, and its departures were shown nowhere
    (tmp_path / "home").mkdir()
    (tmp_path / "work").mkdir()
    mine, _background, _registry = _setup(tmp_path / "home", entry_id="home")
    theirs, _background, registry = _setup(
        tmp_path / "work", [_registered("home", "home_S1_local_stop_person.me")], entry_id="work")
    assert [s._attr_unique_id for s in mine] == ["home_S1_local_stop_person.me",
                                                 "home_S2_local_stop_person.me"]
    assert [s._attr_unique_id for s in theirs] == ["work_S1_local_stop_person.me",
                                                   "work_S2_local_stop_person.me"]
    # the other entry's reload leaves the first one's sensor where it is
    assert list(registry.entries) == ["sensor.home_s1_local_stop_person_me"]


def test_a_place_with_no_stop_around_takes_out_the_stops_left_behind(tmp_path):
    # the position is known and the stops were read: there are none here,
    # and the sensors of the stops left behind went unavailable for good
    left = _registered("me", "me_FAR_local_stop_person.me")
    added, _background, registry = _setup(tmp_path, [left], position=(10.0, 10.0))
    assert added == []
    assert registry.entries == {}
