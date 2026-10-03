"""An api key sent as HTTP Basic: typed raw, encoded by the integration.

Some hosts (the CTS of Strasbourg) take their key as the user of an HTTP
Basic login: Authorization: Basic base64("key:"). A key that already
holds its user:password is encoded as it is. The static zip and the
realtime feeds ask with the same header, the key's name plays no part,
and the encoded form is masked in the logs as the key itself is. The
other places a key goes are left as they were.
"""
from __future__ import annotations

import base64

import ha_stub

const = ha_stub.load("const")
rt_source = ha_stub.load("rt_source")
freshness = ha_stub.load("freshness")
key_mask = ha_stub.load("key_mask")
flow_source = ha_stub.load("flow_source")

KEY = "d00dfeed-0000-4000-8000-000000000001"
BASIC = {"api_key_location": "basic", "api_key": KEY, "api_key_name": "ignored"}


def _basic(text):
    return "Basic " + base64.b64encode(text.encode()).decode()


def test_a_raw_key_is_sent_as_the_user_of_a_basic_login():
    assert rt_source.rt_headers(BASIC) == {"Authorization": _basic(KEY + ":")}


def test_a_key_holding_its_password_is_encoded_as_it_is():
    headers = rt_source.rt_headers({**BASIC, "api_key": "user:secret"})
    assert headers == {"Authorization": _basic("user:secret")}


def test_the_protobuf_accept_header_rides_along():
    headers = rt_source.rt_headers({**BASIC, "accept": True})
    assert headers == {"Authorization": _basic(KEY + ":"), "Accept": "application/x-protobuf"}


def test_the_static_zip_asks_with_the_same_header_and_a_bare_url():
    url, headers = freshness.source_request({**BASIC, "url": "https://h/feed.zip"})
    assert url == "https://h/feed.zip"
    assert headers["Authorization"] == _basic(KEY + ":")
    assert headers["User-Agent"] == const.USER_AGENT


def test_the_encoded_key_is_masked_in_the_logs():
    key_mask.note_key(KEY)
    assert key_mask.KEY_MASK in key_mask.hide_keys(f"headers {_basic(KEY + ':')}")
    assert "ZDAwZGZlZWQ" not in key_mask.hide_keys(f"headers {_basic(KEY + ':')}")


def test_the_key_screen_offers_it_and_keeps_it():
    schema = flow_source._source_key_schema({"api_key_location": "basic", "api_key": KEY})
    location = next(m for m in schema if str(m) == "api_key_location")
    assert location.default() == "basic"
    assert "basic" in schema[location].config["options"]


def test_the_other_places_are_unchanged():
    assert rt_source.rt_headers({"api_key_location": "header", "api_key_name": "x-api-key",
                                 "api_key": KEY}) == {"x-api-key": KEY}
    assert rt_source.rt_headers({"api_key_location": "query_string", "api_key": KEY}) is None
    assert rt_source.with_query_key("https://h/f", BASIC) == "https://h/f"
