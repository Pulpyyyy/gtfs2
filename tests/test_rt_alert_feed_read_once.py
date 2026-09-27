"""An alert feed is read once for every sensor it serves.

Every sensor of a source is handed the same alert feed while the feed
cache holds it, and each one read every informed entity of it again,
through protobuf, every minute: 60 ms a sensor on SNCF's 548 alerts. The
fields are now read once per feed, and a sensor only weighs the alerts
that name one of its trips, one of its stops, its line, or something as
wide as an operator. What each of those says is unchanged: an alert of
each kind still reaches the sensor it concerns, and only that one.
"""
from __future__ import annotations

import types

from google.transit import gtfs_realtime_pb2

import ha_stub

alerts = ha_stub.load("alerts")
ha_stub.load("gtfs_rt_helper")


def _feed(*informed):
    """One alert per dict of informed entity fields, its text the fields."""
    message = gtfs_realtime_pb2.FeedMessage()
    for i, fields in enumerate(informed):
        entity = message.entity.add()
        entity.id = str(i)
        target = entity.alert.informed_entity.add()
        for name, value in fields.items():
            if name == "trip_id":
                target.trip.trip_id = value
            else:
                setattr(target, name, value)
        entity.alert.header_text.translation.add(text=str(sorted(fields.items())))
    return message.entity


def _sensor(trip="T1", stop="S1", route="R1"):
    return types.SimpleNamespace(_data={}, _route_id=route, _trip_id=trip, _stop_id=stop,
                                 _destination_id="S9", _trip_list=[trip],
                                 _direction="0", hass=None)


def _texts(got):
    return sorted(i["text"] for i in got.get("origin_stop_alerts") or [])


def test_the_fields_are_read_once_for_every_sensor(monkeypatch):
    read = []
    reader = alerts._entity_fields
    monkeypatch.setattr(alerts, "_entity_fields", lambda x: read.append(x) or reader(x))
    feed = _feed({"stop_id": "S1"}, {"route_id": "R2"}, {"trip_id": "T7"})
    for sensor in (_sensor(), _sensor("T2", "S2"), _sensor("T7", "S3", "R2")):
        alerts.journey_alerts(sensor, feed)
    assert len(read) == 3
    # a feed downloaded again is read again
    alerts.journey_alerts(_sensor(), _feed({"stop_id": "S1"}))
    assert len(read) == 4


def test_each_kind_of_alert_reaches_the_sensor_it_names():
    by_stop = {"stop_id": "S1"}
    by_trip = {"trip_id": "T1"}
    by_line = {"route_id": "R1"}
    by_operator = {"agency_id": "A"}
    elsewhere = [{"stop_id": "S5"}, {"trip_id": "T5"}, {"route_id": "R5"}]
    feed = _feed(by_stop, by_trip, by_line, by_operator, *elsewhere)
    got = alerts.journey_alerts(_sensor(), feed)
    assert _texts(got) == sorted(str(sorted(f.items())) for f in (by_stop, by_trip, by_line, by_operator))
    # a sensor on another line, stop and trip hears the operator alone
    got = alerts.journey_alerts(_sensor("T9", "S8", "R8"), feed)
    assert _texts(got) == [str(sorted(by_operator.items()))]


def test_a_trip_named_by_its_cut_id_is_still_found():
    # SNCF names a train OCESN853603F in its alerts and
    # OCESN853603F1187_F:TER:... in its timetable; a cut that stops before
    # a letter names another train
    feed = _feed({"trip_id": "OCESN853603F"}, {"trip_id": "OCESN853603"})
    got = alerts.journey_alerts(_sensor("OCESN853603F1187_F:TER", "S8", "R8"), feed)
    assert _texts(got) == [str(sorted({"trip_id": "OCESN853603F"}.items()))]
