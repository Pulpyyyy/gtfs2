"""An entry of every version async_migrate_entry has a rule for reaches 10.4.

Home Assistant calls async_migrate_entry for an entry older than the
flow's VERSION, 10, or its MINOR_VERSION, 4, and keeps what the function
hands async_update_entry: the data, the options, the unique_id and the
versions, which it sets on the entry before the next rule reads it. The
rules, as __init__ writes them:

    4        route_type becomes 99, no filter on the line list, and the
             agency every operator; the offset moves from the data to the
             options, 0 when the data had none; the entry is then at 9
    5        route_type 99 and every operator, then at 9
    6        every operator, then at 9
    7, 8, 9  a realtime key kept under an old name becomes api_key, and
             api_key_name the name it is sent under: api_key is sent as
             Authorization, x_api_key and ocp_apim_subscription_key under
             their own names, which go from the options; the entry is then
             at 10. An entry brought to 9 by the rules above goes through
             this one too
    10.1     a datasource entry's unique_id becomes gtfs2-source-<file>,
             every other entry keeps its own; the entry is then at 10.2.
             Every entry brought to 10 above goes through this one too
    10.2     an entry whose url is no feed url (the "na" a source made
             from a zip in the folder held, and its journeys) takes the
             file:// url of the source's kept zip; a datasource entry
             drops extract_from, its url says where its feed comes from;
             an http url stays. Every entry brought to 10.2 goes through
             this one too
    10.3     a local stops entry's sensors take the entry's id at the head
             of their unique_id, in the registry, their entity_id kept; any
             other entry has nothing to rename. Every entry brought to 10.3
             goes through this one too
    10.4     left as it is

What the entry holds besides is carried through untouched. A version
below 4 has no rule, and is not checked here.
"""
from __future__ import annotations

import asyncio
import os
import types

import pytest

import ha_stub

integration = ha_stub.load("__init__")
source_refresh = ha_stub.load("data.source_refresh")

CONFIG = os.path.abspath("config")

JOURNEY = {"file": "tao", "name": "to work", "url": "na", "extract_from": "zip",
           "route": "ORLEANS:Line:40", "route_type": "3", "direction": "0",
           "origin": "a: A", "destination": "b: B"}


class _Entries:
    """hass.config_entries as a migration uses it: what async_update_entry
    is handed lands on the entry, read-only as Home Assistant keeps it."""

    def __init__(self):
        self.updates = 0

    def async_entries(self, domain=None):
        # no datasource entry to take a url from: the kept zip's
        return []

    def async_update_entry(self, entry, *, data=None, options=None, version=None,
                           minor_version=None, unique_id=None):
        self.updates += 1
        if unique_id is not None:
            entry.unique_id = unique_id
        if minor_version is not None:
            entry.minor_version = minor_version
        if data is not None:
            entry.data = types.MappingProxyType(dict(data))
        if options is not None:
            entry.options = types.MappingProxyType(dict(options))
        if version is not None:
            entry.version = version
        return True


def _hass(entries):
    return types.SimpleNamespace(
        config_entries=entries,
        config=types.SimpleNamespace(path=lambda *parts: os.path.join(CONFIG, *parts)))


def _zip_url(file):
    """The file:// url of a source's kept zip, as the component names it."""
    return source_refresh.source_zip_url(_hass(None), file)


# the journey once it reaches 10.3 and on: fetched from its source's kept zip
JOURNEY_FED = {**JOURNEY, "url": _zip_url("tao")}


def _migrated(version, data, options):
    """(version, data, options) of an entry once migrated."""
    entry = types.SimpleNamespace(
        entry_id="e1", title="to work", version=version, minor_version=1,
        unique_id="gtfs-to work",
        data=types.MappingProxyType(dict(data)), options=types.MappingProxyType(dict(options)))
    hass = _hass(_Entries())
    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True
    assert (entry.minor_version, entry.unique_id) == (4, "gtfs-to work")
    return entry.version, dict(entry.data), dict(entry.options)


def test_version_4_moves_the_offset_to_the_options_and_opens_every_line_and_operator():
    assert _migrated(4, {**JOURNEY, "offset": 5}, {"api_key": "k"}) == (
        10, {**JOURNEY_FED, "route_type": "99", "agency": "0: ALL"},
        {"offset": 5, "api_key": "k", "api_key_name": "Authorization"})


def test_version_4_without_an_offset_gets_one_of_0():
    assert _migrated(4, JOURNEY, {}) == (
        10, {**JOURNEY_FED, "route_type": "99", "agency": "0: ALL"}, {"offset": 0})


def test_version_5_opens_every_line_and_operator():
    assert _migrated(5, JOURNEY, {"x_api_key": "x", "refresh_interval": 5}) == (
        10, {**JOURNEY_FED, "route_type": "99", "agency": "0: ALL"},
        {"api_key": "x", "api_key_name": "x_api_key", "refresh_interval": 5})


def test_version_6_opens_every_operator_and_keeps_the_line_filter():
    assert _migrated(6, JOURNEY, {"ocp_apim_subscription_key": "o"}) == (
        10, {**JOURNEY_FED, "agency": "0: ALL"},
        {"api_key": "o", "api_key_name": "ocp_apim_subscription_key"})


@pytest.mark.parametrize("version", [7, 8, 9])
def test_versions_7_to_9_keep_the_realtime_key_under_its_new_name(version):
    # both old names at once: x_api_key is read after api_key, and wins
    assert _migrated(version, JOURNEY, {"api_key": "k", "x_api_key": "x"}) == (
        10, JOURNEY_FED, {"api_key": "x", "api_key_name": "x_api_key"})


@pytest.mark.parametrize("version", [7, 8, 9])
def test_versions_7_to_9_drop_an_empty_old_key_and_touch_nothing_else(version):
    assert _migrated(version, JOURNEY, {"x_api_key": "", "offset": 2}) == (
        10, JOURNEY_FED, {"offset": 2})


def _at_10(minor_version, data, unique_id):
    entries = _Entries()
    entry = types.SimpleNamespace(entry_id="e1", title="x", version=10, minor_version=minor_version,
                                  unique_id=unique_id, data=types.MappingProxyType(dict(data)),
                                  options=types.MappingProxyType({"api_key": "k"}))
    hass = _hass(entries)
    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True
    return (entry.version, entry.minor_version, entry.unique_id, dict(entry.data),
            dict(entry.options), entries.updates)


def test_version_10_4_is_left_as_it_is():
    assert _at_10(4, JOURNEY, "gtfs-to work") == (
        10, 4, "gtfs-to work", JOURNEY, {"api_key": "k"}, 0)


def test_version_10_3_has_nothing_to_rename_for_a_journey():
    # a journey's entry is not a local stops one: the registry is not read
    assert _at_10(3, JOURNEY, "gtfs-to work") == (
        10, 4, "gtfs-to work", JOURNEY, {"api_key": "k"}, 1)


SOURCE = {"kind": "datasource", "file": "gtfs-home", "url": "na", "extract_from": "zip"}
SOURCE_FED = {"kind": "datasource", "file": "gtfs-home", "url": _zip_url("gtfs-home")}


def test_version_10_2_gives_a_zip_source_and_its_journeys_the_url_of_its_kept_zip():
    assert _at_10(2, JOURNEY, "gtfs-to work") == (
        10, 4, "gtfs-to work", JOURNEY_FED, {"api_key": "k"}, 2)
    assert _at_10(2, SOURCE, "gtfs2-source-gtfs-home") == (
        10, 4, "gtfs2-source-gtfs-home", SOURCE_FED, {"api_key": "k"}, 2)


def test_version_10_2_keeps_an_http_url_and_drops_a_datasource_s_extract_from():
    hosted = {"kind": "datasource", "file": "tao", "url": "https://h/tao.zip",
              "extract_from": "url"}
    journey = {**JOURNEY, "url": "https://h/tao.zip", "extract_from": "url"}
    assert _at_10(2, hosted, "gtfs2-source-tao")[3] == {
        "kind": "datasource", "file": "tao", "url": "https://h/tao.zip"}
    assert _at_10(2, journey, "gtfs-to work")[3] == journey


def test_version_10_1_gives_a_datasource_entry_its_prefix():
    # a source named gtfs-home held the unique_id a journey named home is given
    assert _at_10(1, SOURCE, "gtfs-home") == (
        10, 4, "gtfs2-source-gtfs-home", SOURCE_FED, {"api_key": "k"}, 3)


def test_version_10_1_keeps_any_other_entry_s_unique_id():
    assert _at_10(1, JOURNEY, "gtfs-to work") == (
        10, 4, "gtfs-to work", JOURNEY_FED, {"api_key": "k"}, 3)


class _Registry:
    """The entity registry as the migration reads it: entries held by their
    config entry, a unique_id changed by entity id."""

    def __init__(self, entries):
        self.entries = {e.entity_id: e for e in entries}

    def async_update_entity(self, entity_id, *, new_unique_id):
        self.entries[entity_id].unique_id = new_unique_id


def _registered(entry_id, unique_id, domain="sensor"):
    return types.SimpleNamespace(domain=domain, config_entry_id=entry_id, unique_id=unique_id,
                                 entity_id=f"{domain}.{entry_id}_{unique_id.split('_')[0].lower()}")


def test_version_10_3_puts_the_entry_in_its_local_stop_sensors_unique_id(monkeypatch):
    # the user's entity_id stays: only the unique_id is changed, and only
    # the entry's own, another entry's sensor at the same stop for the
    # same person waits for that entry's migration
    mine = _registered("e1", "S1_local_stop_person.me")
    theirs = _registered("e2", "S1_local_stop_person.me")
    registry = _Registry([mine, theirs])
    monkeypatch.setattr(integration, "er", types.SimpleNamespace(
        async_get=lambda hass: registry,
        async_entries_for_config_entry=lambda reg, entry_id: [
            e for e in list(reg.entries.values()) if e.config_entry_id == entry_id]))
    around = {"device_tracker_id": "person.me", "file": "tao", "name": "around me",
              "url": _zip_url("tao")}
    assert _at_10(3, around, None)[:3] == (10, 4, None)
    assert (mine.entity_id, mine.unique_id) == ("sensor.e1_s1", "e1_S1_local_stop_person.me")
    assert theirs.unique_id == "S1_local_stop_person.me"
