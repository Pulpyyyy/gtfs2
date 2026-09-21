"""Removing a datasource takes every file of it, and only those.

A source leaves more than its zip and database beside it: the record of
the installed edition, and what a download, a refresh or an import
stopped half way leaves. A new source of the same name must start from
nothing, and another source's files must stay.
"""
from __future__ import annotations

import types
from pathlib import Path

import requests

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "boarding" / "static.zip"

OWN = [".zip", ".zip.meta.json", ".zip.new", ".sqlite", ".sqlite-journal",
       ".sqlite-wal", ".sqlite-shm", ".sqlite.meta.json", "_temp.zip",
       "_temp_out.zip", ".refresh.sqlite", ".refresh.sqlite-journal",
       ".import.sqlite", ".import.sqlite-journal", ".import.sqlite.zip"]
DATABASE = {".sqlite", ".sqlite-journal", ".sqlite-wal", ".sqlite-shm", ".sqlite.meta.json"}


def _hass(root):
    return types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(root / p)))


def _lay_out(root):
    gtfs_dir = root / "gtfs2"
    gtfs_dir.mkdir()
    for suffix in OWN:
        (gtfs_dir / ("src" + suffix)).write_bytes(b"x")
    # another source whose name starts the same way
    (gtfs_dir / "src2.zip").write_bytes(b"x")
    (gtfs_dir / "src2.sqlite").write_bytes(b"x")
    return gtfs_dir


def test_remove_takes_every_file_of_the_source(tmp_path):
    gtfs_dir = _lay_out(tmp_path)
    assert gtfs_helper.remove_datasource(_hass(tmp_path), "gtfs2", "src", True) == "removed"
    assert sorted(p.name for p in gtfs_dir.iterdir()) == ["src2.sqlite", "src2.zip"]


def test_remove_spares_what_it_is_told_to_keep(tmp_path):
    gtfs_dir = _lay_out(tmp_path)
    gtfs_helper.remove_datasource(_hass(tmp_path), "gtfs2", "src", True, keep=(".zip.new",))
    left = {p.name for p in gtfs_dir.iterdir()} - {"src2.sqlite", "src2.zip"}
    assert left == {"src.zip.new"}


class _Download:
    """A host answering the feed, as get_gtfs reads a response."""
    status_code = 200
    headers = {}
    url = "https://h/src.zip"

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


def test_an_update_keeps_the_edition_it_downloaded(tmp_path, monkeypatch):
    # the refresh fallback: the old zip and database go, the download
    # staged beside them comes in. Removing "every file" once took the
    # staged download too, and the source was left with nothing at all
    feed = FEED.read_bytes()
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    (gtfs_dir / "src.zip").write_bytes(feed)
    (gtfs_dir / "src.sqlite").write_bytes(b"old")
    # the download goes through fetch, or requests.get before fetch existed
    monkeypatch.setattr(gtfs_helper, "fetch", lambda *a, **k: _Download(feed), raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Download(feed))
    # the unpacking that follows runs in a forked process, which is not
    # what is under test (and Windows has no fork)
    idle = types.SimpleNamespace(start=lambda: None, join=lambda: None)
    monkeypatch.setattr(gtfs_helper.multiprocessing, "get_context",
                        lambda _kind: types.SimpleNamespace(Process=lambda **_kw: idle))
    got = gtfs_helper.get_gtfs(_hass(tmp_path), "gtfs2", {
        "file": "src", "url": "https://h/src.zip", "extract_from": "url"}, True)
    assert got == "extracting"
    assert (gtfs_dir / "src.zip").read_bytes() == feed
    assert not (gtfs_dir / "src.zip.new").exists()


def test_remove_without_database_keeps_it(tmp_path):
    gtfs_dir = _lay_out(tmp_path)
    gtfs_helper.remove_datasource(_hass(tmp_path), "gtfs2", "src", False)
    left = {p.name for p in gtfs_dir.iterdir()} - {"src2.sqlite", "src2.zip"}
    # the database and its own side files stay; the journal goes, as it
    # always has
    assert left == {"src" + s for s in DATABASE - {".sqlite-journal"}}
