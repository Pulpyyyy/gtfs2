"""A local stops entry reads its stops again once its tracker has moved away.

The stops were read at the entry's pace only, 15 minutes by default: a
person off a bus waited that long for the stops of where they got off,
unless an automation reloaded the entry as they moved. The coordinator
follows its tracker: more than half the radius from where the stops were
last read, for the entry's away_for (two minutes unless its options say
otherwise), asks for a reading at once. A jump away
and back within that time asks for none: a phone does that, and the
stops over there are not the person's. Nor does a tracker riding, gone
further over that time than the entry's walking pace covers (7 km/h
unless its options say otherwise, 0 to follow no move at all): the stops file past a
bus, none can be boarded, and a reading every two minutes of a ride was
a load for nothing. The wait starts again, until it walks or stands.
"""
from __future__ import annotations

import asyncio
import datetime
import types

import ha_stub

ha_stub.install()

coordinator_mod = ha_stub.load("coordinator")
local_stops = ha_stub.load("domain.local_stops")

# the stops read from here, a radius of 200 m: half of it is 0.0009 degree
HERE = (47.0, 1.0)
NEAR = (47.0005, 1.0005)
AWAY = (47.002, 1.0)
START = datetime.datetime(2026, 11, 19, 8, 0, tzinfo=datetime.timezone.utc)
# 111 m north of AWAY, a walk of two minutes; 555 m, a ride
WALKED = (47.003, 1.0)
RIDDEN = (47.007, 1.0)


class _Rig:
    """A coordinator following its tracker, with Home Assistant's two
    helpers held: what listens to the tracker, and the timers armed."""

    def __init__(self, monkeypatch, read_from=HERE, options=None):
        self.tracker = types.SimpleNamespace(attributes={})
        self.timers = []
        self.delays = []
        self.cancelled = []
        self.refreshes = 0
        self.unlistened = False
        hass = types.SimpleNamespace(states=types.SimpleNamespace(get=lambda entity: self.tracker))
        def in_background(hass, coro, name):
            asyncio.run(coro)

        entry = types.SimpleNamespace(entry_id="e1", data={"device_tracker_id": "person.me"},
                                      title="around me",
                                      async_create_background_task=in_background,
                                      options=options or {})
        self.coordinator = coordinator_mod.GTFSLocalStopUpdateCoordinator(hass, entry)
        self.coordinator._data = {"device_tracker_id": "person.me", "radius": 200}
        self.coordinator._read_from = read_from

        async def request_refresh():
            self.refreshes += 1

        self.coordinator.async_request_refresh = request_refresh

        def track(hass, entities, heard):
            assert entities == ["person.me"]
            self.heard = heard
            return lambda: setattr(self, "unlistened", True)

        def later(hass, delay, fire):
            self.delays.append(delay)
            self.timers.append(fire)
            return lambda: self.cancelled.append(fire)

        monkeypatch.setattr(coordinator_mod, "async_track_state_change_event", track)
        monkeypatch.setattr(coordinator_mod, "async_call_later", later)
        self.unfollow = self.coordinator.async_follow_tracker()

    def at(self, position, heard=True, minute=None, accuracy=None):
        """The tracker seen at a position; or standing there unseen yet,
        as when a timer fires between two of its updates. `minute` is
        when it gave the position, for the pace between two of them."""
        self.tracker.attributes = {"latitude": position[0], "longitude": position[1]}
        if accuracy is not None:
            self.tracker.attributes["gps_accuracy"] = accuracy
        if minute is not None:
            self.tracker.last_updated = START + datetime.timedelta(minutes=minute)
        if heard:
            self.heard(None)

    def time_is_up(self):
        """The last timer armed fires, unless cancelled since."""
        fire = self.timers[-1]
        if fire not in self.cancelled:
            asyncio.run(fire(None))


def test_a_tracker_away_for_long_enough_asks_for_a_reading(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    assert rig.refreshes == 0
    rig.time_is_up()
    assert rig.refreshes == 1
    # and the reading asked for is not put off to the entry's pace
    assert rig.coordinator._read_lately({"gtfs_updated_at": "2026-11-19T10:00:00+00:00"}, {}) is False


def test_moving_on_while_away_arms_no_second_timer(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    rig.at((47.003, 1.0))
    assert len(rig.timers) == 1


def test_a_jump_away_and_back_asks_for_nothing(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    rig.at(HERE)
    rig.time_is_up()
    assert rig.refreshes == 0 and len(rig.cancelled) == 1
    # away again later: a new wait, from its start
    rig.at(AWAY)
    assert len(rig.timers) == 2


def test_a_tracker_within_half_the_radius_asks_for_nothing(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(NEAR)
    assert rig.timers == []


def test_stops_never_read_yet_ask_for_nothing(monkeypatch):
    # no position to be away from: the first reading comes at setup
    rig = _Rig(monkeypatch, read_from=None)
    rig.at(AWAY)
    assert rig.timers == [] and rig.refreshes == 0


def test_the_first_position_after_a_reading_without_one_asks_for_a_reading(monkeypatch):
    # a phone not heard of yet when Home Assistant started: the reading
    # found no position, and the stops came a quarter of an hour later
    rig = _Rig(monkeypatch, read_from=None)
    rig.coordinator.data = {"gtfs_updated_at": "2026-11-19T08:00:00+00:00"}
    rig.at((None, None))
    assert rig.refreshes == 0
    rig.at(AWAY)
    assert rig.refreshes == 1 and rig.timers == []
    assert rig.coordinator._read_lately(rig.coordinator.data, {}) is False


def test_a_position_too_vague_is_not_followed(monkeypatch):
    # off by more than half the radius of 200 m: the jumps of a phone
    rig = _Rig(monkeypatch)
    rig.at(AWAY, accuracy=150)
    assert rig.timers == []
    # and it ends no wait either: it says nothing of where the person is
    rig.at(AWAY, accuracy=20)
    rig.at(HERE, accuracy=150)
    assert len(rig.timers) == 1 and rig.cancelled == []


def test_a_tracker_losing_its_position_asks_for_nothing(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at((None, None))
    assert rig.timers == []


def test_unloading_the_entry_stops_the_following(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    rig.unfollow()
    assert rig.unlistened and len(rig.cancelled) == 1


def test_a_reading_records_where_it_was_taken_from(monkeypatch):
    rig = _Rig(monkeypatch, read_from=None)
    rig.tracker.attributes = {"latitude": AWAY[0], "longitude": AWAY[1]}
    me = rig.coordinator
    me._data.update({"gtfs_dir": "gtfs2", "file": "src", "offset": 0, "schedule": object()})
    monkeypatch.setattr(local_stops, "check_extracting", lambda *args: False)
    monkeypatch.setattr(local_stops, "_feed_now", lambda *args: "2026-11-19T10:00:00")
    monkeypatch.setattr(local_stops, "_fetch_local_stop_rows", lambda *args: [])
    monkeypatch.setattr(local_stops, "_radius_kept", lambda *args: 0.002)
    monkeypatch.setattr(local_stops, "_interpret_local_stop_rows", lambda me, rows: rows)
    assert local_stops.get_local_stops_next_departures(me) == []
    assert me._read_from == AWAY


def test_a_tracker_walking_on_while_away_still_gets_its_reading(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    rig.at(WALKED)
    rig.time_is_up()
    assert rig.refreshes == 1


def test_a_tracker_riding_gets_no_reading_until_it_walks_or_stands(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    rig.at(RIDDEN)
    rig.time_is_up()
    # in a bus: no reading, the wait starts again from where it is
    assert rig.refreshes == 0 and len(rig.timers) == 2
    rig.at((47.012, 1.0))
    rig.time_is_up()
    assert rig.refreshes == 0 and len(rig.timers) == 3
    # off the bus, standing: the reading comes
    rig.time_is_up()
    assert rig.refreshes == 1


def test_the_pace_is_told_east_to_west_too(monkeypatch):
    # a degree of longitude is shorter than one of latitude: 0.007 of it
    # at 47 north is 530 m, a ride all the same
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    rig.at((47.002, 1.007))
    rig.time_is_up()
    assert rig.refreshes == 0
    assert round(local_stops.tracker_metres_from(
        rig.coordinator.hass, rig.coordinator._data, AWAY)) == 530


def test_the_pace_is_the_entry_s_option(monkeypatch):
    # 20 km/h: a bicycle is followed too, 555 m in two minutes is under it
    rig = _Rig(monkeypatch, options={"follow_up_to": 20})
    rig.at(AWAY)
    rig.at(RIDDEN)
    rig.time_is_up()
    assert rig.refreshes == 1


def test_a_pace_of_0_follows_no_move(monkeypatch):
    rig = _Rig(monkeypatch, options={"follow_up_to": 0})
    rig.at(AWAY)
    assert rig.timers == []


def test_the_wait_is_the_entry_s_option_two_minutes_without(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.at(AWAY)
    assert rig.delays == [120]
    rig = _Rig(monkeypatch, options={"away_for": 5})
    rig.at(AWAY)
    assert rig.delays == [300]
    # and the pace is told over that wait: 555 m in five minutes is a walk
    rig.at(RIDDEN)
    rig.time_is_up()
    assert rig.refreshes == 1


def test_a_phone_giving_few_positions_is_riding_by_the_pace_between_two(monkeypatch):
    # one position every five minutes: it stands where it was through the
    # whole wait, and the ride read as standing. 2.2 km in five minutes
    # is 27 km/h
    rig = _Rig(monkeypatch)
    rig.at(HERE, minute=0)
    rig.at((47.02, 1.0), minute=5)
    rig.time_is_up()
    assert rig.refreshes == 0 and len(rig.timers) == 2


def test_off_the_bus_the_reading_comes_after_one_more_wait(monkeypatch):
    # standing at the stop the phone gives nothing more: the ride it came
    # by holds the wait under way, not the next
    rig = _Rig(monkeypatch)
    rig.at(HERE, minute=0)
    rig.at((47.02, 1.0), minute=5)
    rig.time_is_up()
    rig.time_is_up()
    assert rig.refreshes == 1


def test_a_walk_between_two_positions_gets_its_reading(monkeypatch):
    # 222 m in five minutes
    rig = _Rig(monkeypatch)
    rig.at(HERE, minute=0)
    rig.at(AWAY, minute=5)
    rig.time_is_up()
    assert rig.refreshes == 1


def test_two_positions_seconds_apart_tell_no_pace(monkeypatch):
    # 20 m off within ten seconds is no 7 km/h: the earlier position is
    # kept, and the walk from it told once there is time between them
    rig = _Rig(monkeypatch)
    rig.at(HERE, minute=0)
    rig.at(AWAY, minute=0.1)
    assert rig.coordinator._rode is False
    rig.at(AWAY, minute=5)
    rig.time_is_up()
    assert rig.refreshes == 1


def test_a_position_a_few_metres_off_is_no_move(monkeypatch):
    # 22 m in a minute and a half is 0.9 km/h, and no move at all: the
    # earlier position stays the one the next is compared with
    rig = _Rig(monkeypatch)
    rig.at(HERE, minute=0)
    rig.at((47.0002, 1.0), minute=1.5)
    assert rig.coordinator._fix[:2] == HERE
