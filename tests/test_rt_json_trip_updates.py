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
