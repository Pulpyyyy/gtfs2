"""A feed address is checked by its syntax, one rule wherever it is typed.

The source screens only asked that the address start with http://,
https:// or file://: "https://" alone, "http://host:abc" or an address
with a space in its host went through and failed at every download. The
check stays on the syntax: whether the address answers is the download's
to say.
"""
from __future__ import annotations

import pytest

import ha_stub

ha_stub.install()

sources = ha_stub.load("flow.sources")


@pytest.mark.parametrize("url", [
    "https://transport.data.gouv.fr/resources/80587/download",
    "http://192.168.1.239:8123/local/gtfs2/1_1.json",
    "https://gtfs.bus-tracker.fr/gtfs-rt/chartres/trip-updates",
    "file:///config/gtfs2/tao.zip",
    "file:///config/www/gtfs2/filibus%20tu.rt",
    "file:///C:/gtfs2/tao.zip",
    "file://localhost/config/gtfs2/tao.zip",
    "  https://h/feed.zip  ",
])
def test_a_feed_address_is_valid(url):
    assert sources.valid_feed_url(url)


@pytest.mark.parametrize("url", [
    "",
    "https://",
    "http:/h/feed.zip",
    "htps://h/feed.zip",
    "ftp://h/feed.zip",
    "h/feed.zip",
    "http://h:abc/feed.zip",
    "http://[::1/feed.zip",
    "https://exa mple.com/feed.zip",
    "file://",
])
def test_a_feed_address_is_refused(url):
    assert not sources.valid_feed_url(url)
