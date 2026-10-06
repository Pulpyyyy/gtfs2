"""The sensor and the leg file read a stop update's delay the same way.

A train that arrives five minutes late and makes up time while it stands
at the stop leaves with the departure's delay. The sensor took the larger
of the two delays and announced five minutes, the leg file took the
departure's: on the SNCF feed 34 of 5,876 stop updates read 1 to 20
minutes apart. Both now read the departure when it says anything, the
arrival otherwise.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time

import ha_stub

realtime = ha_stub.load("domain.realtime")
leg_mod = ha_stub.load("data.leg_file")
rt_feed = ha_stub.load("feed.rt_feed")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 27, 11, 30, tzinfo=UTC)
SCHEDULED = NOW + datetime.timedelta(minutes=8)
EXPECTED = int((NOW + datetime.timedelta(minutes=9)).timestamp())
LATE_ARRIVAL = {"time": EXPECTED - 60, "delay": 300}


def _context():
    return types.SimpleNamespace(
        _data={"file": "src", "next_departure": {"trip_id": "T1", "departure_time": SCHEDULED}},
        _rt_group="trip", _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://feed.invalid/rt", _route_id="R1",
        _trip_id="T1", _trip_short_name="", _direction="0", _stop_id="S1", _stop_sequence=3,
        _trip_list=[])


def _update(**parts):
    return {"stop_id": "S1", "stop_sequence": 3, **parts}


def _sensor_delays(update):
    feed = [{"id": "e1", "trip_update": {"trip": {"trip_id": "T1", "route_id": "R1"},
                                         "stop_time_update": [update]}}]
    with freeze_time(NOW):
        found = realtime.get_rt_route_trip_statuses(_context(), feed)
    return found["R1"]["0"]["S1"]["delays"]


def _leg_delay(update):
    trips = {"T1": {"stops": {"S1": {"sequence": 3, "scheduled": SCHEDULED.isoformat()}}}}
    feed = [{"trip_update": {"trip": {"trip_id": "T1"}, "stop_time_update": [update]}}]
    leg_mod._time_leg_trips(trips, {}, feed)
    return trips["T1"]["stops"]["S1"]["delay"]


def test_the_departure_s_delay_wins_over_a_later_arrival():
    update = _update(arrival=LATE_ARRIVAL, departure={"time": EXPECTED, "delay": 60})
    assert _sensor_delays(update) == [60]
    assert _leg_delay(update) == 60


def test_an_arrival_alone_gives_its_delay():
    update = _update(arrival=LATE_ARRIVAL)
    assert _sensor_delays(update) == [300]
    assert _leg_delay(update) == 300


def test_a_departure_with_a_delay_and_no_time_keeps_the_arrival_s_time():
    assert rt_feed.stop_update_clock(
        _update(arrival=LATE_ARRIVAL, departure={"delay": 60})) == (EXPECTED - 60, 60)


def test_a_json_feed_s_text_values_are_read_as_numbers():
    assert rt_feed.stop_update_clock(
        _update(departure={"time": str(EXPECTED), "delay": "60"})) == (EXPECTED, 60)
