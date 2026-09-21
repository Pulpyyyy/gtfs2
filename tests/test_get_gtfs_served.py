"""A built datasource answers without its zip.

get_gtfs fetched the zip again whenever it was missing, database there
or not: every call downloaded the feed, and a host down turned a working
datasource into "no_data_file". Outside a refresh, the database is what
the sensors read, and it is enough.
"""
from __future__ import annotations

import types
from pathlib import Path

import pygtfs

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "boarding" / "static.zip"


def _built(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    # built straight from the fixture: the source's own zip is gone
    schedule = pygtfs.Schedule(str(gtfs_dir / "src.sqlite"))
    pygtfs.append_feed(schedule, str(FEED))
    schedule.engine.dispose()
    return types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))


def _host_down(*args, **kwargs):
    raise ConnectionError("host down")


def test_url_source_answers_from_its_database(tmp_path, monkeypatch):
    hass = _built(tmp_path)
    monkeypatch.setattr(gtfs_helper, "fetch", _host_down)
    got = gtfs_helper.get_gtfs(hass, "gtfs2", {"file": "src", "url": "https://h/src.zip",
                                              "extract_from": "url"})
    assert got.feeds
    got.engine.dispose()


def test_zip_source_answers_from_its_database(tmp_path):
    hass = _built(tmp_path)
    got = gtfs_helper.get_gtfs(hass, "gtfs2", {"file": "src", "url": "na", "extract_from": "zip"})
    assert got.feeds
    got.engine.dispose()


def test_a_refresh_still_needs_the_feed(tmp_path, monkeypatch):
    hass = _built(tmp_path)
    monkeypatch.setattr(gtfs_helper, "fetch", _host_down)
    got = gtfs_helper.get_gtfs(hass, "gtfs2", {"file": "src", "url": "https://h/src.zip",
                                              "extract_from": "url"}, True)
    assert got == "no_data_file"
