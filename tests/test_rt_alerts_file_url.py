"""An alerts feed is read from any url, as the other realtime feeds are.

update_gtfs_rt_local writes a feed to a local file, alerts included, for
a source to read through a file:// url. The trip updates and the vehicle
positions read any url they are given; the alerts asked for one starting
with "http", and a file:// alerts feed was never read.
"""
from __future__ import annotations

import types

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")


def _alerts(monkeypatch, url):
    read = []
    monkeypatch.setattr(gtfs_rt_helper, "_read_feed",
                        lambda me, feed_url, label: read.append((feed_url, label)) or ["entity"])
    monkeypatch.setattr(gtfs_rt_helper, "journey_alerts",
                        lambda me, entities: {"origin": entities})
    got = gtfs_rt_helper.get_rt_alerts(types.SimpleNamespace(_alerts_url=url))
    return got, read


def test_a_file_alerts_feed_is_read(monkeypatch):
    url = "file:///config/www/gtfs2/tao_alerts.rt"
    got, read = _alerts(monkeypatch, url)
    assert read == [(url, "alerts")]
    assert got == {"origin": ["entity"]}


def test_no_alerts_url_reads_nothing(monkeypatch):
    for url in (None, ""):
        got, read = _alerts(monkeypatch, url)
        assert (got, read) == ({}, [])
