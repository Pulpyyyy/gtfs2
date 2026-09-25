"""A source read whole still keeps the lines its route sensors follow.

A train or local stops entry makes its source refresh whole, every line
of the new edition. A route sensor on the same source follows one line:
when the new edition drops it, the swap must be refused and the line
named, as the route by route refresh does, not leave that sensor empty.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import ha_stub

source_zip = ha_stub.load("source_zip")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "boarding" / "static.zip"


def _source(tmp_path):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    shutil.copy(FEED, gtfs_dir / "src.zip")
    # the database in place, recognisable; closed, or the swap waits for it
    conn = sqlite3.connect(gtfs_dir / "src.sqlite")
    conn.execute("create table marker (x)")
    conn.commit()
    conn.close()
    return gtfs_dir


def _tables(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()


def test_a_followed_line_gone_refuses_the_swap(tmp_path):
    gtfs_dir = _source(tmp_path)
    data = {"file": "src", "read_routes": ["B1", "GONE"]}
    got = source_zip._refresh_whole_feed(str(gtfs_dir), "src", "src.zip",
                                         str(gtfs_dir / "src.zip"), data)
    assert got is False
    assert data["lines_missing"] == ["GONE"]
    assert "marker" in _tables(gtfs_dir / "src.sqlite")


def test_every_followed_line_there_swaps(tmp_path):
    gtfs_dir = _source(tmp_path)
    data = {"file": "src", "read_routes": ["B1"]}
    got = source_zip._refresh_whole_feed(str(gtfs_dir), "src", "src.zip",
                                         str(gtfs_dir / "src.zip"), data)
    assert got == {"B1": None}
    assert "lines_missing" not in data
    assert "marker" not in _tables(gtfs_dir / "src.sqlite")


def _refresh(gtfs_dir, monkeypatch):
    """refresh_datasource on a zip source, a fork refused: the legacy
    extract forked and rebuilt the database in place."""
    import types
    gtfs_helper = ha_stub.load("gtfs_helper")

    def no_fork():
        raise AssertionError("the legacy extract ran")
    monkeypatch.setattr(gtfs_helper.os, "fork", no_fork, raising=False)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(gtfs_dir)))
    return source_zip.refresh_datasource(hass, "gtfs2", {"file": "src", "extract_from": "zip"})


def _trips(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("select count(*) from trips").fetchone()[0]
    finally:
        conn.close()


def test_a_source_with_no_database_is_built_whole(tmp_path, monkeypatch):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    shutil.copy(FEED, gtfs_dir / "src.zip")
    sent = (gtfs_dir / "src.zip").read_bytes()
    assert _refresh(gtfs_dir, monkeypatch) == {"B1": None}
    assert _trips(gtfs_dir / "src.sqlite") > 0
    assert (gtfs_dir / "src.zip").read_bytes() == sent
    assert not list(gtfs_dir.glob("src.refresh*")) and not (gtfs_dir / "src.extracting").exists()


def test_a_database_a_first_import_left_empty_is_built_whole(tmp_path, monkeypatch):
    # the schema of a real file whose first line never came in
    gtfs_dir = _source(tmp_path)
    conn = sqlite3.connect(gtfs_dir / "src.sqlite")
    conn.execute("create table trips (trip_id varchar, route_id varchar)")
    conn.commit()
    conn.close()
    assert _refresh(gtfs_dir, monkeypatch) == {"B1": None}
    assert _trips(gtfs_dir / "src.sqlite") > 0 and "marker" not in _tables(gtfs_dir / "src.sqlite")
