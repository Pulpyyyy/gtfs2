"""A local stop in another time zone than its agency's.

The query hands _build_local_stop_element the departure as the agency's
wall clock. Amtrak's agency runs on New York time while its stops keep
their own zones: Emeryville is on Los Angeles time, Chicago on Chicago
time. The departure shown is converted to the stop's zone, and the check
against now read the agency's clock as if it were the stop's: a stop west
of the agency kept departures already gone, up to its offset (the 48-feed
sweep, Amtrak). A stop east of its agency would drop departures still to
come by the same reading; no feed here has one.
"""
from __future__ import annotations

import datetime
import types
import zoneinfo

import pytest

import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")

NEW_YORK = zoneinfo.ZoneInfo("America/New_York")
LOS_ANGELES = zoneinfo.ZoneInfo("America/Los_Angeles")
CHICAGO = zoneinfo.ZoneInfo("America/Chicago")
# 12:00 in New York, 09:00 in Los Angeles, 11:00 in Chicago
NOW = datetime.datetime(2026, 9, 22, 16, 0, tzinfo=datetime.timezone.utc)

ROW = {
    "trip_id": "T1", "direction_id": 0, "trip_short_name": "3711",
    "route_id": "R1", "stop_id": "EMY", "stop_sequence": 3,
    "stop_name": "Emeryville", "route_short_name": None,
    "route_long_name": "Coast Starlight", "trip_headsign": "Seattle",
}


def _element(agency_clock, stop_zone):
    """The element for a departure at agency_clock, New York's wall clock."""
    return gtfs_helper._build_local_stop_element(
        types.SimpleNamespace(_realtime=False, _icon="mdi:train"), ROW,
        agency_clock, NEW_YORK, stop_zone, NOW, True)


@pytest.mark.parametrize("stop_zone", [LOS_ANGELES, CHICAGO, NEW_YORK])
def test_a_departure_gone_is_left_out_whatever_the_stop_zone(stop_zone):
    # 11:50 in New York: ten minutes ago, wherever the stop is
    assert _element("2026-09-22 11:50:00", stop_zone) is None


@pytest.mark.parametrize("stop_zone", [LOS_ANGELES, CHICAGO, NEW_YORK])
def test_a_departure_to_come_is_listed_in_the_stop_clock(stop_zone):
    # 12:10 in New York: ten minutes from now
    element = _element("2026-09-22 12:10:00", stop_zone)
    assert element is not None
    shown = datetime.datetime(2026, 9, 22, 12, 10, tzinfo=NEW_YORK).astimezone(stop_zone)
    assert element["departure"] == shown.strftime(gtfs_helper.TIME_STR_FORMAT)
