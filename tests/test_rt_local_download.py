"""update_gtfs_rt_local keeps the feed it downloads in a file of its own.

get_gtfs_rt reads the feed as the sensors do (rt_feed._feed_body) and
writes it beside the source as <file>.rt. A feed that cannot be read
leaves the last good file in place: an error page written first used to
replace it, and the readers parsed that instead.
"""
from __future__ import annotations

import types

import ha_stub

rt_local = ha_stub.load("feed.rt_local")


def _hass(tmp_path):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))))


def _download(tmp_path, monkeypatch, body):
    monkeypatch.setattr(rt_local, "_feed_body", lambda url, headers, label: body)
    return rt_local.get_gtfs_rt(_hass(tmp_path), "gtfs2", {"url": "https://h/rt", "file": "src"})


def test_the_feed_read_is_written_beside_the_source(tmp_path, monkeypatch):
    assert _download(tmp_path, monkeypatch, b"feed") == "ok"
    assert (tmp_path / "gtfs2" / "src.rt").read_bytes() == b"feed"


def test_a_feed_that_cannot_be_read_keeps_the_last_file(tmp_path, monkeypatch):
    assert _download(tmp_path, monkeypatch, b"feed") == "ok"
    assert _download(tmp_path, monkeypatch, None) == "no_rt_data_file"
    assert (tmp_path / "gtfs2" / "src.rt").read_bytes() == b"feed"
