"""The local stops read the source's trip updates, not a copy of their own.

They downloaded the feed apart, to www/gtfs2/<name>_localstop.rt, and read
it back through a relative file:// path: a second download of the feed
every cycle, the same bytes the source's other sensors had just fetched,
and a cache entry the realtime window never looked at, so a source read
only by local stops never had its window kept open by a vehicle still
under way. They now go through the source's feed cache.
"""
from __future__ import annotations

import sys
import time
import types

from google.transit import gtfs_realtime_pb2

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")
gtfs_rt_helper = sys.modules[gtfs_helper.get_gtfs_feed_entities.__module__]

URL = "http://rt.test/local-trips"
SOURCE = "town"


def _feed(stop_at):
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    entity = message.entity.add()
    entity.id = "1"
    entity.trip_update.trip.trip_id = "T1"
    entity.trip_update.trip.route_id = "R1"
    stop = entity.trip_update.stop_time_update.add()
    stop.stop_id = "S1"
    stop.departure.time = stop_at
    return message.SerializeToString()


def _local_stops():
    return types.SimpleNamespace(
        _realtime=True, _trip_update_url=URL, _headers={},
        _data={"file": SOURCE, "name": "around me"})


def _forget():
    key = (SOURCE, URL, "trip_data")
    for store in (gtfs_rt_helper._FEED_CACHE, gtfs_rt_helper._FEED_FAILED,
                  gtfs_rt_helper._FEED_PUBLISHED):
        store.pop(key, None)


def test_local_stops_share_the_download_of_the_source(monkeypatch):
    _forget()
    stop_at = int(time.time()) + 600
    downloads = []
    monkeypatch.setattr(gtfs_rt_helper, "_feed_body",
                        lambda url, headers, label: downloads.append(url) or _feed(stop_at))
    # a line sensor of the source reads the feed, then the local stops
    journey = gtfs_rt_helper.get_gtfs_feed_entities(URL, None, "trip_data", owner=SOURCE)
    local = gtfs_helper._local_stop_feed(_local_stops())
    assert downloads == [URL]
    assert local == journey
    assert local[0]["trip_update"]["trip"]["trip_id"] == "T1"
    _forget()


def test_the_realtime_window_sees_what_local_stops_read(monkeypatch):
    _forget()
    stop_at = int(time.time()) + 600
    monkeypatch.setattr(gtfs_rt_helper, "_feed_body",
                        lambda url, headers, label: _feed(stop_at))
    gtfs_helper._local_stop_feed(_local_stops())
    # no line named: a source read by local stops alone listens to the whole feed
    assert gtfs_rt_helper.cached_feed_has_future_stop(SOURCE, URL, [], stop_at - 60)
    _forget()


def test_a_feed_that_cannot_be_read_leaves_an_empty_list(monkeypatch):
    _forget()
    monkeypatch.setattr(gtfs_rt_helper, "_feed_body", lambda url, headers, label: None)
    assert gtfs_helper._local_stop_feed(_local_stops()) == []
    _forget()


def test_without_realtime_nothing_is_read(monkeypatch):
    monkeypatch.setattr(gtfs_rt_helper, "_feed_body",
                        lambda *args: (_ for _ in ()).throw(AssertionError("read")))
    context = _local_stops()
    context._realtime = False
    assert gtfs_helper._local_stop_feed(context) is None
