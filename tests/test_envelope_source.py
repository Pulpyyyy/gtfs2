"""The network picked inside an envelope stays picked.

A source that is an envelope of zips (SEPTA's gtfs_public.zip holds
google_bus.zip and google_rail.zip) is built from the network the user
picked. That pick has to reach the datasource entry and come back at every
refresh: without it a refresh fetched the envelope, found no routes.txt
in it and kept the old data, for ever and in silence.
"""
from __future__ import annotations

import asyncio
import types
import zipfile
import zlib
from pathlib import Path

import ha_stub

rt_source = ha_stub.load("rt_source")
source_zip = ha_stub.load("source_zip")


def _hass(created):
    async def async_init(domain, context=None, data=None):
        created.append(data)

    return types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_entries=lambda domain=None: [],
        flow=types.SimpleNamespace(async_init=async_init)))


def test_the_entry_keeps_the_network_picked():
    created = []
    asyncio.run(rt_source.async_ensure_datasource_entry(
        _hass(created), "septa", url="https://h/gtfs_public.zip",
        extract_from="url", api={}, inner_zip="google_bus.zip"))
    assert created[0]["inner_zip"] == "google_bus.zip"


def test_a_plain_source_names_no_network():
    created = []
    asyncio.run(rt_source.async_ensure_datasource_entry(
        _hass(created), "tao", url="https://h/tao.zip", extract_from="url", api={}))
    assert "inner_zip" not in created[0]


def test_the_refresh_asks_for_that_network_again():
    entry = types.SimpleNamespace(data={
        "file": "septa", "url": "https://h/gtfs_public.zip", "extract_from": "url",
        "inner_zip": "google_bus.zip", "api_key_location": "not_applicable"})
    cfg = rt_source.static_feed_config(types.SimpleNamespace(), entry)
    assert cfg["inner_zip"] == "google_bus.zip"


def test_the_refresh_download_takes_the_member_out(tmp_path, monkeypatch):
    # a host that stopped answering ranges sends the whole envelope: the
    # download is staged with the member to take out of it
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    staged_with = []
    monkeypatch.setattr(source_zip, "routes_in", lambda path: {"R1"})
    monkeypatch.setattr(source_zip, "_open_source",
                        lambda data, url, headers: types.SimpleNamespace(raise_for_status=lambda: None))
    # a refresh never keeps an envelope: the source already named its network
    monkeypatch.setattr(source_zip, "stage_zip",
                        lambda response, path, inner=None, envelope_ok=False:
                        staged_with.append(inner) if not envelope_ok else None)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))
    got = source_zip.refresh_datasource(hass, "gtfs2", {
        "file": "septa", "url": "https://h/gtfs_public.zip", "extract_from": "url",
        "inner_zip": "google_bus.zip"})
    assert got is False  # stopped right after the staging, as stage_zip said
    assert staged_with == ["google_bus.zip"]


# --- the envelope's own edges ------------------------------------------------

zip_peek = ha_stub.load("zip_peek")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "boarding" / "static.zip"


def _envelope(path):
    with zipfile.ZipFile(path, "w") as zout:
        zout.writestr("google_bus.zip", FEED.read_bytes())
        zout.writestr("google_rail.zip", FEED.read_bytes())
    return path


class _Whole:
    """A host that ignores ranges: the whole envelope, answered 200."""
    status_code = 200
    headers = {}
    url = "https://h/gtfs_public.zip"

    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


def test_a_host_without_ranges_still_offers_the_networks(tmp_path, monkeypatch):
    body = _envelope(tmp_path / "envelope.zip").read_bytes()
    monkeypatch.setattr(source_zip, "inner_zips", lambda url, headers: [])
    monkeypatch.setattr(source_zip, "_open_source", lambda data, url, headers: _Whole(body))
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(tmp_path / p)))
    data = {"file": "septa", "url": "https://h/gtfs_public.zip", "extract_from": "url"}
    assert source_zip.ensure_source_zip(hass, "gtfs2", data) == "zip_holds_zips"
    assert data["inner_zips"] == ["google_bus.zip", "google_rail.zip"]


def test_a_member_cut_short_leaves_nothing(tmp_path, monkeypatch):
    envelope = _envelope(tmp_path / "envelope.zip")
    monkeypatch.setattr(zip_peek, "_MEMBER_MAX", 1000)
    staged = tmp_path / "septa.zip.new.inner"
    assert not zip_peek.extract_member(str(envelope), "google_bus.zip", str(staged))
    assert not staged.exists()


def test_a_member_is_inflated_a_chunk_at_a_time():
    payload = b"\0" * (8 * 1024 * 1024)
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = packer.compress(payload) + packer.flush()
    remote = types.SimpleNamespace(url="https://h/e.zip", headers={}, close=lambda: None,
                                   iter_content=lambda chunk_size: iter([packed]))
    member = zip_peek._MemberResponse(remote, len(packed), zip_peek._DEFLATED)
    sizes = [len(c) for c in member.iter_content(chunk_size=64 * 1024)]
    assert sum(sizes) == len(payload)
    assert max(sizes) <= 64 * 1024


def test_a_directory_too_large_is_not_asked_for(monkeypatch):
    # the end record announces a 100 MB directory: nothing more is fetched
    tail = b"x" * 10 + b"PK\x05\x06" + b"\0" * 8 + (100 * 1024 ** 2).to_bytes(4, "little") + b"\0" * 8
    asked = []

    def ranged(url, headers, span, stream=False):
        asked.append(span)
        return tail, types.SimpleNamespace()

    monkeypatch.setattr(zip_peek, "_ranged", ranged)
    members, _ = zip_peek._directory("https://h/e.zip", {})
    assert members == {}
    assert asked == [f"-{zip_peek._TAIL}"]
