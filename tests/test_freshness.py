"""The download guards, the sidecar, the freshness probe, and where the
static feed's address and key come from.

No network: every response is built by hand and the host is a stand-in
for fetch. The promises:

    guards       a payload that is not a zip never replaces anything, and
                 leaves neither a zip nor a staged file behind; a good
                 download is staged, swapped in, and its sidecar records
                 the validators and the size the host sent
    probe        one small request answers whether the feed changed: a 304
                 is "unchanged", and the condition sent is the kept ETag; a
                 200 with the same validators (a weak ETag included) is
                 "unchanged", with new ones "changed" and names the new
                 version; no validators is "unknown", a dead host "error";
                 no sidecar at all is "changed", since one refresh rewrites
                 it; a sidecar holding only a hash is "unknown"; a host
                 refusing HEAD is asked with a conditional GET
    fetch        a new download moves the sidecar with it; a download that
                 is no zip answers None and leaves the kept zip whole
    key trio     the static key is stored as three fields behind a real key,
                 the location alone without one, a blank key being none
    resolution   a source that has not taken the key over yet reads it from
                 the journey entry that carries one, a keyless journey never
                 stripping it, and its own url wins; the refresh data carries
                 the journeys' import flags; a source that took the key over
                 speaks for itself, and one that dropped it resolves none
    mirror       the source's url and key land on every journey entry; a key
                 the source dropped comes off the entries that had it; an
                 entry with nothing to change is not written

Already promised elsewhere, not repeated here: same bytes are not kept and
new bytes are (test_fetch_if_new.py), and removing a datasource takes the
sidecar with the zip (test_remove_datasource.py).
"""
from __future__ import annotations

import asyncio
import io
import os
import types
import zipfile
from pathlib import Path

import pytest

import ha_stub

freshness = ha_stub.load("freshness")
rt_source = ha_stub.load("rt_source")
source_refresh = ha_stub.load("source_refresh")

URL = "https://example.org/gtfs.zip"
DATA = {"file": "tao", "url": URL}
FIRST_HEADERS = {"ETag": '"aaa"', "Last-Modified": "Mon, 01 Sep 2026 17:33:00 GMT"}


def _zip_bytes(marker):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        zout.writestr("agency.txt", "agency_id,agency_name\nX," + marker)
        # the three tables a staged download must carry to be a feed at
        # all: stage_zip refuses a zip without them
        zout.writestr("routes.txt", "route_id\nR\n")
        zout.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
        zout.writestr("stop_times.txt", "trip_id,stop_id,stop_sequence\nT,A,1\n")
    return buffer.getvalue()


def _response(content=b"", headers=None, status=200):
    return types.SimpleNamespace(
        content=content, headers=headers or {}, url=URL, status_code=status,
        raise_for_status=lambda: None, close=lambda: None)


def _answering(reply, asked=None):
    """fetch, answering every request with reply and noting its headers."""
    def fetch(method, url, headers=None, **kwargs):
        if asked is not None:
            asked.append((method, headers or {}))
        return reply(method) if callable(reply) else reply
    return fetch


def _dead(method, url, **kwargs):
    raise OSError("no route to host")


@pytest.fixture
def kept(tmp_path):
    """A source whose first download went through: its zip and sidecar."""
    zip_path = str(tmp_path / "tao.zip")
    first = _response(_zip_bytes("v1"), FIRST_HEADERS)
    freshness.adopt_zip(first, freshness.stage_zip(first, zip_path), zip_path)
    return zip_path


# --- guards -------------------------------------------------------------------

def test_a_payload_that_is_not_a_zip_replaces_nothing(tmp_path):
    zip_path = str(tmp_path / "tao.zip")
    assert freshness.stage_zip(_response(b"not a zip at all"), zip_path) is None
    assert not os.path.exists(zip_path)
    assert not os.path.exists(zip_path + ".new")


def test_a_good_download_is_swapped_in_and_recorded(tmp_path):
    zip_path = str(tmp_path / "tao.zip")
    first = _response(_zip_bytes("v1"), FIRST_HEADERS)
    staged = freshness.stage_zip(first, zip_path)
    assert staged is not None
    freshness.adopt_zip(first, staged, zip_path)
    assert os.path.exists(zip_path)
    meta = freshness.source_meta(zip_path)
    assert meta.get("etag") == '"aaa"'
    assert meta.get("size") == len(first.content)


# --- probe --------------------------------------------------------------------

def test_a_304_is_unchanged_and_the_kept_etag_is_the_condition(kept, monkeypatch):
    asked = []
    monkeypatch.setattr(freshness, "fetch", _answering(_response(status=304), asked))
    assert freshness.probe_source_freshness(DATA, kept) == "unchanged"
    assert asked[-1][1].get("If-None-Match") == '"aaa"'


def test_the_same_validators_are_unchanged_a_weak_etag_included(kept, monkeypatch):
    # some hosts answer 200 to a conditional request and leave the
    # comparing to the client
    monkeypatch.setattr(freshness, "fetch", _answering(_response(headers={
        "ETag": 'W/"aaa"', "Last-Modified": "Mon, 01 Sep 2026 17:33:00 GMT"})))
    assert freshness.probe_source_freshness(DATA, kept) == "unchanged"


def test_new_validators_are_changed_and_name_the_new_version(kept, monkeypatch):
    monkeypatch.setattr(freshness, "fetch", _answering(_response(headers={
        "ETag": '"bbb"', "Last-Modified": "Tue, 02 Sep 2026 19:30:00 GMT"})))
    probe = freshness.probe_source(DATA, kept)
    assert probe["result"] == "changed"
    assert probe["last_modified"] == "Tue, 02 Sep 2026 19:30:00 GMT"


def test_no_validators_is_unknown(kept, monkeypatch):
    monkeypatch.setattr(freshness, "fetch", _answering(_response(headers={})))
    assert freshness.probe_source_freshness(DATA, kept) == "unknown"


def test_a_dead_host_is_an_error(kept, monkeypatch):
    monkeypatch.setattr(freshness, "fetch", _dead)
    assert freshness.probe_source_freshness(DATA, kept) == "error"


def test_no_sidecar_is_changed(kept, monkeypatch):
    # the sidecar is disposable: without it one refresh rewrites it, and
    # nothing is asked of the host
    os.remove(freshness.source_meta_path(kept))
    monkeypatch.setattr(freshness, "fetch", _dead)
    assert freshness.probe_source_freshness(DATA, kept) == "changed"


def test_a_sidecar_holding_only_a_hash_is_unknown(kept, monkeypatch):
    # the host sent no validators last time: only the download can tell
    with open(freshness.source_meta_path(kept), "w", encoding="utf-8") as out:
        out.write('{"sha256": "abc"}')
    monkeypatch.setattr(freshness, "fetch", _dead)
    assert freshness.probe_source_freshness(DATA, kept) == "unknown"


def test_a_host_refusing_head_is_asked_with_a_conditional_get(kept, monkeypatch):
    asked = []
    monkeypatch.setattr(freshness, "fetch", _answering(
        lambda method: _response(status=405 if method == "head" else 304), asked))
    assert freshness.probe_source_freshness(DATA, kept) == "unchanged"
    assert [method for method, _ in asked] == ["head", "get"]
    assert asked[-1][1].get("If-None-Match") == '"aaa"'


# --- fetch --------------------------------------------------------------------

def test_a_new_download_moves_the_sidecar_with_it(kept, monkeypatch):
    monkeypatch.setattr(freshness, "fetch", _answering(
        _response(_zip_bytes("v2"), {"ETag": '"bbb"'})))
    assert freshness.fetch_if_new(DATA, kept) is True
    assert freshness.source_meta(kept).get("etag") == '"bbb"'


def test_a_download_that_is_no_zip_leaves_the_kept_zip_whole(kept, monkeypatch):
    # a moved url answering 200 with an error page
    before = Path(kept).read_bytes()
    monkeypatch.setattr(freshness, "fetch", _answering(_response(b"<html>404 but 200</html>")))
    assert freshness.fetch_if_new(DATA, kept) is None
    assert zipfile.is_zipfile(kept)
    assert Path(kept).read_bytes() == before


# --- key trio -----------------------------------------------------------------

@pytest.mark.parametrize("fields, stored", [
    ({}, {"api_key_location": "not_applicable"}),
    ({"api_key": "  ", "api_key_name": "x"}, {"api_key_location": "not_applicable"}),
    ({"api_key": " k1 ", "api_key_name": "", "api_key_location": "header"},
     {"api_key": "k1", "api_key_name": "api_key", "api_key_location": "header"}),
], ids=["no_key", "blank_key", "key_normalised"])
def test_the_key_trio_travels_together_behind_a_real_key(fields, stored):
    assert rt_source.static_key_fields(fields) == stored


# --- resolution ---------------------------------------------------------------

class _Entries:
    """Just enough of hass.config_entries for the resolution helpers."""

    def __init__(self, entries):
        self.entries = entries
        self.updated = []

    def async_entries(self, domain=None):
        return list(self.entries)

    def async_update_entry(self, entry, data=None, options=None):
        if data is not None:
            entry.data = dict(data)
        if options is not None:
            entry.options = dict(options)
        self.updated.append(entry.title)
        return True


def _entry(title, data, options=None):
    return types.SimpleNamespace(title=title, data=dict(data), options=dict(options or {}))


def _hass(*entries):
    return types.SimpleNamespace(config_entries=_Entries(list(entries)))


SOURCE = {"kind": "datasource", "file": "tao", "url": "https://host/tao.zip",
          "extract_from": "url"}
KEYLESS = {"file": "tao", "url": "https://old/tao.zip",
           "api_key_location": "not_applicable", "clean_feed_info": True}
KEYED = {"file": "tao", "url": "https://old/tao.zip", "api_key": "j-key",
         "api_key_name": "apikey", "api_key_location": "query_string"}
OWNING = {**SOURCE, "api_key": "s-key", "api_key_name": "token",
          "api_key_location": "header"}
DROPPED = {**SOURCE, "api_key_location": "not_applicable"}


def test_a_source_not_owning_its_key_reads_it_from_its_journeys():
    # the keyless journey comes first and must not strip the keyed one
    legacy = _entry("tao", SOURCE)
    hass = _hass(_entry("line 1", KEYLESS), legacy, _entry("line 2", KEYED))
    cfg = rt_source.static_feed_config(hass, legacy)
    assert cfg.get("api_key") == "j-key"
    assert cfg.get("api_key_name") == "apikey"
    assert cfg.get("url") == "https://host/tao.zip"
    data = source_refresh.refresh_data_for(hass, legacy)
    assert data.get("clean_feed_info") is True
    assert data.get("api_key") == "j-key"


def test_a_source_owning_its_key_speaks_for_itself():
    owning = _entry("tao", OWNING)
    hass = _hass(_entry("line 1", KEYLESS), owning, _entry("line 2", KEYED))
    cfg = rt_source.static_feed_config(hass, owning)
    assert cfg.get("api_key") == "s-key"
    assert cfg.get("api_key_location") == "header"


def test_a_source_that_dropped_its_key_resolves_none():
    # a stale copy on a journey entry stays silent
    dropped = _entry("tao", DROPPED)
    cfg = rt_source.static_feed_config(_hass(dropped, _entry("line 2", KEYED)), dropped)
    assert "api_key" not in cfg


# --- mirror -------------------------------------------------------------------

def test_the_mirror_writes_the_sources_url_and_key_on_every_journey():
    keyless, keyed = _entry("line 1", KEYLESS), _entry("line 2", KEYED)
    owning = _entry("tao", OWNING)
    asyncio.run(rt_source.async_mirror_rt_to_entries(_hass(keyless, owning, keyed), owning))
    assert [e.data.get("url") for e in (keyless, keyed)] == ["https://host/tao.zip"] * 2
    assert [e.data.get("api_key") for e in (keyless, keyed)] == ["s-key"] * 2
    assert keyless.data.get("api_key_location") == "header"


def test_the_mirror_takes_a_dropped_key_off_the_journeys():
    keyless, keyed = _entry("line 1", KEYLESS), _entry("line 2", KEYED)
    owning, dropped = _entry("tao", OWNING), _entry("tao", DROPPED)
    asyncio.run(rt_source.async_mirror_rt_to_entries(_hass(keyless, owning, keyed), owning))
    asyncio.run(rt_source.async_mirror_rt_to_entries(_hass(keyless, dropped, keyed), dropped))
    assert [e.data.get("api_key") for e in (keyless, keyed)] == [None, None]
    assert keyed.data.get("api_key_location") == "not_applicable"


def test_the_mirror_writes_nothing_when_nothing_changes():
    untouched = _entry("line 3", {"file": "tao", "url": "https://host/tao.zip",
                                  "api_key_location": "not_applicable"},
                       {"real_time": False})
    hass = _hass(untouched, _entry("tao", DROPPED))
    asyncio.run(rt_source.async_mirror_rt_to_entries(hass, hass.config_entries.entries[1]))
    assert hass.config_entries.updated == []
