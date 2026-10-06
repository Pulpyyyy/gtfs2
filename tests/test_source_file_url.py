"""A source whose url is file:// is checked and fetched as a hosted one is.

No stand-in for fetch here: the requests go through the session fetch
opens, which answers file:// itself (file_url.FileAdapter). The promises:

    answer       a HEAD names the file's time and size, and carries no
                 body; a GET carries the bytes, streamed or not; the very
                 time of the file is answered 304, an earlier or a later one
                 is another file; a file that is not there is a 404, which
                 raise_for_status raises
    url          a path and its file:// url read back as one another
    download     the first download of a file:// source is staged, swapped
                 in and recorded as a hosted one's, the file's time as its
                 Last-Modified
    probe        unchanged while the file stays as recorded; changed once
                 it is written again; a file gone is an error, and a
                 download of it brings nothing and leaves nothing
    same bytes   a file written again with the same bytes reads as changed
                 to the probe, the download's hash finds it is not: the zip
                 is not replaced, and the next probe is unchanged
    itself       a source whose url is its own kept zip sees a new edition
                 dropped over it, even one keeping an older time, takes it
                 in, and settles: the copy's own time reads as changed
                 once, the hash says otherwise
"""
from __future__ import annotations

import os

import pytest
import requests

import feed_db
import ha_stub

file_url = ha_stub.load("file_url")
freshness = ha_stub.load("feed.freshness")
key_mask = ha_stub.load("key_mask")

# a moment well in the past, and a later one: the file's times are set to
# these, so the questions never ride on the clock of the test machine
EARLY = 1_788_000_000
LATER = EARLY + 3600


_zip_bytes = feed_db.marked_zip


# built once: a zip records when each member was written, and two
# builds a second apart differ in it
V1, V2 = _zip_bytes("v1"), _zip_bytes("v2")


def _write(path, content, when):
    with open(path, "wb") as out:
        out.write(content)
    os.utime(path, (when, when))


@pytest.fixture
def feed(tmp_path):
    """A feed published on this machine, outside the gtfs2 folder."""
    folder = tmp_path / "share"
    folder.mkdir()
    path = str(folder / "tao.zip")
    _write(path, V1, EARLY)
    return path


@pytest.fixture
def source(tmp_path, feed):
    """(data, kept zip) of a source fetched from that feed once."""
    data = {"file": "tao", "url": file_url.file_url(feed)}
    zip_path = str(tmp_path / "tao.zip")
    assert freshness.fetch_if_new(data, zip_path) is True
    return data, zip_path


# --- answer -------------------------------------------------------------------

def test_a_head_names_the_file_and_carries_no_body(feed):
    response = key_mask.fetch("head", file_url.file_url(feed))
    assert response.status_code == 200
    assert response.headers["Content-Length"] == str(os.path.getsize(feed))
    assert response.headers["Last-Modified"] == "Sat, 29 Aug 2026 10:40:00 GMT"
    assert not response.content


def test_a_get_carries_the_bytes_streamed_or_not(feed):
    url = file_url.file_url(feed)
    assert key_mask.fetch("get", url).content == V1
    streamed = key_mask.fetch("get", url, stream=True)
    try:
        assert b"".join(streamed.iter_content(chunk_size=7)) == V1
    finally:
        streamed.close()


def test_only_the_very_time_of_the_file_is_answered_304(feed):
    url = file_url.file_url(feed)

    def asked(since):
        return key_mask.fetch("head", url, headers={"If-Modified-Since": since}).status_code

    assert asked("Sat, 29 Aug 2026 10:40:00 GMT") == 304
    assert asked("Sat, 29 Aug 2026 10:39:59 GMT") == 200
    assert asked("Sat, 29 Aug 2026 10:40:01 GMT") == 200
    assert asked("not a date") == 200


def test_a_file_that_is_not_there_is_a_404(tmp_path):
    response = key_mask.fetch("get", file_url.file_url(str(tmp_path / "gone.zip")))
    assert response.status_code == 404
    with pytest.raises(requests.HTTPError):
        response.raise_for_status()


# --- url ----------------------------------------------------------------------

def test_a_path_and_its_url_read_back_as_one_another(feed):
    url = file_url.file_url(feed)
    assert url.startswith(file_url.FILE_SCHEME)
    assert os.path.samefile(file_url.url_path(url), feed)


# --- download -----------------------------------------------------------------

def test_the_first_download_is_kept_and_recorded(source, feed):
    _data, zip_path = source
    with open(zip_path, "rb") as kept:
        assert kept.read() == V1
    meta = freshness.source_meta(zip_path)
    assert meta["last_modified"] == "Sat, 29 Aug 2026 10:40:00 GMT"
    assert meta["size"] == os.path.getsize(feed)
    assert meta["url"] == file_url.file_url(feed)


# --- probe --------------------------------------------------------------------

def test_the_probe_is_unchanged_while_the_file_stays(source):
    data, zip_path = source
    assert freshness.probe_source(data, zip_path)["result"] == "unchanged"


def test_the_probe_is_changed_once_the_file_is_written_again(source, feed):
    data, zip_path = source
    _write(feed, V2, LATER)
    probe = freshness.probe_source(data, zip_path)
    assert probe["result"] == "changed"
    assert probe["last_modified"] == "Sat, 29 Aug 2026 11:40:00 GMT"
    assert freshness.fetch_if_new(data, zip_path) is True
    with open(zip_path, "rb") as kept:
        assert kept.read() == V2


def test_a_file_gone_is_an_error_and_its_download_brings_nothing(source, feed):
    data, zip_path = source
    os.remove(feed)
    assert freshness.probe_source(data, zip_path)["result"] == "error"
    assert freshness.download_feed(data, zip_path) == (None, None)
    assert not os.path.exists(zip_path + ".new")
    with open(zip_path, "rb") as kept:
        assert kept.read() == V1


# --- same bytes ---------------------------------------------------------------

def test_the_same_bytes_written_again_are_not_taken_in(source, feed):
    data, zip_path = source
    _write(feed, V1, LATER)
    assert freshness.probe_source(data, zip_path)["result"] == "changed"
    assert freshness.fetch_if_new(data, zip_path) is False
    # the host's new time is kept, so the question is answered for free
    assert freshness.probe_source(data, zip_path)["result"] == "unchanged"


# --- itself -------------------------------------------------------------------

def test_a_source_fed_by_its_own_kept_zip_takes_a_new_edition_in(tmp_path):
    zip_path = str(tmp_path / "tao.zip")
    _write(zip_path, V1, EARLY)
    data = {"file": "tao", "url": file_url.file_url(zip_path)}
    assert freshness.fetch_if_new(data, zip_path) is True
    # the copy over itself has a time of its own: changed once, then the
    # hash finds the same bytes and the question settles
    assert freshness.fetch_if_new(data, zip_path) is False
    assert freshness.probe_source(data, zip_path)["result"] == "unchanged"
    # a new edition dropped over the kept zip, with the time it was
    # published at, older than the copy's
    _write(zip_path, V2, LATER)
    assert freshness.probe_source(data, zip_path)["result"] == "changed"
    assert freshness.fetch_if_new(data, zip_path) is True
    with open(zip_path, "rb") as kept:
        assert kept.read() == V2
