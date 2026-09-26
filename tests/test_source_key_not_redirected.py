"""A key sent in a header goes to its own host, not where a redirect points.

requests drops Authorization on a redirect to another host and nothing
else, so an x-api-key header followed the redirect to the CDN or bucket
serving the file. fetch's session drops every header the caller gave on
such a redirect, User-Agent and Accept apart, and keeps them on the same
host and on the upgrade of that host to https.
"""
from __future__ import annotations

import types

import requests

import ha_stub

key_mask = ha_stub.load("key_mask")

GIVEN = {"x-api-key": "SECRET", "User-Agent": "home-assistant-gtfs2",
         "Accept": "application/x-protobuf"}


def _redirect(origin, target):
    """The headers of the request a redirect from origin to target sends."""
    session = key_mask._KeyStaysHome(GIVEN)
    prepared = requests.Request("GET", target, headers=GIVEN).prepare()
    response = types.SimpleNamespace(request=types.SimpleNamespace(url=origin))
    session.rebuild_auth(prepared, response)
    return {name.lower(): value for name, value in prepared.headers.items()}


def test_other_host_loses_the_key():
    headers = _redirect("https://api.example.org/gtfs.zip",
                        "https://cdn.example.net/bucket/gtfs.zip")
    assert "x-api-key" not in headers
    assert headers["user-agent"] == "home-assistant-gtfs2"
    assert headers["accept"] == "application/x-protobuf"


def test_same_host_keeps_the_key():
    headers = _redirect("https://api.example.org/gtfs.zip",
                        "https://api.example.org/v2/gtfs.zip")
    assert headers["x-api-key"] == "SECRET"


def test_https_upgrade_keeps_the_key():
    headers = _redirect("http://api.example.org/gtfs.zip",
                        "https://api.example.org/gtfs.zip")
    assert headers["x-api-key"] == "SECRET"
