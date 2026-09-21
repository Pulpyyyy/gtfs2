"""An alert about a later departure of the board is not the next one's.

The sensor shows the next departure, and its board lists the ones behind
it. An alert naming one of those later trips is kept in the lists, hung
on that trip, so a card can show it there; the sentence speaks of the
departure the sensor shows, and never takes it. "T2 does not stop at
your origin" says nothing of T1 at your origin either.
"""
from __future__ import annotations

import types

from google.transit import gtfs_realtime_pb2

import ha_stub

alerts = ha_stub.load("alerts")
ha_stub.load("gtfs_rt_helper")


def _feed(*alerts_given):
    message = gtfs_realtime_pb2.FeedMessage()
    for i, (trip, stop, text) in enumerate(alerts_given):
        entity = message.entity.add()
        entity.id = str(i)
        informed = entity.alert.informed_entity.add()
        if trip:
            informed.trip.trip_id = trip
        if stop:
            informed.stop_id = stop
        entity.alert.header_text.translation.add(text=text)
    return message.entity


def _board():
    # T1 leaves next from S1 to S9, T2 is listed behind it
    return types.SimpleNamespace(_data={}, _route_id="R1", _trip_id="T1", _stop_id="S1",
                                 _destination_id="S9", _trip_list=["T1", "T2"],
                                 _direction="0", hass=None)


def test_a_later_trip_alone_stays_in_the_list():
    got = alerts.journey_alerts(_board(), _feed(("T2", None, "T2 cancelled")))
    assert [(i["text"], i["trips"], i.get("later_only")) for i in got["origin_stop_alerts"]] \
        == [("T2 cancelled", ["T2"], True)]
    assert "origin_stop_alert" not in got
    assert "destination_stop_alert" not in got


def test_a_later_trip_at_your_origin_is_not_the_next_one_at_it():
    got = alerts.journey_alerts(_board(), _feed(("T2", "S1", "T2 does not stop at S1")))
    items = got["origin_stop_alerts"]
    assert [(i["trips"], i.get("later_only")) for i in items] == [(["T2"], True)]
    assert "origin_stop_alert" not in got


def test_the_next_trip_still_takes_the_sentence():
    got = alerts.journey_alerts(_board(), _feed(("T1", None, "T1 cancelled"),
                                                ("T2", None, "T2 cancelled")))
    assert got["origin_stop_alert"] == "T1 cancelled"
    assert [i["text"] for i in got["origin_stop_alerts"]] == ["T1 cancelled", "T2 cancelled"]


def test_your_origin_alone_still_takes_the_sentence():
    got = alerts.journey_alerts(_board(), _feed((None, "S1", "S1 closed"),
                                                ("T2", None, "T2 cancelled")))
    assert got["origin_stop_alert"] == "S1 closed"
