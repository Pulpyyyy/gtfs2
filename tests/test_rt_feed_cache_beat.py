"""A realtime feed is downloaded once for each time it is published.

The coordinators tick once a minute each, at the second they were set up,
and the feed cache kept a download 30 s: two downloads a minute went
through, one of them the same bytes again (IDFM's gateway publishes its
7.3 MB once a minute, measured 2026-09-27). A feed tells when it was
published (header.timestamp); once two publications show its beat, a
download is kept until the next one is due, plus the lag the feed shows,
and never past FEED_CACHE_MAX_AGE. A feed without a timestamp keeps the
30 s.
"""
from __future__ import annotations

import types

import pytest
from google.transit import gtfs_realtime_pb2

import ha_stub

rt_feed = ha_stub.load("feed.rt_feed")

URL = "http://rt.test/trips"
KEY = ("town", URL, "trip_data")


def _feed(published):
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    if published:
        message.header.timestamp = published
    entity = message.entity.add()
    entity.id = "1"
    entity.trip_update.trip.trip_id = f"T{published}"
    return message.SerializeToString()


class Host:
    """The feed's host and the clock: `published` is what the host serves,
    `now` the time; each download is noted."""

    def __init__(self, monkeypatch) -> None:
        self.now = 1000.0
        self.published = 0
        self.downloads = []
        monkeypatch.setattr(rt_feed, "time", types.SimpleNamespace(
            time=lambda: self.now, sleep=lambda _s: None))
        monkeypatch.setattr(rt_feed, "_feed_body", self._body)
        for store in (rt_feed._FEED_CACHE, rt_feed._FEED_FAILED,
                      rt_feed._FEED_PUBLISHED):
            store.pop(KEY, None)

    def _body(self, url, headers, label):
        self.downloads.append(self.now)
        return _feed(self.published)

    def read(self, at):
        self.now = at
        return rt_feed.get_gtfs_feed_entities(URL, None, "trip_data", owner="town")


@pytest.fixture
def host(monkeypatch):
    return Host(monkeypatch)


def test_a_feed_not_yet_published_again_is_not_downloaded_again(host):
    # published at 1000 and 1060: a beat of 60 s
    host.published = 1000
    host.read(1005)
    host.published = 1060
    host.read(1065)
    # a sensor ticking 35 s later, past the 30 s: the next publication is
    # due at 1120, seen from 1130 at the latest
    host.read(1100)
    host.read(1129)
    assert host.downloads == [1005, 1065]
    host.published = 1120
    got = host.read(1131)
    assert host.downloads == [1005, 1065, 1131]
    assert got[0]["trip_update"]["trip"]["trip_id"] == "T1120"


def test_a_feed_without_a_timestamp_keeps_the_30_s(host):
    host.read(1000)
    host.read(1029)
    host.read(1031)
    assert host.downloads == [1000, 1031]


def test_one_publication_alone_tells_no_beat(host):
    host.published = 1000
    host.read(1005)
    host.read(1036)
    assert host.downloads == [1005, 1036]


def test_never_kept_past_the_longest_age(host):
    # a beat of 5 minutes: kept FEED_CACHE_MAX_AGE at most all the same
    host.published = 1000
    host.read(1000)
    host.published = 1300
    host.read(1300)
    host.read(1300 + rt_feed.FEED_CACHE_MAX_AGE - 1)
    host.read(1300 + rt_feed.FEED_CACHE_MAX_AGE)
    assert host.downloads == [1000, 1300, 1300 + rt_feed.FEED_CACHE_MAX_AGE]


def test_a_reading_that_missed_a_publication_does_not_stretch_the_beat(host):
    host.published = 1000
    host.read(1000)
    host.published = 1060
    host.read(1060)
    # read again 2 minutes on, two publications later: the beat stays 60
    host.published = 1180
    host.read(1180)
    host.read(1239)
    host.read(1251)
    assert host.downloads == [1000, 1060, 1180, 1251]


def _published(monkeypatch, body, label="trip_data"):
    monkeypatch.setattr(rt_feed, "_feed_body", lambda url, headers, label: body)
    return rt_feed._fetch_feed(URL, {}, label)[1]


def test_the_timestamp_is_read_off_the_header(monkeypatch):
    assert _published(monkeypatch, _feed(1790509440)) == 1790509440
    assert _published(monkeypatch, _feed(1790509440), "vehicle_positions") == 1790509440
    assert _published(monkeypatch, _feed(1790509440), "alerts") == 1790509440
    assert _published(monkeypatch, _feed(0)) is None
    assert _published(monkeypatch, b'{"header": {"timestamp": 5}}') is None
    # a body cut short is no feed: no timestamp either
    assert _published(monkeypatch, b"\x0a\xff") is None
