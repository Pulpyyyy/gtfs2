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
import types

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
    monkeypatch.setattr(rt_local, "fetch", lambda *args, **kwargs: types.SimpleNamespace(
        status_code=200, content=json.dumps(answer).encode(), text=""))
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
    assert trip["start_time"] == _at("2026-10-01T08:01:00+00:00")
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


def test_a_siri_root_reads_the_same(monkeypatch):
    assert _converted(monkeypatch, {"Siri": DELIVERY}) == _converted(monkeypatch, DELIVERY)


def test_an_answer_of_another_shape_is_said(monkeypatch):
    assert _converted(monkeypatch, {"Siri": {"Other": 1}}) == "issues with getting siri data"
    assert _converted(monkeypatch, {"Other": 1}) == "issues with getting siri data"
