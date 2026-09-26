"""Which alerts concern a journey, by what their entities name.

The fields of one informed_entity hold together: an agency, a kind of
line or a direction other than the journey's make it about something
else, and one naming only an agency or only a kind of line is about the
whole line. A line id the feed qualifies is still the line.
"""
from __future__ import annotations

from google.transit import gtfs_realtime_pb2

import ha_stub

alerts = ha_stub.load("alerts")
ha_stub.load("gtfs_rt_helper")


def _alert(**fields):
    alert = gtfs_realtime_pb2.Alert()
    entity = alert.informed_entity.add()
    for name, value in fields.items():
        setattr(entity, name, value)
    return alert


def _scope(alert, facts=("TAO", "3"), direction="0"):
    return alerts._alert_scope(alert, {"S1"}, {"S9"}, "R1", "T1", set(), (),
                               route_facts=facts, direction=direction)


def test_the_line_by_a_qualified_id():
    assert _scope(_alert(route_id="ORLEANS:R1"))["route"]
    assert not _scope(_alert(route_id="R11"))["route"]


def test_a_whole_agency_or_kind_of_line():
    assert _scope(_alert(agency_id="TAO"))["route"]
    assert not any(_scope(_alert(agency_id="SNCF")).values())
    assert _scope(_alert(route_type=3))["route"]
    assert not any(_scope(_alert(route_type=0)).values())
    # the line's agency unknown: nothing to judge by, the alert is kept
    assert _scope(_alert(agency_id="TAO"), facts=(None, "3"))["route"]


def test_the_other_way_is_not_this_journey():
    assert _scope(_alert(route_id="R1", direction_id=0))["route"]
    assert not any(_scope(_alert(route_id="R1", direction_id=1)).values())
    # a journey with no direction takes both ways
    assert _scope(_alert(route_id="R1", direction_id=1), direction=None)["route"]


def test_a_stop_of_another_agency_is_not_ours():
    assert _scope(_alert(stop_id="S1"))["origin"]
    assert not any(_scope(_alert(stop_id="S1", agency_id="SNCF")).values())
