"""An api key carried in the query string: joined right, encoded, once.

with_query_key is the one place a key joins a url, for the static zip
and for the realtime feeds alike. A url that already asks something gets
the key after "&", and a key holding "+", "&" or "=" reaches the host as
typed.
"""
from __future__ import annotations

import ha_stub

rt_source = ha_stub.load("rt_source")
freshness = ha_stub.load("freshness")
source_zip = ha_stub.load("source_zip")
with_query_key = rt_source.with_query_key

IN_QUERY = {"api_key_location": "query_string", "api_key_name": "apikey"}


JOINS = [
    ("https://h/feed.zip", "abc123", "https://h/feed.zip?apikey=abc123"),
    ("https://h/feed?format=zip", "abc123", "https://h/feed?format=zip&apikey=abc123"),
    ("https://h/feed.zip", "a+b&c=d", "https://h/feed.zip?apikey=a%2Bb%26c%3Dd"),
]


def test_key_joins_the_url():
    for url, key, expected in JOINS:
        assert with_query_key(url, {**IN_QUERY, "api_key": key}) == expected


def test_no_key_or_key_elsewhere_leaves_the_url():
    assert with_query_key("https://h/f", {**IN_QUERY, "api_key": ""}) == "https://h/f"
    assert with_query_key("https://h/f", {"api_key_location": "header", "api_key": "abc123"}) == "https://h/f"
    assert with_query_key(None, {**IN_QUERY, "api_key": "abc123"}) is None


def test_missing_name_falls_back_on_the_default():
    assert with_query_key("https://h/f", {"api_key_location": "query_string",
                                          "api_key": "abc123"}) == "https://h/f?api_key=abc123"


def test_static_requests_use_it():
    data = {**IN_QUERY, "url": "https://h/feed?format=zip", "api_key": "a+b"}
    expected = "https://h/feed?format=zip&apikey=a%2Bb"
    assert source_zip._source_request(data)[0] == expected
    assert freshness._request_parts(data)[0] == expected
