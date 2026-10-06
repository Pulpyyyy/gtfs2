"""A night check missed while Home Assistant was down is caught up.

The last look at a source's host is kept in the zip's sidecar, so it
survives a restart. Arming the check, at start or on an options change,
also plans one look a few minutes later, which only asks the host when
the last look is older than the source's interval.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import types

import ha_stub

freshness = ha_stub.load("feed.freshness")
source_refresh = ha_stub.load("data.source_refresh")
dt_util = source_refresh.dt_util


def _hass(root):
    return types.SimpleNamespace(
        data={}, config=types.SimpleNamespace(path=lambda *p: str(root.joinpath(*p))))


def _sidecar(root, **meta):
    gtfs_dir = root / "gtfs2"
    gtfs_dir.mkdir(exist_ok=True)
    (gtfs_dir / "src.zip.meta.json").write_text(json.dumps(meta))
    return gtfs_dir / "src.zip"


def _ago(hours):
    return (dt_util.utcnow() - datetime.timedelta(hours=hours)).isoformat()


def test_a_look_is_kept_in_the_sidecar(tmp_path):
    zip_path = _sidecar(tmp_path, sha256="abc", downloaded_at=_ago(100))
    freshness.note_checked(str(zip_path))
    meta = json.loads((tmp_path / "gtfs2" / "src.zip.meta.json").read_text())
    assert meta["sha256"] == "abc"
    last = source_refresh.last_look(_hass(tmp_path), "src")
    assert dt_util.utcnow() - last < datetime.timedelta(minutes=1)


def test_no_sidecar_is_not_made_up(tmp_path):
    (tmp_path / "gtfs2").mkdir()
    freshness.note_checked(str(tmp_path / "gtfs2" / "src.zip"))
    assert not (tmp_path / "gtfs2" / "src.zip.meta.json").exists()


def test_last_look_falls_back_on_the_download(tmp_path):
    _sidecar(tmp_path, sha256="abc", downloaded_at="2026-09-01T03:00:00+00:00")
    last = source_refresh.last_look(_hass(tmp_path), "src")
    assert last == dt_util.parse_datetime("2026-09-01T03:00:00+00:00")


def test_the_next_check_announced_counts_from_the_same_last_look(tmp_path):
    # after a restart the run has learned nothing yet: the gate counts from
    # the look the zip's sidecar recorded an hour ago, and so must the next
    # check the update entity announces, not from the download days before
    zip_meta = {"sha256": "abc", "checked_at": _ago(1), "downloaded_at": _ago(100)}
    entry = types.SimpleNamespace(
        data={"file": "src", "extract_from": "url"},
        options={"static_refresh_mode": "notify", "static_check_interval": 48})
    announced = source_refresh.next_check_at(_hass(tmp_path), entry, zip_meta)
    # the gate lets a look through 12 hours before the interval is up
    assert announced - dt_util.now() >= datetime.timedelta(hours=48 - 12 - 1)


def _arm(monkeypatch, tmp_path, checked_hours_ago):
    _sidecar(tmp_path, sha256="abc", checked_at=_ago(checked_hours_ago))
    hass = _hass(tmp_path)

    async def job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = job
    planned, checked = [], []
    monkeypatch.setattr(source_refresh, "async_track_time_change",
                        lambda *a, **k: (lambda: None))
    monkeypatch.setattr(source_refresh, "async_call_later",
                        lambda h, delay, action: planned.append((delay, action)) or (lambda: None))

    async def check(h, e):
        checked.append(e)

    monkeypatch.setattr(source_refresh, "async_check_source", check)
    entry = types.SimpleNamespace(
        entry_id="e1", data={"file": "src", "extract_from": "url"},
        options={"static_refresh_mode": source_refresh.STATIC_REFRESH_AUTO,
                 "static_check_interval": 24})
    source_refresh.async_arm_source_check(hass, entry)
    assert [delay for delay, _ in planned] == [source_refresh.CATCH_UP_DELAY]
    asyncio.run(planned[0][1](None))
    return checked


def test_an_overdue_source_is_looked_at(monkeypatch, tmp_path):
    assert len(_arm(monkeypatch, tmp_path, checked_hours_ago=30)) == 1


def test_a_source_looked_at_tonight_is_left(monkeypatch, tmp_path):
    assert _arm(monkeypatch, tmp_path, checked_hours_ago=5) == []
