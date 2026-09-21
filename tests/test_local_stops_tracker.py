"""Local stops with a tracker that is not there, or not placed.

A person or zone may be gone, renamed, or not loaded yet at start: its
state is then None. The refresh and the options screen read its
position, and raised on it; they now find no stop near nowhere.
"""
from __future__ import annotations

import types

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


def _hass(state):
    return types.SimpleNamespace(states=types.SimpleNamespace(get=lambda _entity: state))


PLACED = types.SimpleNamespace(attributes={"latitude": 47.9, "longitude": 1.9})
UNPLACED = types.SimpleNamespace(attributes={})


def test_position_of_a_tracker():
    assert gtfs_helper._tracker_position(_hass(PLACED), "person.a") == (47.9, 1.9)
    assert gtfs_helper._tracker_position(_hass(UNPLACED), "person.a") == (None, None)
    assert gtfs_helper._tracker_position(_hass(None), "person.gone") == (None, None)


def test_no_stop_is_near_a_missing_tracker():
    # never reaches the database: there is nowhere to look around
    assert gtfs_helper.get_local_stop_list(_hass(None), None, {"device_tracker_id": "person.gone"}) == 0


def test_the_refresh_lists_nothing_for_a_missing_tracker(monkeypatch):
    monkeypatch.setattr(gtfs_helper, "check_extracting", lambda *a: False)
    me = types.SimpleNamespace(hass=_hass(None), _data={
        "schedule": object(), "offset": 0, "file": "src", "gtfs_dir": ".",
        "device_tracker_id": "person.gone", "radius": 100})
    assert gtfs_helper.get_local_stops_next_departures(me) == []
