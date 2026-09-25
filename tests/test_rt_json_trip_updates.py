"""Trip updates served as json: what the feed leaves out is not an error.

The protobuf reader writes every field of a stop update, arrival and
departure included, with zeros for what is missing. A json feed leaves
out what it does not know (no line, no stop_id, only a departure at a
first stop) and writes its int64 times as strings. Each of those raised
a KeyError, and the realtime of the cycle went with it.
"""
from __future__ import annotations

import datetime
import types

from freezegun import freeze_time

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

NOW = datetime.datetime(2026, 9, 22, 8, 0, tzinfo=datetime.timezone.utc)
IN_TEN = int((NOW + datetime.timedelta(minutes=10)).timestamp())


def _context():
    return types.SimpleNamespace(
        _data={"file": "src"}, _rt_group="trip", _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://feed.invalid/rt", _route_delimiter=None,
        _route_id="R1", _trip_id="T1", _trip_short_name="", _direction="0",
        _stop_id="S1", _stop_sequence=3, _trip_list=[])


def _departures(feed):
    with freeze_time(NOW):
        found = gtfs_rt_helper.get_rt_route_trip_statuses(_context(), feed)
    return found.get("R1", {}).get("0", {}).get("S1", {})


def test_a_departure_only_as_strings_with_no_line():
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T1"},
        "stop_time_update": [{"stop_id": "S1", "stop_sequence": 3,
                              "departure": {"time": str(IN_TEN), "delay": "120"}}]}}]
    got = _departures(feed)
    assert got["trips"] == ["T1"]
    assert got["delays"] == [120]
    assert got["departures"][0].timestamp() == IN_TEN


def test_a_stop_named_by_its_sequence_alone():
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T1", "route_id": "R1"},
        "stop_time_update": [{"stop_sequence": 3, "arrival": {"time": IN_TEN}}]}}]
    assert _departures(feed)["trips"] == ["T1"]


def test_an_update_with_no_stops():
    feed = [{"id": "e1", "trip_update": {"trip": {"trip_id": "T1"}}}]
    assert _departures(feed) == {}


def test_the_window_reads_a_stop_time_written_as_text():
    # the polling window asks the cached feed whether a stop is still to
    # come; times as strings raised there, and the window stayed open blind
    entities = [{"trip_update": {"trip": {"route_id": "R1"},
                                 "stop_time_update": [{"departure": {"time": str(IN_TEN)}}]}}]
    gtfs_rt_helper._FEED_CACHE[("owner", "http://feed.invalid/rt", "trip_data")] = (0, entities)
    try:
        now = int(NOW.timestamp())
        assert gtfs_rt_helper.cached_feed_has_future_stop("owner", "http://feed.invalid/rt", ["R1"], now)
        assert not gtfs_rt_helper.cached_feed_has_future_stop(
            "owner", "http://feed.invalid/rt", ["R1"], IN_TEN + 60)
    finally:
        gtfs_rt_helper._FEED_CACHE.pop(("owner", "http://feed.invalid/rt", "trip_data"), None)


def test_a_line_qualified_by_the_feed_is_read_up_to_its_delimiter():
    me = _context()
    me._rt_group, me._route_delimiter = "route", "-"
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T9", "route_id": "R1-2026", "direction_id": "0"},
        "stop_time_update": [{"stop_id": "S1", "departure": {"time": IN_TEN}}]}}]
    with freeze_time(NOW):
        found = gtfs_rt_helper.get_rt_route_trip_statuses(me, feed)
    assert found["R1"]["0"]["S1"]["trips"] == ["T9"]


def test_a_delay_without_a_time_is_laid_on_the_timetable():
    me = _context()
    me._data = {"file": "src", "next_departure": {
        "trip_id": "T1", "departure_time": NOW + datetime.timedelta(minutes=10)}}
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T1"},
        "stop_time_update": [{"stop_id": "S1", "departure": {"delay": 60}}]}}]
    with freeze_time(NOW):
        got = gtfs_rt_helper.get_rt_route_trip_statuses(me, feed)["R1"]["0"]["S1"]
    assert got["departures"][0].timestamp() == IN_TEN + 60 and got["delays"] == [60]


def test_a_departure_gone_by_is_not_listed():
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T1"},
        "stop_time_update": [{"stop_id": "S1", "departure": {"time": IN_TEN - 1200}}]}}]
    got = _departures(feed)
    assert (got["departures"], got["delays"], got["trips"]) == ([], [], [])


def test_no_trip_update_feed_reads_nothing(monkeypatch):
    # the vehicles still land on the map, the board keeps the timetable
    me = _context()
    me._trip_update_url, me._vehicle_position_url = None, "http://feed.invalid/vp"
    read = []
    monkeypatch.setattr(gtfs_rt_helper, "get_rt_vehicle_positions", lambda self: read.append(self))
    me._feed_entities = "stale"
    assert gtfs_rt_helper.get_rt_route_trip_statuses(me) == {}
    assert read == [me] and me._feed_entities is None


def _trip_feed(relationship):
    """A protobuf feed of one trip update whose trip carries that
    schedule_relationship number, written raw: bindings that do not know
    the value could not set it."""
    from google.transit import gtfs_realtime_pb2
    trip = gtfs_realtime_pb2.TripDescriptor(trip_id="T1").SerializeToString()
    trip += bytes([4 << 3, relationship])  # field 4, varint
    update = bytes([1 << 3 | 2, len(trip)]) + trip  # TripUpdate.trip
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    entity = feed.entity.add(id="e1")
    entity.trip_update.ParseFromString(update)
    return feed.SerializeToString()


def test_the_converter_reads_the_trip_relationships_of_today_s_spec():
    # DELETED (7) and NEW (8) came into gtfs-realtime.proto after the
    # bindings 1.0.0 were generated: read through those, a deleted trip
    # came out SCHEDULED and stood on the board as a departure
    for number, name in ((3, "CANCELED"), (7, "DELETED"), (8, "NEW")):
        entity = gtfs_rt_helper.convert_gtfs_realtime_to_json(_trip_feed(number))["entity"][0]
        assert entity["trip_update"]["trip"]["schedule_relationship"] == name


def test_a_json_direction_written_as_a_number_reaches_the_sensor():
    # the sensor reads its departures under its direction as text
    me = _context()
    me._rt_group = "route"
    feed = [{"id": "e1", "trip_update": {
        "trip": {"trip_id": "T9", "route_id": "R1", "direction_id": 0},
        "stop_time_update": [{"stop_id": "S1", "departure": {"time": IN_TEN}}]}}]
    with freeze_time(NOW):
        found = gtfs_rt_helper.get_rt_route_trip_statuses(me, feed)
    assert list(found["R1"]) == ["0"]
    assert found["R1"]["0"]["S1"]["trips"] == ["T9"]
