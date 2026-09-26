"""The lists that change every minute stay out of the recorder.

The departure sensor names its unrecorded attributes by the names it
writes them under. The realtime trips were listed by the coordinator's
keys ("Next Services RT Trips"...) while the sensor writes them as
next_departures_realtime_trips and the like: those three went on
filling the recorder a row a minute.

sensor.py needs Home Assistant's sensor classes to import, so the set is
read out of its source; the names are the ones the attribute writer
really produces.
"""
from __future__ import annotations

import ast

import ha_stub

const = ha_stub.load("const")
departure_attributes = ha_stub.load("departure_attributes")

SENSOR = ha_stub.COMPONENT / "sensor.py"


def _unrecorded(class_name):
    tree = ast.parse(SENSOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (isinstance(item, ast.Assign)
                        and item.targets[0].id == "_unrecorded_attributes"):
                    names = set()
                    for elt in item.value.args[0].elts:
                        names.add(elt.value if isinstance(elt, ast.Constant)
                                  else getattr(const, elt.id))
                    return names
    raise AssertionError(f"no _unrecorded_attributes on {class_name}")


def test_the_realtime_trips_stay_out():
    written = {}
    departure_attributes.realtime_trips(written, {
        const.ATTR_NEXT_RT_TRIPS: ["T1"], const.ATTR_RT_CANCELLED: ["T2"],
        const.ATTR_RT_SKIPPED: ["T3"]})
    assert written
    assert set(written) <= _unrecorded("GTFSDepartureSensor")
