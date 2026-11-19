"""A local stops entry gets a sensor for every stop served around it.

The sensors were made from the stops with a departure in the window at
setup: an entry added in the evening, after the last bus, or at a stop
only served at some hours, got no sensor for them, and none came later
until the entry was reloaded. "Location based stops never worked"
(vingerha/gtfs2#208). Every stop within the radius that a trip calls at
gets its sensor; one with nothing coming lists nothing until it has.

The sensors then follow the tracker at every reading of the coordinator:
a stop come around gets its sensor, a stop missing from two readings in
a row loses it (the entry's readings_away option), and a reading that
cannot tell the stops around takes nothing away. Where the radius holds
more stops than the entry's max_local_stops, the nearest ones are kept:
the options screen refuses such a radius where the tracker stands then,
but a person walks on into a denser part of town.
"""
from __future__ import annotations

import asyncio
import types

import feed_db
import ha_stub

ha_stub.install()

sensor = ha_stub.load("sensor")
local_stops = ha_stub.load("domain.local_stops")
real_er = sensor.er

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    # S1 and S2 called at by the line, D a depot no trip calls at, all close;
    # F called at too, far from them
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\n"
                  "S2,Two,47.0005,1.0005\nD,Depot,47.0002,1.0002\nF,Far,47.1,1.1\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,10,Ten,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T,06:00:00,06:00:00,S1,1\nT,06:10:00,06:10:00,S2,2\n"
                       "T,06:30:00,06:30:00,F,3\n"),
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


HERE = (47.0002, 1.0002)
FAR = (47.1, 1.1)
NOWHERE = (None, None)


def _setup(tmp_path, registered=(), position=HERE, entry_id="me", readings=(), options=None,
           most=None):
    """The platform set up for a local stops entry: (sensors added, the
    refreshes handed to a background task, none of them run), and the
    registry it left when it held `registered` before. A sensor added is
    registered, as Home Assistant registers it. `readings` are what the
    coordinator hands its listeners afterwards: the position of the
    tracker at a reading, or None for a minute with a departure gone."""
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

    listeners = []

    def heard():
        """The coordinator telling its listeners, and what they left to
        run in the background run."""
        for listener in listeners:
            listener()
        while pending:
            asyncio.run(pending.pop(0))

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
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None))
    background = []

    pending = []

    def in_background(hass, coro, name):
        if name.startswith("gtfs2 local stops"):
            pending.append(coro)
            return
        background.append(name)
        coro.close()

    entry = types.SimpleNamespace(entry_id=entry_id, data={"device_tracker_id": "person.me", "file": "src"},
                                  options={**(options or {}), **({"max_local_stops": most} if most else {})},
                                  runtime_data=coordinator, title="around me",
                                  async_create_background_task=in_background,
                                  async_on_unload=lambda unsubscribe: None)
    coordinator.config_entry = entry
    added = []

    def add(entities, update=False):
        added.extend(entities)
        for entity in entities:
            new = _registered(entry_id, entity._attr_unique_id)
            registry.entries[new.entity_id] = new

    try:
        asyncio.run(sensor.async_setup_entry(hass, entry, add))
        for stamp, where in enumerate(readings, 1):
            if where is None:
                # the same reading, a departure taken out of it
                coordinator.data = {**coordinator.data}
                heard()
                continue
            tracker.attributes = {"latitude": where[0], "longitude": where[1]}
            coordinator.data = {**coordinator.data, "gtfs_updated_at": stamp}
            heard()
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
    assert sorted(registry.entries) == [kept.entity_id, "sensor.me_s2_local_stop_person_me"]


def test_another_persons_sensors_are_left_alone(tmp_path):
    # each person followed has their own entry: one entry's reload never
    # reads the others', at the same stop or far away
    same_stop = _registered("other", "other_S1_local_stop_person.other")
    far = _registered("other", "other_FAR_local_stop_person.other")
    _added, _background, registry = _setup(tmp_path, [same_stop, far])
    assert {same_stop.entity_id, far.entity_id} <= set(registry.entries)


def test_a_tracker_without_a_position_keeps_every_sensor(tmp_path):
    # no stop is found around nowhere: that is not a move away from them
    left = _registered("me", "me_FAR_local_stop_person.me")
    added, _background, registry = _setup(tmp_path, [left], position=NOWHERE)
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
    assert "sensor.home_s1_local_stop_person_me" in registry.entries


def test_a_place_with_no_stop_around_takes_out_the_stops_left_behind(tmp_path):
    # the position is known and the stops were read: there are none here,
    # and the sensors of the stops left behind went unavailable for good
    left = _registered("me", "me_FAR_local_stop_person.me")
    added, _background, registry = _setup(tmp_path, [left], position=(10.0, 10.0))
    assert added == []
    assert registry.entries == {}


def _stops(registry):
    return sorted(e.unique_id.split("_")[1] for e in registry.entries.values())


def test_a_stop_come_around_gets_its_sensor_at_the_next_reading(tmp_path):
    # the sensors were made once, when the entry loaded: a person moving
    # kept the stops of where that was until the entry was reloaded
    added, _background, registry = _setup(tmp_path, readings=[FAR])
    assert [s._stop["stop_id"] for s in added] == ["S1", "S2", "F"]
    # the stops left are missing from one reading only: still there
    assert _stops(registry) == ["F", "S1", "S2"]


def test_a_stop_missing_from_two_readings_in_a_row_loses_its_sensor(tmp_path):
    _added, _background, registry = _setup(tmp_path, readings=[FAR, FAR])
    assert _stops(registry) == ["F"]


def test_a_jump_away_and_back_takes_no_sensor_away(tmp_path):
    # a phone jumps a few hundred metres away and back within minutes: one
    # reading taken there must not take the sensors away until the next
    added, _background, registry = _setup(tmp_path, readings=[FAR, HERE, FAR, HERE])
    assert _stops(registry) == ["F", "S1", "S2"]
    # the sensors are the same ones throughout, never made again: none was
    # missing from two readings in a row, the stop of the jump no more
    assert [s._stop["stop_id"] for s in added] == ["S1", "S2", "F"]
    # back for good, the stop of the jump goes
    _added, _background, registry = _setup(tmp_path, readings=[FAR, HERE, HERE])
    assert _stops(registry) == ["S1", "S2"]


def test_a_stop_met_again_after_its_sensor_went_gets_a_new_one(tmp_path):
    added, _background, registry = _setup(tmp_path, readings=[FAR, FAR, HERE])
    assert [s._stop["stop_id"] for s in added] == ["S1", "S2", "F", "S1", "S2"]
    assert _stops(registry) == ["F", "S1", "S2"]


def test_a_reading_without_a_position_takes_nothing_away_and_counts_for_nothing(tmp_path):
    _added, _background, registry = _setup(tmp_path, readings=[NOWHERE, NOWHERE, NOWHERE])
    assert _stops(registry) == ["S1", "S2"]
    # nor does it count as a reading away: one away, two unknown, still there
    _added, _background, registry = _setup(tmp_path, readings=[FAR, NOWHERE, NOWHERE])
    assert _stops(registry) == ["F", "S1", "S2"]


def test_a_departure_gone_between_two_readings_is_no_reading(tmp_path):
    # the coordinator tells its listeners every minute a departure went:
    # the same reading, which must not count twice against a stop
    _added, _background, registry = _setup(tmp_path, readings=[FAR, None, None])
    assert _stops(registry) == ["F", "S1", "S2"]


def test_the_readings_a_stop_may_miss_are_the_entry_s_option(tmp_path):
    # 1: gone at the first reading without it
    _added, _background, registry = _setup(tmp_path, readings=[FAR], options={"readings_away": 1})
    assert _stops(registry) == ["F"]
    # 3: still there after two
    _added, _background, registry = _setup(tmp_path, readings=[FAR, FAR], options={"readings_away": 3})
    assert _stops(registry) == ["F", "S1", "S2"]


def test_the_nearest_stops_are_kept_where_the_radius_holds_more_than_allowed(tmp_path):
    # S1 lies 0.0002 degree from the tracker, S2 0.0003
    added, _background, _registry = _setup(tmp_path, most=1)
    assert [s._stop["stop_id"] for s in added] == ["S1"]


def test_the_departures_are_read_within_the_stops_kept(tmp_path):
    schedule = feed_db.build(tmp_path, FEED)
    try:
        whole = local_stops._radius_kept(schedule, *HERE, {"radius": 200}, 2)
        cut = local_stops._radius_kept(schedule, *HERE, {"radius": 200}, 1)
    finally:
        schedule.engine.dispose()
    # no more stops than allowed: the entry's radius, 200 m in degrees
    assert whole == 200 / 111111
    # one too many: just past S1, short of S2
    assert 0.0002 < cut < 0.0003
