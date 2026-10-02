"""What a rebuild of a source, or an import of lines, tells the user when it ends.

A failed rebuild raises a repairs issue, one per source: the lines the
new edition lost when that is the reason (nothing to fix in one click),
a plain "not updated" otherwise (its fix tries again), each replacing the
other. A rebuild that goes through clears both. Lines left without a
sensor get an issue each, so a second one no longer hides the first, and
a sensor reading the line again clears it. An import of lines, a report
of what the user just did, stays a notification: it names those it
brought in and, when it stopped at a line, those it left out.
"""
from __future__ import annotations

import asyncio

import ha_stub

notifications = ha_stub.load("notifications")
ISSUES = ha_stub.ISSUES


def _record(monkeypatch):
    raised = []

    async def notify(hass, key, notification_id, **values):
        raised.append((key, notification_id, values))

    monkeypatch.setattr(notifications, "_async_notify", notify)
    ISSUES.clear()
    return raised


def _issue(issue_id):
    return ISSUES.get(("gtfs2", issue_id))


def test_a_failed_refresh_says_so(monkeypatch):
    raised = _record(monkeypatch)
    asyncio.run(notifications.async_notify_refresh(None, "src", False))
    issue = _issue("refresh_failed_src")
    assert (issue["translation_key"], issue["is_fixable"]) == ("refresh_failed", True)
    assert issue["translation_placeholders"] == {"file": "src"}
    assert issue["data"] == {"file": "src"}
    assert not raised


def test_lost_lines_are_named(monkeypatch):
    _record(monkeypatch)
    asyncio.run(notifications.async_notify_refresh(None, "src", False))
    asyncio.run(notifications.async_notify_refresh(None, "src", False, ["1:A", "1:B"]))
    issue = _issue("lines_missing_src")
    assert (issue["translation_key"], issue["is_fixable"]) == ("lines_missing", False)
    assert issue["translation_placeholders"] == {"file": "src", "lines": "A, B"}
    # the source has one reason not to be updated at a time
    assert _issue("refresh_failed_src") is None


def test_a_refresh_that_goes_through_clears_it(monkeypatch):
    _record(monkeypatch)
    asyncio.run(notifications.async_notify_refresh(None, "src", False))
    asyncio.run(notifications.async_notify_refresh(None, "other", False, ["1:A"]))
    asyncio.run(notifications.async_notify_refresh(None, "src", True))
    asyncio.run(notifications.async_notify_refresh(None, "other", True))
    assert ISSUES == {}


def test_an_import_names_the_lines_it_brought_in(monkeypatch):
    raised = _record(monkeypatch)
    asyncio.run(notifications.async_notify_import(None, "src", ["1:A", "1:B"], {"1:A": 12, "1:B": 0}))
    assert raised == [("import_done", "gtfs2_import_src", {"file": "src", "lines": "A, B"})]


def test_an_import_that_stops_at_a_line_names_those_left_out(monkeypatch):
    # the copy of B failed: the import stopped there and never tried C
    raised = _record(monkeypatch)
    asyncio.run(notifications.async_notify_import(None, "src", ["1:A", "1:B", "1:C"], {"1:A": 12}))
    assert raised == [("import_partial", "gtfs2_import_src",
                       {"file": "src", "lines": "A", "missing": "B, C"})]


def test_an_import_that_brings_nothing_says_so(monkeypatch):
    raised = _record(monkeypatch)
    asyncio.run(notifications.async_notify_import(None, "src", ["1:A"], {}))
    assert raised == [("import_failed", "gtfs2_import_src", {"file": "src"})]


def test_each_orphaned_line_has_its_own(monkeypatch):
    raised = _record(monkeypatch)
    asyncio.run(notifications.async_notify_line_orphaned(None, "src", "R1", "A"))
    asyncio.run(notifications.async_notify_line_orphaned(None, "src", "R2", "B"))
    assert sorted(key[1] for key in ISSUES) == ["line_orphaned_src/R1", "line_orphaned_src/R2"]
    issue = _issue("line_orphaned_src/R1")
    assert (issue["translation_key"], issue["is_fixable"]) == ("line_orphaned", True)
    assert issue["translation_placeholders"] == {"file": "src", "line": "A"}
    assert issue["data"] == {"file": "src", "route": "R1", "line": "A"}
    assert not raised


def test_two_sources_and_lines_never_share_an_orphan_issue(monkeypatch):
    # source names and route ids both hold "_": "tao_bus" + "12" and
    # "tao" + "bus_12" read as one id, and the second issue replaced the
    # first
    _record(monkeypatch)
    asyncio.run(notifications.async_notify_line_orphaned(None, "tao_bus", "12", "12"))
    asyncio.run(notifications.async_notify_line_orphaned(None, "tao", "bus_12", "Bus 12"))
    assert len(ISSUES) == 2


def test_a_line_read_again_clears_the_issue_an_older_version_raised(monkeypatch):
    # an issue is persistent: one raised under the former id outlives the
    # upgrade, and the line read again clears it too
    _record(monkeypatch)
    ISSUES[("gtfs2", "line_orphaned_src_R1")] = {"data": {"file": "src", "route": "R1"}}
    notifications.clear_line_orphaned(None, "src", "R1")
    assert ISSUES == {}


def test_a_line_read_again_is_no_orphan(monkeypatch):
    _record(monkeypatch)
    asyncio.run(notifications.async_notify_line_orphaned(None, "src", "R1", "A"))
    notifications.clear_line_orphaned(None, "src", "R1")
    assert ISSUES == {}
