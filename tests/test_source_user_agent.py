"""One User-Agent for every request the integration makes.

The static zip and its checks named the client home-assistant-gtfs2, the
realtime feeds GTFS2-HomeAssistant/1.0 with upstream's address. A host
reading its logs saw two clients, and the address led to a project that
does not send this traffic. Both now send const.USER_AGENT, naming this
repository.
"""
from __future__ import annotations

import ha_stub

const = ha_stub.load("const")
freshness = ha_stub.load("feed.freshness")
rt_feed = ha_stub.load("feed.rt_feed")


def test_the_static_zip_and_the_realtime_feeds_send_the_same_one():
    _url, headers = freshness.source_request({"url": "https://h/feed.zip"})
    assert headers["User-Agent"] == const.USER_AGENT
    assert rt_feed._with_user_agent(None)["User-Agent"] == const.USER_AGENT


def test_it_names_the_client_and_where_it_comes_from():
    assert const.USER_AGENT.startswith("GTFS2-HomeAssistant/")
    assert "https://github.com/Pulpyyyy/gtfs2" in const.USER_AGENT


def test_a_header_the_caller_gives_still_wins():
    assert rt_feed._with_user_agent({"User-Agent": "mine"})["User-Agent"] == "mine"
