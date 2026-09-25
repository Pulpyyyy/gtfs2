"""Removing a datasource takes every file of it, and only those.

A source leaves more than its zip and database beside it: the record of
the installed edition, and what a download, a refresh or an import
stopped half way leaves. A new source of the same name must start from
nothing, and another source's files must stay.
"""
from __future__ import annotations

import types
from pathlib import Path

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


def test_remove_without_database_keeps_it(tmp_path):
    gtfs_dir = _lay_out(tmp_path)
    gtfs_helper.remove_datasource(_hass(tmp_path), "gtfs2", "src", False)
    left = {p.name for p in gtfs_dir.iterdir()} - {"src2.sqlite", "src2.zip"}
    # the database and its own side files stay; the journal goes, as it
    # always has
    assert left == {"src" + s for s in DATABASE - {".sqlite-journal"}}
