"""A SIRI stop-monitoring answer turned into the realtime feed's shape.

convert_realtime_siri_trips_to_json reads one stop's monitored visits
and writes each as a trip update calling at that stop: its journey as
the trip, its expected times (else the aimed ones) as the call's. Hosts
send the delivery under a Siri root (Strasbourg) or without one (MTA);
both read the same.
"""
from __future__ import annotations

import datetime
import json

import ha_stub

rt_local = ha_stub.load("rt_local")

VISIT = {"MonitoredVehicleJourney": {
    "LineRef": "A", "DirectionRef": 1,
    "FramedVehicleJourneyRef": {"DatedVehicleJourneyRef": "J1"},
    "MonitoredCall": {
        "AimedArrivalTime": "2026-10-01T08:00:00+00:00",
        "ExpectedArrivalTime": "2026-10-01T08:02:00+00:00",
        "AimedDepartureTime": "2026-10-01T08:01:00+00:00"}}}
DELIVERY = {"ServiceDelivery": {
    "ResponseTimestamp": "2026-10-01T07:59:00+00:00",
    "StopMonitoringDelivery": [{"version": "2.0", "MonitoredStopVisit": [VISIT]}]}}


def _converted(monkeypatch, answer):
    monkeypatch.setattr(rt_local, "_feed_body",
                        lambda url, headers, label: json.dumps(answer).encode())
    return rt_local.convert_realtime_siri_trips_to_json("https://h/siri?k=1", {}, "S1")


def _at(text):
    return datetime.datetime.fromisoformat(text).timestamp()


def test_a_visit_becomes_a_trip_update_at_the_stop(monkeypatch):
    feed = _converted(monkeypatch, DELIVERY)
    assert feed["header"]["gtfs_realtime_version"] == "2.0"
    assert feed["header"]["timestamp"] == "2026-10-01T07:59:00+00:00"
    [entity] = feed["entity"]
    assert entity["id"] == "J1"
    trip = entity["trip_update"]["trip"]
    assert (trip["trip_id"], trip["route_id"], trip["direction_id"]) == ("J1", "A", "1")
    # the day of the visit's times, the host naming no operating day, and
    # no start time, the host not saying when the trip left its first stop
    assert trip["start_date"] == "20261001"
    assert "start_time" not in trip
    [call] = entity["trip_update"]["stop_time_update"]
    assert call["stop_id"] == "S1"
    # expected when the host knows it, aimed otherwise
    assert call["arrival"]["time"] == _at("2026-10-01T08:02:00+00:00")
    assert call["departure"]["time"] == _at("2026-10-01T08:01:00+00:00")


def _visit(journey, **times):
    return {"MonitoredVehicleJourney": {
        "LineRef": "A", "DirectionRef": 1,
        "FramedVehicleJourneyRef": {"DatedVehicleJourneyRef": journey},
        "MonitoredCall": times}}


def _delivery(*visits):
    return {"ServiceDelivery": {
        "ResponseTimestamp": "2026-10-01T07:59:00+00:00",
        "StopMonitoringDelivery": [{"version": "2.0", "MonitoredStopVisit": list(visits)}]}}


def test_a_visit_without_a_time_does_not_cost_the_others(monkeypatch):
    # the first stop of a line gives no arrival, the last no departure, and
    # a host may leave a visit with neither: that one alone is left out
    feed = _converted(monkeypatch, _delivery(
        _visit("FIRST", AimedDepartureTime="2026-10-01T08:01:00+00:00"),
        _visit("LAST", ExpectedArrivalTime="2026-10-01T08:05:00+00:00"),
        _visit("NONE"),
        _visit("NULL", ExpectedArrivalTime=None, AimedDepartureTime="2026-10-01T08:09:00+00:00")))
    calls = {e["id"]: e["trip_update"]["stop_time_update"][0] for e in feed["entity"]}
    assert set(calls) == {"FIRST", "LAST", "NULL"}
    assert calls["FIRST"]["arrival"] == {}
    assert calls["FIRST"]["departure"]["time"] == _at("2026-10-01T08:01:00+00:00")
    assert calls["LAST"]["departure"] == {}
    assert calls["LAST"]["arrival"]["time"] == _at("2026-10-01T08:05:00+00:00")
    assert calls["NULL"]["departure"]["time"] == _at("2026-10-01T08:09:00+00:00")


def test_the_delay_is_expected_less_aimed(monkeypatch):
    feed = _converted(monkeypatch, _delivery(
        _visit("LATE", AimedArrivalTime="2026-10-01T08:00:00+00:00",
               ExpectedArrivalTime="2026-10-01T08:02:00+00:00",
               AimedDepartureTime="2026-10-01T08:01:00+00:00",
               ExpectedDepartureTime="2026-10-01T08:03:30+00:00"),
        _visit("EARLY", AimedDepartureTime="2026-10-01T09:00:00+00:00",
               ExpectedDepartureTime="2026-10-01T08:59:00+00:00"),
        _visit("AIMED", AimedDepartureTime="2026-10-01T10:00:00+00:00")))
    calls = {e["id"]: e["trip_update"]["stop_time_update"][0] for e in feed["entity"]}
    assert calls["LATE"]["arrival"]["delay"] == 120
    assert calls["LATE"]["departure"]["delay"] == 150
    assert calls["EARLY"]["departure"]["delay"] == -60
    # the host gives one time only: no delay said, the readers take the
    # gap to the timetable as for any feed that leaves it out
    assert "delay" not in calls["AIMED"]["departure"]


def test_the_trip_starts_on_the_operating_day_the_host_names(monkeypatch):
    # MTA's shape: the operating day in DataFrameRef, the trip's first
    # departure in OriginAimedDepartureTime, in the host's own offset
    night = _visit("NIGHT", AimedDepartureTime="2026-10-02T00:40:00-04:00")
    journey = night["MonitoredVehicleJourney"]
    journey["FramedVehicleJourneyRef"]["DataFrameRef"] = "2026-10-01"
    journey["OriginAimedDepartureTime"] = "2026-10-02T00:15:00-04:00"
    day = _visit("DAY", AimedDepartureTime="2026-10-01T08:01:00-04:00")
    day["MonitoredVehicleJourney"]["FramedVehicleJourneyRef"]["DataFrameRef"] = "2026-10-01"
    day["MonitoredVehicleJourney"]["OriginAimedDepartureTime"] = "2026-10-01T07:40:00-04:00"
    feed = _converted(monkeypatch, _delivery(night, day))
    trips = {e["id"]: e["trip_update"]["trip"] for e in feed["entity"]}
    assert trips["DAY"]["start_date"] == "20261001"
    assert trips["DAY"]["start_time"] == "07:40:00"
    # left after midnight, on the operating day before: past 24:00
    assert trips["NIGHT"]["start_date"] == "20261001"
    assert trips["NIGHT"]["start_time"] == "24:15:00"


def test_a_siri_root_reads_the_same(monkeypatch):
    assert _converted(monkeypatch, {"Siri": DELIVERY}) == _converted(monkeypatch, DELIVERY)


def test_an_answer_of_another_shape_is_said(monkeypatch):
    assert _converted(monkeypatch, {"Siri": {"Other": 1}}) == "issues with getting siri data"
    assert _converted(monkeypatch, {"Other": 1}) == "issues with getting siri data"
