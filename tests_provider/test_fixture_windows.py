"""The validity window of every fixture feed, as read from its zip.

read_feed_window answers from feed_info.txt and the calendars of the zip
kept beside the database. The fixtures are real feeds cut down, their
calendars cut with them, so the last service day here is the fixture's,
not the network's; what holds is that feed_info is read where the feed
ships one (not on the made-up boarding line), and that the calendars
always give a last day. The values are the fixtures' own, recorded.

    pytest tests_provider/test_fixture_windows.py
"""
from __future__ import annotations

from pathlib import Path

import pytest

import ha_stub

ha_stub.install()

feed_window = ha_stub.load("feed_window")

FIXTURES = Path(__file__).parent / "fixtures"
# fixture: (feed_version, feed_end_date, first_service_day, last_service_day)
WINDOWS = {
    "adelaide": ("1698", "2027-04-09", "2026-09-09", "2027-04-09"),
    "boarding": (None, None, "2026-01-01", "2027-12-31"),
    "gvb": ("9545", "2026-12-12", "2026-08-31", "2026-11-29"),
    "palmbus": ("25082026", "2027-01-03", "2026-09-01", "2026-12-18"),
    "sncf": ("2026-08-25", "2027-01-31", "2026-08-25", "2026-12-12"),
    "sncf-journeys": ("2026-08-31", "2027-02-28", "2026-08-31", "2026-12-12"),
    "tao-journeys": (None, "2027-01-03", "2026-08-24", "2026-12-24"),
}


@pytest.mark.parametrize("fixture", sorted(WINDOWS), ids=sorted(WINDOWS))
def test_feed_window(record_property, fixture):
    window = feed_window.read_feed_window(FIXTURES / fixture / "static.zip")
    got = (window.get("feed_version"), window.get("feed_end_date"),
           window.get("first_service_day"), window.get("last_service_day"))
    record_property("case", {"fixture": fixture, "promise": "feed_window"})
    record_property("checks", [{"ok": got == WINDOWS[fixture],
                                "text": f"{fixture}: expected {WINDOWS[fixture]}, got {got}",
                                "expected": list(WINDOWS[fixture]), "got": list(got)}])
    assert got == WINDOWS[fixture]
