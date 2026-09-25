"""An entry of every version async_migrate_entry has a rule for reaches 10.

Home Assistant calls async_migrate_entry for an entry older than the
flow's VERSION, 10, and keeps what the function hands async_update_entry:
the data, the options and the version, which it sets on the entry before
the next rule reads it. The rules, as __init__ writes them:

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
    10       left as it is

What the entry holds besides is carried through untouched. A version
below 4 has no rule, and is not checked here.
"""
from __future__ import annotations

import asyncio
import types

import pytest

import ha_stub

integration = ha_stub.load("__init__")

JOURNEY = {"file": "tao", "name": "to work", "url": "na", "extract_from": "zip",
           "route": "ORLEANS:Line:40", "route_type": "3", "direction": "0",
           "origin": "a: A", "destination": "b: B"}


class _Entries:
    """hass.config_entries as a migration uses it: what async_update_entry
    is handed lands on the entry, read-only as Home Assistant keeps it."""

    def __init__(self):
        self.updates = 0

    def async_update_entry(self, entry, *, data=None, options=None, version=None):
        self.updates += 1
        if data is not None:
            entry.data = types.MappingProxyType(dict(data))
        if options is not None:
            entry.options = types.MappingProxyType(dict(options))
        if version is not None:
            entry.version = version
        return True


def _migrated(version, data, options):
    """(version, data, options) of an entry once migrated."""
    entry = types.SimpleNamespace(
        entry_id="e1", title="to work", version=version, minor_version=1,
        data=types.MappingProxyType(dict(data)), options=types.MappingProxyType(dict(options)))
    hass = types.SimpleNamespace(config_entries=_Entries())
    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True
    return entry.version, dict(entry.data), dict(entry.options)


def test_version_4_moves_the_offset_to_the_options_and_opens_every_line_and_operator():
    assert _migrated(4, {**JOURNEY, "offset": 5}, {"api_key": "k"}) == (
        10, {**JOURNEY, "route_type": "99", "agency": "0: ALL"},
        {"offset": 5, "api_key": "k", "api_key_name": "Authorization"})


def test_version_4_without_an_offset_gets_one_of_0():
    assert _migrated(4, JOURNEY, {}) == (
        10, {**JOURNEY, "route_type": "99", "agency": "0: ALL"}, {"offset": 0})


def test_version_5_opens_every_line_and_operator():
    assert _migrated(5, JOURNEY, {"x_api_key": "x", "refresh_interval": 5}) == (
        10, {**JOURNEY, "route_type": "99", "agency": "0: ALL"},
        {"api_key": "x", "api_key_name": "x_api_key", "refresh_interval": 5})


def test_version_6_opens_every_operator_and_keeps_the_line_filter():
    assert _migrated(6, JOURNEY, {"ocp_apim_subscription_key": "o"}) == (
        10, {**JOURNEY, "agency": "0: ALL"},
        {"api_key": "o", "api_key_name": "ocp_apim_subscription_key"})


@pytest.mark.parametrize("version", [7, 8, 9])
def test_versions_7_to_9_keep_the_realtime_key_under_its_new_name(version):
    # both old names at once: x_api_key is read after api_key, and wins
    assert _migrated(version, JOURNEY, {"api_key": "k", "x_api_key": "x"}) == (
        10, JOURNEY, {"api_key": "x", "api_key_name": "x_api_key"})


@pytest.mark.parametrize("version", [7, 8, 9])
def test_versions_7_to_9_drop_an_empty_old_key_and_touch_nothing_else(version):
    assert _migrated(version, JOURNEY, {"x_api_key": "", "offset": 2}) == (
        10, JOURNEY, {"offset": 2})


def test_version_10_is_left_as_it_is():
    entries = _Entries()
    entry = types.SimpleNamespace(entry_id="e1", title="to work", version=10, minor_version=1,
                                  data=types.MappingProxyType(dict(JOURNEY)),
                                  options=types.MappingProxyType({"api_key": "k"}))
    hass = types.SimpleNamespace(config_entries=entries)
    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True
    assert (entry.version, dict(entry.data), dict(entry.options), entries.updates) == (
        10, JOURNEY, {"api_key": "k"}, 0)
