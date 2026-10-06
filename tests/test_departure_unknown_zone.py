"""A zone Home Assistant does not know reads the departure in its own zone.

A feed may name an agency zone that is misspelt, or missing from the host's
zone database. Home Assistant answers None for it, and the sensor read the
departure against no zone at all: comparing it with an aware now raised
TypeError on every refresh, and the sensor went unavailable. The departures
service and the local stops fall back on Home Assistant's zone; so does the
sensor now.
"""
from __future__ import annotations

import datetime
import types
import zoneinfo

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

departures = ha_stub.load("data.departures")

PARIS = zoneinfo.ZoneInfo("Europe/Paris")


def _as_home_assistant(name):
    """Home Assistant's get_time_zone: None for a name it cannot find."""
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return None


def _zones(monkeypatch, **zones):
    monkeypatch.setattr(dt_util, "get_time_zone", _as_home_assistant)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone="Europe/Paris"))
    item = {"agency_timezone": None, "origin_stop_timezone": None,
            "dest_stop_timezone": None, **zones}
    return departures._departure_zones(hass, item)


def test_an_unknown_agency_zone_reads_in_home_assistant_s(monkeypatch):
    dt_util.set_default_time_zone(PARIS)
    try:
        origin, destination = _zones(monkeypatch, agency_timezone="Europe/Pariss")
    finally:
        dt_util.set_default_time_zone(datetime.timezone.utc)
    assert origin == PARIS
    assert destination == PARIS


def test_an_unknown_destination_zone_reads_in_the_origin_s(monkeypatch):
    origin, destination = _zones(monkeypatch, origin_stop_timezone="America/New_York",
                                 dest_stop_timezone="America/Nowhere")
    assert origin == zoneinfo.ZoneInfo("America/New_York")
    assert destination == origin
