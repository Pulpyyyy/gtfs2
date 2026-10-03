"""The arrivals a service lists are read in the zone the sensor reads them in.

A stop time carries no zone: the agency's names it, else the stop's. A
feed naming neither for the agency nor for the destination, but one for
the origin, had the sensor read the arrival in the origin's zone and the
arrivals service in Home Assistant's. The arrival now takes the
origin's too; with no zone said anywhere, Home Assistant's, as the
departure does.
"""
from __future__ import annotations

import datetime
import zoneinfo

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

departure_services = ha_stub.load("departure_services")

UTC = datetime.timezone.utc


def _arrivals(monkeypatch, **zones):
    row = {"trip_id": "T1", "origin_depart_dt": "2026-10-01 08:00:00",
           "dest_arrival_dt": "2026-10-01 09:00:00", "agency_timezone": None,
           "origin_stop_timezone": None, "dest_stop_timezone": None, **zones}
    monkeypatch.setattr(departure_services, "_fetch_departure_rows",
                        lambda *args, **kwargs: ([row], None))
    monkeypatch.setattr(departure_services, "departure_query_args", lambda data: {})
    data = {"route_type": "3", "origin": "O: Origin", "destination": "D: Destination",
            "schedule": object()}
    return departure_services._route_departures_between(
        data, None, None, at="dest_arrival_dt")


def test_an_arrival_with_only_the_origin_zone_is_read_in_it(monkeypatch):
    dt_util.set_default_time_zone(UTC)
    assert _arrivals(monkeypatch, origin_stop_timezone="America/New_York") == [
        datetime.datetime(2026, 10, 1, 13, 0, tzinfo=UTC)]


def test_the_destination_zone_comes_before_the_origin_one(monkeypatch):
    dt_util.set_default_time_zone(UTC)
    assert _arrivals(monkeypatch, origin_stop_timezone="America/New_York",
                     dest_stop_timezone="Europe/Paris") == [
        datetime.datetime(2026, 10, 1, 7, 0, tzinfo=UTC)]


def test_an_arrival_with_no_zone_said_is_read_in_home_assistant_s(monkeypatch):
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))
    try:
        assert _arrivals(monkeypatch) == [datetime.datetime(2026, 10, 1, 7, 0, tzinfo=UTC)]
    finally:
        dt_util.set_default_time_zone(UTC)


def test_an_arrival_in_a_zone_unknown_to_home_assistant_is_read_in_the_origin_s(monkeypatch):
    # as the sensor reads it (test_departure_unknown_zone): the service
    # read it in Home Assistant's
    def as_home_assistant(name):
        """Home Assistant's get_time_zone: None for a name it cannot find."""
        try:
            return zoneinfo.ZoneInfo(name)
        except zoneinfo.ZoneInfoNotFoundError:
            return None

    monkeypatch.setattr(dt_util, "get_time_zone", as_home_assistant)
    dt_util.set_default_time_zone(UTC)
    assert _arrivals(monkeypatch, origin_stop_timezone="America/New_York",
                     dest_stop_timezone="Mars/Olympus_Mons") == [
        datetime.datetime(2026, 10, 1, 13, 0, tzinfo=UTC)]
