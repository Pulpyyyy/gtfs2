"""A realtime feed that cannot be fetched leaves the local stops timetable.

The local stops sensor downloads the trip updates first; when that
failed, every departure went with it and the sensor showed nothing for
the cycle. The timetable now stands on its own, without delays, and the
host is not asked again line by line. Reuses the captured cases of
test_stop_combined.
"""
from __future__ import annotations

import datetime
from unittest.mock import patch

from freezegun import freeze_time

import test_stop_combined as combined

gtfs_helper = combined.gtfs_helper
dt_util = combined.dt_util


def _departures(stops):
    return sorted((d["stop_id"], str(d["trip_id"]), str(d["departure_datetime"]))
                  for stop in stops for d in stop["departure"])


def test_a_failed_download_keeps_the_timetable():
    assert combined.CASES
    for case_id, case_dir in combined.CASES:
        rows = combined._parse_literal(combined._find_case_file(
            case_dir, case_id, "_static_realtime_stop_input_fetch_departure_rows.txt").read_text(encoding="utf-8"))
        _label, captured_at = combined._parse_datetime_capture(combined._find_case_file(
            case_dir, case_id, "_static_realtime_stop_input_datetime.txt").read_text(encoding="utf-8"))
        dt_util.set_default_time_zone(dt_util.get_time_zone(combined.TIMEZONE))
        hass = combined._FakeHass(combined.TIMEZONE)
        at = captured_at.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        with freeze_time(at, tz_offset=0):
            static = combined._LocalStopContext(hass, 0, "local_stop_name")
            static._realtime = False
            timetable = gtfs_helper._interpret_local_stop_rows(static, rows)

            failing = combined._LocalStopContext(hass, 0, "local_stop_name")
            with patch.object(gtfs_helper, "get_gtfs_rt", return_value="error"), \
                    patch.object(gtfs_helper, "get_gtfs_feed_entities",
                                 side_effect=AssertionError("the host was asked again")):
                got = gtfs_helper._interpret_local_stop_rows(failing, rows)
        assert _departures(timetable), case_id
        assert isinstance(got, list), case_id
        assert _departures(got) == _departures(timetable), case_id
