"""The sources the flow lists: whole names, no working files.

The datasources come from the .sqlite files of the gtfs2 folder and the
zips waiting there; a refresh or an import works in files of its own
beside a source, and those are not sources.
"""
from __future__ import annotations

import asyncio
import types

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


def _hass(root):
    async def job(fn, *args):
        return fn(*args)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda p: str(root / p)),
        async_add_executor_job=job)


def _folder(root, names):
    gtfs_dir = root / "gtfs2"
    gtfs_dir.mkdir()
    for name in names:
        (gtfs_dir / name).write_bytes(b"x")


def test_datasources_by_whole_name(tmp_path):
    _folder(tmp_path, ["tao.sqlite", "tao.zip", "sncf.v2.sqlite", "tao.refresh.sqlite",
                       "tao.import.sqlite", "tao.sqlite-journal", "tao.sqlite.meta.json"])
    got = asyncio.run(gtfs_helper.get_datasources(_hass(tmp_path), "gtfs2"))
    assert got == ["sncf.v2", "tao"]


def test_zips_leave_out_what_an_import_is_working_on(tmp_path):
    _folder(tmp_path, ["tao.zip", "tao.import.sqlite.zip", "tao_temp.zip", "tao.zip.new",
                       "zou.zip"])
    got = asyncio.run(gtfs_helper.get_zipfiles(_hass(tmp_path), "gtfs2"))
    assert got == ["tao", "zou"]


def test_a_missing_folder_is_made(tmp_path):
    assert asyncio.run(gtfs_helper.get_datasources(_hass(tmp_path), "gtfs2")) == []
    assert (tmp_path / "gtfs2").is_dir()
