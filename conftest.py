"""What the tests of tests/ and tests_provider/ share.

Home Assistant's default zone is one global the integration reads through
dt_util.now(), and many tests set it to the zone they read a timetable in.
It is put back to UTC after each test, so a test that does not set it reads
UTC whatever ran before it, not the zone the previous test left behind.

--component runs the suites against another checkout's integration, the
tests and fixtures staying these ones: what upstream's code answers to our
promises, or a commit extracted from history.

    pytest tests_provider/ --component ../upstream/custom_components/gtfs2
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--component", default=None,
        help="the custom_components/gtfs2 directory to test "
             "(default: the one of this checkout)")


def pytest_configure(config):
    component = config.getoption("--component")
    if component is None:
        return
    component = Path(component).resolve()
    if not (component / "manifest.json").is_file():
        raise pytest.UsageError(f"--component {component}: no manifest.json there")
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
    import ha_stub  # noqa: PLC0415
    ha_stub.COMPONENT = component


def pytest_report_header(config):
    component = config.getoption("--component")
    return [f"gtfs2 component under test: {Path(component).resolve()}"] if component else []


@pytest.fixture(autouse=True)
def _default_zone_back_to_utc():
    yield
    dt_util = sys.modules.get("homeassistant.util.dt")
    if dt_util is not None:
        dt_util.set_default_time_zone(datetime.timezone.utc)
