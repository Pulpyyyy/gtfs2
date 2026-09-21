"""A realtime host that is down is said once, not once a minute per entry.

Every entry reading a source fetches its realtime feeds each cycle, so an
outage of the host wrote the same error once per entry and per minute
until it came back. The failure is logged when it is new for the url,
at debug while it lasts, and its end is said once.
"""
from __future__ import annotations

import logging
import types

import requests

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

URL = "https://rt.example.org/trip-updates"


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_an_outage_is_said_once_and_its_end_too(monkeypatch, caplog):
    monkeypatch.setattr(gtfs_rt_helper, "gtfs_realtime_pb2",
                        types.SimpleNamespace(FeedMessage=lambda: None))
    gtfs_rt_helper._FAILING.clear()
    answer = {"fail": True}

    def fetch(method, url, **kwargs):
        if answer["fail"]:
            raise requests.ConnectionError("down")
        return types.SimpleNamespace(status_code=200, content=b'{"entity": [1]}', text="")

    monkeypatch.setattr(gtfs_rt_helper, "fetch", fetch)
    caplog.set_level(logging.DEBUG, logger=gtfs_rt_helper.__name__)
    for _ in range(3):
        assert gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "trip_data") is None
    assert len(_errors(caplog)) == 1

    answer["fail"] = False
    assert gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "trip_data") == [1]
    assert any("answers again" in r.getMessage() for r in caplog.records)
    assert gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "trip_data") == [1]
    assert sum("answers again" in r.getMessage() for r in caplog.records) == 1

    # a new outage is news again
    answer["fail"] = True
    gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "trip_data")
    assert len(_errors(caplog)) == 2


def test_each_url_speaks_for_itself(monkeypatch, caplog):
    monkeypatch.setattr(gtfs_rt_helper, "gtfs_realtime_pb2",
                        types.SimpleNamespace(FeedMessage=lambda: None))
    gtfs_rt_helper._FAILING.clear()

    def fetch(method, url, **kwargs):
        return types.SimpleNamespace(status_code=503, content=b"", text="Service Unavailable")

    monkeypatch.setattr(gtfs_rt_helper, "fetch", fetch)
    caplog.set_level(logging.DEBUG, logger=gtfs_rt_helper.__name__)
    for url in (URL, URL, URL + "?other", URL + "?other"):
        gtfs_rt_helper._fetch_gtfs_feed_entities(url, {}, "trip_data")
    assert len(_errors(caplog)) == 2


def test_a_json_alert_feed_says_it_is_back(monkeypatch, caplog):
    # read into protobuf messages, a json alert feed left by its own path:
    # its return was never said, and its next outage only at debug level
    gtfs_rt_helper._FAILING.clear()
    answer = {"fail": True}

    def fetch(method, url, **kwargs):
        if answer["fail"]:
            raise requests.ConnectionError("down")
        return types.SimpleNamespace(status_code=200, text="",
                                     content=b'{"header": {"gtfs_realtime_version": "2.0"}, "entity": []}')

    monkeypatch.setattr(gtfs_rt_helper, "fetch", fetch)
    caplog.set_level(logging.DEBUG, logger=gtfs_rt_helper.__name__)
    assert gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "alerts") is None
    answer["fail"] = False
    assert list(gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "alerts")) == []
    assert any("answers again" in r.getMessage() for r in caplog.records)
    # so the next outage is news again
    answer["fail"] = True
    gtfs_rt_helper._fetch_gtfs_feed_entities(URL, {}, "alerts")
    assert len(_errors(caplog)) == 2
