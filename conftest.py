"""What the tests of tests/ and tests_provider/ share.

Home Assistant's default zone is one global the integration reads through
dt_util.now(), and many tests set it to the zone they read a timetable in.
It is put back to UTC after each test, so a test that does not set it reads
UTC whatever ran before it, not the zone the previous test left behind.
"""
from __future__ import annotations

import datetime
import sys

import pytest


@pytest.fixture(autouse=True)
def _default_zone_back_to_utc():
    yield
    dt_util = sys.modules.get("homeassistant.util.dt")
    if dt_util is not None:
        dt_util.set_default_time_zone(datetime.timezone.utc)
