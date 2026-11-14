"""A line of a type the tables did not name no longer breaks its sensor.

Riga's trolleybuses are route_type 11, a type GTFS defines (12 is the
monorail) and the integration already names in its line labels. The table
of type names left both out and was read strictly: every refresh of a
trolleybus sensor raised KeyError: 11 and the sensor never showed a
departure (data.gov.lv, marsrutusaraksti10_2026.zip, 2026-11-14).
"""
from __future__ import annotations

from pygtfs.gtfs_entities import Route

import ha_stub

attributes = ha_stub.load("domain.attributes")
const = ha_stub.load("const")


def _route_type_of(route_type):
    route = Route(feed_id=1, route_id="riga_trol_3", route_short_name="3",
                  route_long_name="Centraltirgus - Sarkandaugava", route_type=route_type)
    found = {}
    attributes.route_and_trip_attributes(found, route, None)
    return found[const.ATTR_ROUTE_TYPE]


def test_a_trolleybus_line_is_named():
    assert _route_type_of(11) == "Trolleybus"
    assert const.ICONS[11] == "mdi:bus"


def test_a_monorail_line_is_named():
    assert _route_type_of(12) == "Monorail"


def test_a_type_no_table_names_keeps_its_number():
    assert _route_type_of(1601) == "1601"
