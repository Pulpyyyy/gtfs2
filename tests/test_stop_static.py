"""Generic runner for every captured local-stop coordinator case.

Discovers every `case_N` group under `case_stop/` (by shared case
number in the filename, same convention as test_route_combined.py) and,
for each one, runs the real, unmodified
`GTFSLocalStopUpdateCoordinator._async_update_data()` and checks its
real return value against the case's captured output.

Only the true database boundaries are replaced:

    get_gtfs                          -- opens a real GTFS sqlite file
    check_datasource_index            -- runs SQL against the database
    get_local_stops_next_departures   -- queries the database (via
                                          `_fetch_local_stop_rows`);
                                          replaced with the real,
                                          unmodified
                                          `_interpret_local_stop_rows()`'s
                                          own output, computed from this
                                          case's rows

Every line of `coordinator.py` itself, and `_interpret_local_stop_rows`
(including `_build_local_stop_element` and the grouping/sort logic),
run for real, unmodified.

RT is intentionally out of scope here -- `real_time` is left out of
the fake config entry's options, so the coordinator's RT branch never
triggers, same as `_realtime = False` on the interpret-side stand-in.

Adding a new case is: capture three files into a new `case_N` group.
Nothing here needs to change.
"""
from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import case_files
import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

# Loaded on their own rather than through the package, whose __init__
# pulls in the platforms and with them the rest of Home Assistant.
local_stops = ha_stub.load("local_stops")
coordinator_mod = ha_stub.load("coordinator")

CASE_ROOT = Path(__file__).parent / "case_stop"

# Fixed integration config -- not per-case diffable data, so not stored
# as its own case file. Same choice as TIMEZONE in the route test suites.
TIMEZONE = "Europe/Paris"


class _FakeConfig:
    def __init__(self, time_zone: str) -> None:
        self.time_zone = time_zone

    def path(self, value: str = "") -> str:
        return value


class _FakeHass:
    """Stand-in for `homeassistant.core.HomeAssistant`.

    `.async_add_executor_job` is exercised for real by
    `_async_update_data()` -- every call it makes goes through here,
    running the target function synchronously since there's no real
    executor thread pool needed for a single test invocation.
    """

    def __init__(self, time_zone: str) -> None:
        self.config = _FakeConfig(time_zone)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _FakeConfigEntry:
    """Stand-in for `homeassistant.config_entries.ConfigEntry`.

    `.data` and `.options` are read directly by `_async_update_data()`.
    `real_time` is deliberately absent from `.options` -- RT is out of
    scope for this test file.
    """

    def __init__(self) -> None:
        self.entry_id = "test_entry"
        self.data = {
            "name": "local_stop_name",
            "file": "zou_proximite",
            "device_tracker_id": "device_tracker.test_device",
        }
        self.options = {
            "offset": 0,
            "timerange": 30,
            "radius": 200,
        }


class _LocalStopContext:
    """Stand-in for the object `_interpret_local_stop_rows` reads `self`
    off of when called directly (bypassing the coordinator's own
    executor-job call, which is mocked here since it also touches the
    database via `_fetch_local_stop_rows`).

    Only what that function and `_build_local_stop_element` actually
    read: `self.hass.config.time_zone`, `self._data["offset"]`, and
    `self._realtime` (gating the RT branch, left False -- static only).
    """

    def __init__(self, hass, offset: int) -> None:
        self.hass = hass
        self._data = {"offset": offset}
        self._realtime = False


CASES = case_files.discover_cases(CASE_ROOT)

# the schedule get_gtfs hands the refresh. An object and not a word: a
# word is what get_gtfs answers when there is no database, and the refresh
# reads nothing from it. The captures name it by the word they were
# recorded with
SCHEDULE = object()


@pytest.mark.parametrize("case_id,case_dir", CASES, ids=[c[0] for c in CASES])
def test_stop_static(case_id: str, case_dir: Path):
    rows = case_files.parse_literal(
        case_files.find_case_file(case_dir, case_id, "_static_stop_input_fetch_departure_rows.txt").read_text(encoding="utf-8")
    )
    label, captured_at = case_files.parse_datetime_capture(
        case_files.find_case_file(case_dir, case_id, "_static_stop_input_datetime.txt").read_text(encoding="utf-8")
    )
    expected = case_files.parse_literal(
        case_files.find_case_file(case_dir, case_id, "_static_stop_output_coordinator_data.txt").read_text(encoding="utf-8")
    )

    dt_util.set_default_time_zone(dt_util.get_time_zone(TIMEZONE))
    hass = _FakeHass(TIMEZONE)
    entry = _FakeConfigEntry()
    captured_at_utc = captured_at.astimezone(datetime.timezone.utc)

    with freeze_time(captured_at_utc.replace(tzinfo=None), tz_offset=0):
        ctx = _LocalStopContext(hass, entry.options["offset"])
        precomputed_local_stops = local_stops._interpret_local_stop_rows(ctx, rows)

        coord = coordinator_mod.GTFSLocalStopUpdateCoordinator(hass, entry)

        with patch.object(coordinator_mod, "get_gtfs", return_value=SCHEDULE), \
             patch.object(coordinator_mod, "check_datasource_index", return_value=None), \
             patch.object(coordinator_mod, "get_local_stops_next_departures", return_value=precomputed_local_stops):
            result = asyncio.run(coord._async_update_data())

    result = case_files.normalize_datetimes(result)
    assert result["schedule"] is SCHEDULE
    result["schedule"] = "FAKE_SCHEDULE"

    assert result == expected, (
        f"[{case_id}] ({label}) coordinator.data did not match "
        f"case_*_static_stop_output_coordinator_data.txt"
    )


def test_a_source_without_a_database_says_it_once(caplog):
    """get_gtfs answers a word while the source has no database. The index
    check and the stops each warned at every refresh; the stop sensors,
    one per stop found, cannot say it, none is found. The coordinator
    says it once, reads nothing, and says it again only once the database
    has come back and gone again."""
    dt_util.set_default_time_zone(dt_util.get_time_zone(TIMEZONE))
    coord = coordinator_mod.GTFSLocalStopUpdateCoordinator(_FakeHass(TIMEZONE), _FakeConfigEntry())
    schedules = iter(["not_built", "not_built", SCHEDULE, "not_built"])
    reads = []

    def refresh():
        with patch.object(coordinator_mod, "get_gtfs", return_value=next(schedules)), \
             patch.object(coordinator_mod, "check_datasource_index",
                          side_effect=lambda *args: reads.append("index")), \
             patch.object(coordinator_mod, "get_local_stops_next_departures",
                          side_effect=lambda *args: reads.append("stops") or []):
            return asyncio.run(coord._async_update_data())

    def said():
        return [r for r in caplog.records if r.levelname == "WARNING"]

    with caplog.at_level("DEBUG"):
        first = refresh()
        refresh()
    assert first["local_stops_next_departures"] == [] and first["extracting"] is False
    assert reads == []
    assert len(said()) == 1 and "not_built" in said()[0].getMessage()
    with caplog.at_level("DEBUG"):
        refresh()
    assert reads == ["index", "stops"]
    with caplog.at_level("DEBUG"):
        refresh()
    assert len(said()) == 2
