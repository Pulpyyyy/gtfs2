"""A feed that gives a delay and no time: the timetable's time plus the delay.

A feed may update every call of a trip by its stop_sequence, with a
delay and no time. A trip running late was read as
the timetable plus its delay; one running on time, delay 0, was read as
no update at all, its time 0 taken for 1970 and the departure dropped:
the sensors of the on-time lines showed no realtime. A delay of 0 is a
departure on time. A feed that gives neither a time nor a delay says
nothing, and the converter now leaves a delay the feed does not give
as None, where it wrote 0 and the two read the same.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time
from google.transit import gtfs_realtime_pb2

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")
rt_feed = ha_stub.load("rt_feed")

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 3, 19, 20, tzinfo=UTC)
SCHEDULED = NOW + datetime.timedelta(minutes=8)


def _context():
    departure = {"trip_id": "T1", "departure_time": SCHEDULED}
    return types.SimpleNamespace(
        _data={"file": "src", "next_departure": departure}, _rt_group="trip", _headers={},
        _vehicle_position_url=None, _trip_update_url="http://feed.invalid/rt",
        _route_id="R1", _trip_id="T1", _trip_short_name="",
        _direction="0", _stop_id="S1", _stop_sequence=3, _trip_list=[])


def _read(arrival, departure=None):
    # no stop_id, the call named by its sequence
    update = {"stop_id": "", "stop_sequence": 3, "arrival": arrival}
    if departure is not None:
        update["departure"] = departure
    feed = [{"id": "e1", "trip_update": {"trip": {"trip_id": "T1", "route_id": "R1"},
                                          "stop_time_update": [update]}}]
    with freeze_time(NOW):
        found = gtfs_rt_helper.get_rt_route_trip_statuses(_context(), feed)
    return found.get("R1", {}).get("0", {}).get("S1", {})


def test_a_delay_and_no_time_is_the_timetable_plus_the_delay():
    slot = _read({"delay": 120, "time": 0})
    assert slot["departures"] == [SCHEDULED + datetime.timedelta(minutes=2)]
    assert slot["delays"] == [120]


def test_a_zero_delay_and_no_time_is_on_time():
    slot = _read({"delay": 0, "time": 0}, {"delay": None, "time": 0})
    assert slot["departures"] == [SCHEDULED]
    assert slot["delays"] == [0]


def test_neither_a_delay_nor_a_time_says_nothing():
    assert not _read({"delay": None, "time": 0}, {"delay": None, "time": 0}).get("departures")


def test_the_departure_s_delay_comes_first_even_at_zero():
    # a vehicle that arrives late and leaves on time
    when, delay = rt_feed.stop_update_clock(
        {"arrival": {"delay": 90, "time": 0}, "departure": {"delay": 0, "time": 0}})
    assert (when, delay) == (0, 0)


def test_the_converter_keeps_a_delay_the_feed_does_not_give_apart():
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"
    on_time = entity.trip_update.stop_time_update.add()
    on_time.stop_sequence = 3
    on_time.arrival.delay = 0
    silent = entity.trip_update.stop_time_update.add()
    silent.stop_sequence = 4
    converted = rt_feed.convert_gtfs_realtime_to_json(feed.SerializeToString())
    on_time_dict, silent_dict = converted["entity"][0]["trip_update"]["stop_time_update"]
    assert on_time_dict["arrival"]["delay"] == 0
    assert on_time_dict["departure"]["delay"] is None
    assert silent_dict["arrival"]["delay"] is None


def test_the_converter_keeps_a_sequence_the_feed_does_not_give_apart():
    # a feed naming its calls by stop_id alone: protobuf reads their
    # sequence as 0, which a feed numbering from 0 gives its first call
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add()
    entity.id = "e1"
    entity.trip_update.trip.trip_id = "T1"
    first = entity.trip_update.stop_time_update.add()
    first.stop_sequence = 0
    first.stop_id = "S0"
    by_id = entity.trip_update.stop_time_update.add()
    by_id.stop_id = "S1"
    converted = rt_feed.convert_gtfs_realtime_to_json(feed.SerializeToString())
    first_dict, by_id_dict = converted["entity"][0]["trip_update"]["stop_time_update"]
    assert first_dict["stop_sequence"] == 0
    assert by_id_dict["stop_sequence"] is None


def test_a_local_stop_lays_the_delay_on_its_own_row():
    # a local stops departure is no listed departure of a journey: its
    # timetable time is the row it is built from, the only one it knows
    local_stops = ha_stub.load("local_stops")
    paris = datetime.timezone(datetime.timedelta(hours=2))
    row = {"trip_id": "T1", "direction_id": 0, "trip_short_name": None,
           "route_id": "R1", "stop_id": "S1", "stop_sequence": 3,
           "stop_name": "Centre", "route_short_name": "20",
           "route_long_name": "Centre - Gare", "trip_headsign": "Gare"}
    feed = [{"id": "e1", "trip_update": {"trip": {"trip_id": "T1"}, "stop_time_update": [
        {"stop_id": "", "stop_sequence": 3, "arrival": {"delay": 120, "time": 0},
         "departure": {"delay": None, "time": 0}}]}}]
    me = types.SimpleNamespace(_realtime=True, _icon="mdi:bus", _rt_group="trip",
                               _data={"file": "src"}, _headers={},
                               _vehicle_position_url=None, _trip_update_url="http://feed.invalid/rt")
    with freeze_time(datetime.datetime(2026, 10, 3, 6, 0, tzinfo=UTC)):
        element = local_stops._build_local_stop_element(
            me, row, "2026-10-03 08:10:00", paris, paris,
            datetime.datetime(2026, 10, 3, 8, 0, tzinfo=paris), feed_entities=feed)
    assert element["departure_realtime"] == "08:12"
    assert element["delay_realtime"] == 120
