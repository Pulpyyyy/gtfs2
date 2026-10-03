"""The fields a service call is checked against before its handler runs.

The services were registered with no schema: a missing field surfaced as
a KeyError from inside the handler (update_gtfs_local_stops without its
entity, update_gtfs_rt_local without its url), and a mistyped value as
whatever the code reading it raised. Each service now declares the
fields services.yaml lists; extra keys still pass, for the automations
written against older field lists. update_gtfs drops the extract_from it
no longer reads, as Home Assistant's cv.removed does, and the "na" it
once defaulted its address to, read as none, saying so for both, and
takes an address with a scheme the source screen takes.
"""
from __future__ import annotations

import logging
import types

import pytest
import voluptuous as vol

import ha_stub

integration = ha_stub.load("__init__")


def _registered():
    services = {}

    def register(domain, name, handler, schema=None, **kwargs):
        services[name] = schema

    integration.setup(types.SimpleNamespace(services=types.SimpleNamespace(register=register)), {})
    return services


def test_every_service_has_a_schema():
    services = _registered()
    assert sorted(services) == sorted([
        "update_gtfs", "update_gtfs_rt_local", "update_gtfs_local_stops",
        "extract_departures", "extract_arrivals", "extract_trip_stops", "prune_datasource",
        "intern_datasource"])
    assert all(schema is not None for schema in services.values()), services


def _refused(schema, data):
    with pytest.raises(vol.Invalid):
        schema(data)


def test_update_gtfs():
    schema = _registered()["update_gtfs"]
    _refused(schema, {})
    _refused(schema, {"file": "tao", "url": "ftp://feeds.example/tao.zip"})
    _refused(schema, {"file": "tao", "api_key_location": "cookie"})
    assert schema({"file": "tao", "clean_feed_info": "yes", "older_field": 1}) == {
        "file": "tao", "clean_feed_info": True, "older_field": 1}
    # nothing is added: the handler keeps its own defaults
    assert schema({"file": "tao"}) == {"file": "tao"}
    for url in ("https://h/tao.zip", "file:///config/gtfs2/tao.zip"):
        assert schema({"file": "tao", "url": url}) == {"file": "tao", "url": url}


def test_update_gtfs_drops_extract_from_and_the_old_na(caplog):
    schema = _registered()["update_gtfs"]
    with caplog.at_level(logging.WARNING):
        assert schema({"file": "tao", "extract_from": "zip", "url": "na",
                       "api_key": "na"}) == {"file": "tao"}
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "extract_from is no longer read" in said
    assert "url and api_key given as \"na\"" in said
    # an automation written for an older version is no error: Home
    # Assistant's cv.removed said it at ERROR, every call
    assert [r.levelname for r in caplog.records] == ["WARNING", "WARNING"]


def test_update_gtfs_rt_local():
    schema = _registered()["update_gtfs_rt_local"]
    _refused(schema, {"file": "tao", "url": "https://rt.example/trips"})
    _refused(schema, {"file": "tao", "rt_type": "alerts"})
    _refused(schema, {"file": "tao", "url": "https://rt.example/trips", "rt_type": "siri"})
    _refused(schema, {"file": "tao", "url": "https://rt.example/trips", "rt_type": "alerts",
                      "entity_for_siri": "not an entity"})
    assert schema({"file": "tao", "url": "https://rt.example/trips", "rt_type": "alerts"})["rt_type"] == "alerts"


def test_the_entity_services():
    services = _registered()
    for name in ("update_gtfs_local_stops", "extract_trip_stops"):
        schema = services[name]
        _refused(schema, {})
        _refused(schema, {"entity_id": "home"})
        assert schema({"entity_id": "Zone.Home"}) == {"entity_id": "zone.home"}


def test_extract_departures_and_arrivals():
    for name in ("extract_departures", "extract_arrivals"):
        schema = _registered()[name]
        _refused(schema, {})
        _refused(schema, {"config_entry": "abc", "from_time": "8h15"})
        # the selector's form and the short yaml one both reach the
        # handler as the one it parses
        for given in ("08:15:00", "08:15"):
            assert schema({"config_entry": "abc", "from_time": given})["from_time"] == "08:15:00"
        assert schema({"config_entry": "abc"}) == {"config_entry": "abc"}


def test_the_datasource_services():
    services = _registered()
    for name in ("prune_datasource", "intern_datasource"):
        schema = services[name]
        for picked in (None, "tao", ["device-id", "tao"], []):
            assert schema({"file": picked})["file"] == picked
        assert schema({"dry_run": "no"})["dry_run"] is False
        _refused(schema, {"dry_run": "maybe"})
        _refused(schema, {"file": {"tao": 1}})
