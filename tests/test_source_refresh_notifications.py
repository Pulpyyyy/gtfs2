"""What a rebuild of a source, or an import of lines, tells the user when it ends.

A failed rebuild says so, under one id per source: the lines the new
edition lost when that is the reason, a plain "not updated" otherwise.
A rebuild that goes through clears it. Lines left without a sensor get a
notification each, so a second one no longer hides the first. An import
of lines names those it brought in and, when it stopped at a line, those
it left out.
"""
from __future__ import annotations

import asyncio

import ha_stub

notifications = ha_stub.load("notifications")


def _record(monkeypatch):
    raised, dismissed = [], []

    async def notify(hass, key, notification_id, **values):
        raised.append((key, notification_id, values))

    monkeypatch.setattr(notifications, "_async_notify", notify)
    monkeypatch.setattr(notifications.persistent_notification, "async_dismiss",
                        lambda hass, notification_id: dismissed.append(notification_id))
    return raised, dismissed


def test_a_failed_refresh_says_so(monkeypatch):
    raised, dismissed = _record(monkeypatch)
    asyncio.run(notifications.async_notify_refresh(None, "src", False))
    assert raised == [("refresh_failed", "gtfs2_refresh_src", {"file": "src"})]
    assert not dismissed


def test_lost_lines_are_named(monkeypatch):
    raised, _ = _record(monkeypatch)
    asyncio.run(notifications.async_notify_refresh(None, "src", False, ["1:A", "1:B"]))
    assert raised == [("lines_missing", "gtfs2_refresh_src", {"file": "src", "lines": "A, B"})]


def test_a_refresh_that_goes_through_clears_it(monkeypatch):
    raised, dismissed = _record(monkeypatch)
    asyncio.run(notifications.async_notify_refresh(None, "src", True))
    assert not raised
    assert dismissed == ["gtfs2_refresh_src"]


def test_an_import_names_the_lines_it_brought_in(monkeypatch):
    raised, _ = _record(monkeypatch)
    asyncio.run(notifications.async_notify_import(None, "src", ["1:A", "1:B"], {"1:A": 12, "1:B": 0}))
    assert raised == [("import_done", "gtfs2_import_src", {"file": "src", "lines": "A, B"})]


def test_an_import_that_stops_at_a_line_names_those_left_out(monkeypatch):
    # the copy of B failed: the import stopped there and never tried C
    raised, _ = _record(monkeypatch)
    asyncio.run(notifications.async_notify_import(None, "src", ["1:A", "1:B", "1:C"], {"1:A": 12}))
    assert raised == [("import_partial", "gtfs2_import_src",
                       {"file": "src", "lines": "A", "missing": "B, C"})]


def test_an_import_that_brings_nothing_says_so(monkeypatch):
    raised, _ = _record(monkeypatch)
    asyncio.run(notifications.async_notify_import(None, "src", ["1:A"], {}))
    assert raised == [("import_failed", "gtfs2_import_src", {"file": "src"})]


def test_each_orphaned_line_has_its_own(monkeypatch):
    raised, _ = _record(monkeypatch)
    asyncio.run(notifications.async_notify_line_orphaned(None, "src", "A"))
    asyncio.run(notifications.async_notify_line_orphaned(None, "src", "B"))
    assert [r[1] for r in raised] == ["gtfs2_prune_src_A", "gtfs2_prune_src_B"]
