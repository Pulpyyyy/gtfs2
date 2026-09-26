"""get_gtfs opens a built datasource, and builds none.

It fetched the zip again whenever it was missing, database there or not:
every call downloaded the feed, and a host down turned a working
datasource into "no_data_file". Outside a refresh, the database is what
the sensors read, and it is enough. And a datasource with no database, or
one without a feed, is not built from here any more: it answers
"not_built" ("no_zip_file" with no zip), downloads nothing, creates no
file, and leaves the zip as
it is. A refresh of the source builds it, under the source's lock.
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


def test_url_source_answers_from_its_database(tmp_path, monkeypatch):
    hass = _built(tmp_path)
    got = gtfs_helper.get_gtfs(hass, "gtfs2", {"file": "src", "url": "https://h/src.zip",
                                              "extract_from": "url"})
    assert got.feeds
    got.engine.dispose()


def test_zip_source_answers_from_its_database(tmp_path):
    hass = _built(tmp_path)
    got = gtfs_helper.get_gtfs(hass, "gtfs2", {"file": "src", "url": "na", "extract_from": "zip"})
    assert got.feeds
    got.engine.dispose()


def _unbuilt(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    (gtfs_dir / "src.zip").write_bytes(FEED.read_bytes())
    return gtfs_dir, types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))


def test_no_database_is_not_built_from_here(tmp_path):
    gtfs_dir, hass = _unbuilt(tmp_path)
    for data in ({"file": "src", "url": "na", "extract_from": "zip"},
                 {"file": "src", "url": "https://h/src.zip", "extract_from": "url"}):
        assert gtfs_helper.get_gtfs(hass, "gtfs2", data) == "not_built"
    # no empty file left behind, taken for a datasource next time
    assert sorted(p.name for p in gtfs_dir.iterdir()) == ["src.zip"]
    assert (gtfs_dir / "src.zip").read_bytes() == FEED.read_bytes()


def test_a_database_without_a_feed_is_not_built_from_here(tmp_path):
    gtfs_dir, hass = _unbuilt(tmp_path)
    pygtfs.Schedule(str(gtfs_dir / "src.sqlite")).engine.dispose()
    assert gtfs_helper.get_gtfs(hass, "gtfs2", {"file": "src", "url": "na",
                                               "extract_from": "zip"}) == "not_built"
    assert (gtfs_dir / "src.zip").read_bytes() == FEED.read_bytes()
