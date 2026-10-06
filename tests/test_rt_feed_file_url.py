"""A realtime feed on disk is read from its file:// url as a static one is.

The static sources read a file:// url through file_url.url_path, which
decodes it (file:///config/gtfs2/my%20feed.rt names "my feed.rt", and on
Windows file:///C:/... names C:\\...). The realtime feeds cut "file://" off
by hand: a url with a space, a host, or the form file_url writes was no
file, and the source had no realtime.
"""
from __future__ import annotations

import ha_stub

rt_feed = ha_stub.load("feed.rt_feed")
file_url = ha_stub.load("file_url")


def test_a_feed_named_by_its_file_url_is_read(tmp_path):
    feed = tmp_path / "my feed.rt"
    feed.write_bytes(b"feed bytes")
    assert rt_feed._feed_body(file_url.file_url(str(feed)), {}, "trip_data") == b"feed bytes"


def test_a_feed_gone_is_no_feed(tmp_path):
    gone = file_url.file_url(str(tmp_path / "gone.rt"))
    assert rt_feed._feed_body(gone, {}, "trip_data") is None
