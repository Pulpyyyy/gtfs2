"""A key that travels in the query reaches the host once.

The local stops coordinator builds its trip updates url with the key
already in it; the key fields then rode along to get_gtfs_rt, which
appended the key a second time: ?api_key=K&api_key=K, which a strict host
refuses. A key that travels in a header still reaches get_gtfs_rt, which
is where the header is built. Reuses the captured cases of
test_stop_combined.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from freezegun import freeze_time

import test_stop_combined as combined

gtfs_helper = combined.gtfs_helper
dt_util = combined.dt_util
rt_source = combined.ha_stub.load("rt_source")


def _asked(location):
    """The rt data get_gtfs_rt receives from a local stops refresh."""
    case_id, case_dir = combined.CASES[0]
    rows = combined._parse_literal(combined._find_case_file(
        case_dir, case_id, "_static_realtime_stop_input_fetch_departure_rows.txt").read_text(encoding="utf-8"))
    _label, captured_at = combined._parse_datetime_capture(combined._find_case_file(
        case_dir, case_id, "_static_realtime_stop_input_datetime.txt").read_text(encoding="utf-8"))
    dt_util.set_default_time_zone(dt_util.get_time_zone(combined.TIMEZONE))
    hass = combined._FakeHass(combined.TIMEZONE)
    cfg = {"api_key": "K", "api_key_name": "api_key", "api_key_location": location}
    context = combined._LocalStopContext(hass, 0, "local_stop_name")
    context._rt_key = dict(cfg)
    # what the coordinator builds for a local stops entry
    context._trip_update_url = rt_source.with_query_key("https://h/tu", cfg)
    asked = []
    at = captured_at.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    with freeze_time(at, tz_offset=0), \
            patch.object(gtfs_helper, "get_gtfs_rt", lambda hass, path, data: asked.append(data) or "error"):
        gtfs_helper._interpret_local_stop_rows(context, rows)
    return asked[0]


def test_a_query_key_reaches_the_host_once():
    data = _asked("query_string")
    assert rt_source.with_query_key(data["url"], data) == "https://h/tu?api_key=K"


def test_a_header_key_still_reaches_get_gtfs_rt():
    data = _asked("header")
    assert data["url"] == "https://h/tu"
    assert (data["api_key"], data["api_key_location"]) == ("K", "header")
