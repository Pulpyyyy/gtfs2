"""The delay of a followed trip the board does not list, read off the timetable.

The board lists the departures still to come by the timetable. A metro
late past its own time has left it while the feed still announces it,
and so has a train further than the ten departures listed: their delay
was the feed's alone, which IDFM writes as 0. On the 2026-09-29 capture
of the line 14 at Gare de Lyon, a train due 18:58:36 and announced
19:02:11 showed a delay of 0 at the head of the realtime list. The
timetable's time of those trips at the entity's stop is now read from
the database, and the delay is the gap to it, by the same rule as for
the trips on the board (delay_of).
"""
from __future__ import annotations

import datetime
import types

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, text

import ha_stub

realtime = ha_stub.load("domain.realtime")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 29, 19, 0, tzinfo=UTC)


def _epoch(hh, mm, ss=0, day=29):
    return int(datetime.datetime(2026, 9, day, hh, mm, ss, tzinfo=UTC).timestamp())


@pytest.fixture
def schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stop_times (trip_id text, stop_id text, stop_sequence int, departure_time text)"))
        for trip, clock in (("LATE", "18:58:36"), ("LISTED", "19:05:00"), ("FAR", "20:30:00"),
                            ("NIGHT", "23:58:00"), ("PASTMIDNIGHT", "24:05:00")):
            hh, mm, ss = (int(x) for x in clock.split(":"))
            stamp = datetime.datetime(1970, 1, 1) + datetime.timedelta(hours=hh, minutes=mm, seconds=ss)
            conn.execute(text("insert into stop_times values (:t, 'S1', 3, :d)"),
                         {"t": trip, "d": stamp.strftime("%Y-%m-%d %H:%M:%S.000000")})
    return types.SimpleNamespace(engine=engine)


def _context(schedule):
    # the board lists LISTED alone, on UTC clocks
    departure = {"trip_id": "LISTED", "departure_time": datetime.datetime(2026, 9, 29, 19, 5, tzinfo=UTC)}
    return types.SimpleNamespace(
        _data={"file": "src", "next_departure": departure, "schedule": schedule},
        _rt_group="route", _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://feed.invalid/rt",
        _route_id="R1", _trip_id="LISTED", _trip_short_name="", _direction="0",
        _stop_id="S1", _stop_sequence=3, _trip_list=[])


def _delays(schedule, updates, now=NOW):
    """{trip: delay} the sensor reads, updates being {trip: (time, start_date)}."""
    feed = [{"id": trip, "trip_update": {
        "trip": {"trip_id": trip, "route_id": "R1", "direction_id": 0,
                 **({"start_date": start} if start else {})},
        "stop_time_update": [{"stop_id": "S1", "stop_sequence": 3,
                              "departure": {"time": when, "delay": 0}}]}}
        for trip, (when, start) in updates.items()]
    with freeze_time(now):
        slot = realtime.get_rt_route_trip_statuses(_context(schedule), feed)["R1"]["0"]["S1"]
    return dict(zip(slot["trips"], slot["delays"]))


def test_a_train_late_past_its_own_time_has_its_delay(schedule):
    # due 18:58:36, announced 19:02:11: 215 s late, the case of the capture
    assert _delays(schedule, {"LATE": (_epoch(19, 2, 11), None)}) == {"LATE": 215}


def test_a_train_further_than_the_board_has_its_delay(schedule):
    assert _delays(schedule, {"FAR": (_epoch(20, 29, 30), None)}) == {"FAR": -30}


def test_the_board_s_own_trips_read_as_before(schedule):
    assert _delays(schedule, {"LISTED": (_epoch(19, 6), None)}) == {"LISTED": 60}


def test_the_service_day_the_feed_names_is_taken(schedule):
    # a stop time of 24:05 of yesterday's service is today at 00:05
    now = datetime.datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    got = _delays(schedule, {"PASTMIDNIGHT": (_epoch(0, 7, day=30), "20260929")}, now=now)
    assert got == {"PASTMIDNIGHT": 120}


def test_without_a_service_day_the_nearest_one_is_taken(schedule):
    # due 23:58 on the 29th, announced 00:03 on the 30th: 5 minutes late,
    # not a day early
    now = datetime.datetime(2026, 9, 30, 0, 1, tzinfo=UTC)
    assert _delays(schedule, {"NIGHT": (_epoch(0, 3, day=30), None)}, now=now) == {"NIGHT": 300}


def test_a_trip_the_timetable_does_not_know_keeps_the_feed_s_word(schedule):
    assert _delays(schedule, {"ADDED": (_epoch(19, 3), None)}) == {"ADDED": 0}


def _delay_only(schedule, delays, now=NOW):
    """[(trip, departure epoch, delay)] the sensor reads, the feed giving
    each trip a delay and no time, delays being {trip: seconds}."""
    feed = [{"id": trip, "trip_update": {
        "trip": {"trip_id": trip, "route_id": "R1", "direction_id": 0},
        "stop_time_update": [{"stop_id": "S1", "stop_sequence": 3,
                              "arrival": {"time": 0, "delay": delay},
                              "departure": {"time": 0, "delay": delay}}]}}
        for trip, delay in delays.items()]
    with freeze_time(now):
        found = realtime.get_rt_route_trip_statuses(_context(schedule), feed)
    slot = found.get("R1", {}).get("0", {}).get("S1", {"departures": [], "delays": [], "trips": []})
    return [(trip, int(when.timestamp()), delay)
            for trip, when, delay in zip(slot["trips"], slot["departures"], slot["delays"])]


def test_a_trip_late_past_its_own_time_with_a_delay_alone_is_announced(schedule):
    # a feed that gives delays and no time: the bus due 18:58:36,
    # 6 minutes late, leaves at 19:04:36, before the board's 19:05 one
    got = _delay_only(schedule, {"LATE": 360, "LISTED": 0})
    assert got == [("LATE", _epoch(19, 4, 36), 360), ("LISTED", _epoch(19, 5), 0)]


def test_a_trip_gone_by_with_a_delay_alone_is_dropped(schedule):
    # due 18:58:36 and on time, it left 84 s ago: past, not a departure
    assert _delay_only(schedule, {"LATE": 0}) == []


def test_a_delay_alone_on_a_trip_the_timetable_does_not_know_says_nothing(schedule):
    # no time of its own to lay the delay on: dropped, not given another
    # trip's time
    assert _delay_only(schedule, {"ADDED": 120}) == []


def test_with_the_board_empty_the_timetable_is_read_on_the_network_s_clocks(schedule):
    # a network in New York followed from a Home Assistant on UTC: with no
    # departure on the board to give the zone, LATE's 18:58:36 is New
    # York's, 22:58:36 UTC, not 18:58:36 UTC
    with schedule.engine.begin() as conn:
        conn.execute(text("create table agency (agency_id text, agency_timezone text)"))
        conn.execute(text("create table routes (route_id text, agency_id text)"))
        conn.execute(text("insert into agency values ('A', 'America/New_York')"))
        conn.execute(text("insert into routes values ('R1', 'A')"))
    feed = [{"id": "LATE", "trip_update": {
        "trip": {"trip_id": "LATE", "route_id": "R1", "direction_id": 0},
        "stop_time_update": [{"stop_id": "S1", "stop_sequence": 3, "departure": {"time": 0, "delay": 120}}]}}]
    context = _context(schedule)
    context._data["next_departure"] = {}
    with freeze_time(datetime.datetime(2026, 9, 29, 22, 50, tzinfo=UTC)):
        slot = realtime.get_rt_route_trip_statuses(context, feed)["R1"]["0"]["S1"]
    assert [int(when.timestamp()) for when in slot["departures"]] == [_epoch(23, 0, 36)]
