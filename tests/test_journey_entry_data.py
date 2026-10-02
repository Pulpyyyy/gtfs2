"""A journey or local stops entry holds its source by name, nothing of it.

The source's address, its key and its realtime feeds live on its
datasource entry. A copy on each journey only served a return to the
upstream version, which is not kept: the copies drifted (a journey made
from an existing source held file://.../tao.zip while its source held
the host's address) and spread the api key over every entry.
"""
from __future__ import annotations

import ha_stub

ha_stub.install()

rt_source = ha_stub.load("rt_source")

JOURNEY = {"file": "tao", "agency": "0: ALL", "route_type": "3", "route": "R1",
           "origin": "S1: One", "destination": "S2: Two", "name": "to work"}


def test_a_journey_keeps_nothing_of_its_source():
    typed = {**JOURNEY, "url": "https://h/tao.zip", "extract_from": "url",
             "inner_zip": "bus.zip", "api_key": "secret", "api_key_name": "token",
             "api_key_location": "header", "accept": True,
             "trip_update_url": "https://h/trips", "vehicle_position_url": "https://h/vehicles",
             "alerts_url": "https://h/alerts"}
    assert rt_source.journey_entry_data(typed) == JOURNEY


def test_local_stops_keep_their_tracker():
    typed = {"file": "tao", "device_tracker_id": "person.me", "name": "around me",
             "url": "file:///config/gtfs2/tao.zip", "extract_from": "zip"}
    assert rt_source.journey_entry_data(typed) == {
        "file": "tao", "device_tracker_id": "person.me", "name": "around me"}
