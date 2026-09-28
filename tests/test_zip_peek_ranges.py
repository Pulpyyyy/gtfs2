"""An envelope of zips read by byte ranges, against a host that serves them.

zip_peek reads the end of a remote zip, then its directory, then the one
member the source was built from (SEPTA's gtfs_public.zip holds
google_bus.zip and google_rail.zip): the automatic refresh of such a
source goes through open_member every time. The host here answers the
Range header from a real envelope, so what comes out is compared with the
member's own bytes, and each fallback (a host that ignores ranges, a
member gone, a zip64 directory, a way of storing this does not read)
answers "download it whole" instead.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

import pytest

import ha_stub

zip_peek = ha_stub.load("zip_peek")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "boarding" / "static.zip"
URL = "https://h/gtfs_public.zip"


def _envelope(compression=zipfile.ZIP_DEFLATED, names=("google_bus.zip", "google_rail.zip"),
              filler=0):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as zout:
        if filler:
            # a large member first, so the tail read is a small part of the file
            zout.writestr("readme.txt", os.urandom(filler), compress_type=zipfile.ZIP_STORED)
        for name in names:
            zout.writestr(name, FEED.read_bytes())
    return buffer.getvalue()


class Host:
    """Serves `body` by the Range header; `ranges` False answers 200 with
    the whole file, `extra` bytes more than asked are sent past a range."""

    def __init__(self, monkeypatch, body, ranges=True, extra=0):
        self.body = body
        self.ranges = ranges
        self.extra = extra
        self.asked = []
        self.read = 0
        self.closed = 0
        monkeypatch.setattr(zip_peek, "fetch", self.fetch)

    def fetch(self, method, url, headers=None, **_kw):
        span = headers["Range"]
        self.asked.append(span)
        if not self.ranges:
            return self._response(self.body, 200)
        first, last = re.fullmatch(r"bytes=(\d*)-(\d*)", span).groups()
        if not first:
            part = self.body[-int(last):]
        else:
            part = self.body[int(first):int(last) + 1 + self.extra]
        return self._response(part, 206)

    def _response(self, part, status):
        host = self

        class Response:
            status_code = status
            url = URL
            headers = {"ETag": '"e1"'}

            def raise_for_status(self):
                pass

            @property
            def content(self):
                host.read += len(part)
                return part

            def iter_content(self, chunk_size):
                for at in range(0, len(part), 7):
                    host.read += len(part[at:at + 7])
                    yield part[at:at + 7]

            def close(self):
                host.closed += 1

        return Response()


def _member_bytes(member):
    return b"".join(member.iter_content(chunk_size=4096))


def test_the_networks_are_listed_from_a_few_ranges(monkeypatch):
    body = _envelope(filler=1024 ** 2)
    host = Host(monkeypatch, body)
    assert zip_peek.inner_zips(URL, {}) == ["google_bus.zip", "google_rail.zip"]
    # the tail and the directory, nothing else
    assert len(host.asked) == 2
    assert host.read < len(body) / 10


@pytest.mark.parametrize("compression", [zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED])
def test_the_member_comes_out_as_it_was_packed(monkeypatch, compression):
    host = Host(monkeypatch, _envelope(compression))
    member = zip_peek.open_member(URL, {}, "google_rail.zip")
    assert member is not None
    assert _member_bytes(member) == FEED.read_bytes()
    # shaped like the envelope's response, for adopt_zip
    assert (member.url, member.headers, member.status_code) == (URL, {"ETag": '"e1"'}, 200)
    member.close()
    assert host.closed >= 3


def test_a_host_sending_more_than_asked_is_cut_at_the_member(monkeypatch):
    Host(monkeypatch, _envelope(zipfile.ZIP_STORED), extra=500)
    member = zip_peek.open_member(URL, {}, "google_bus.zip")
    assert _member_bytes(member) == FEED.read_bytes()


def test_a_host_that_ignores_ranges_is_not_read(monkeypatch):
    body = _envelope()
    host = Host(monkeypatch, body, ranges=False)
    assert zip_peek.inner_zips(URL, {}) == []
    assert zip_peek.open_member(URL, {}, "google_bus.zip") is None
    # the whole file was offered and left unread, its connection closed
    assert host.read == 0
    assert host.closed == 2


def test_a_member_gone_from_the_envelope_is_none(monkeypatch):
    Host(monkeypatch, _envelope(names=("google_bus.zip",)))
    assert zip_peek.open_member(URL, {}, "google_rail.zip") is None


def test_a_member_stored_another_way_is_none(monkeypatch):
    Host(monkeypatch, _envelope(zipfile.ZIP_BZIP2))
    assert zip_peek.open_member(URL, {}, "google_bus.zip") is None


def test_a_feed_is_not_an_envelope(monkeypatch):
    Host(monkeypatch, FEED.read_bytes())
    assert zip_peek.inner_zips(URL, {}) == []


def test_a_file_that_is_no_zip_lists_nothing(monkeypatch):
    Host(monkeypatch, b"<html>maintenance</html>" * 100)
    assert zip_peek._directory(URL, {})[0] == {}


def test_a_zip64_directory_is_left_to_the_whole_download(monkeypatch):
    body = bytearray(_envelope())
    end = body.rfind(b"PK\x05\x06")
    body[end + 16:end + 20] = b"\xff\xff\xff\xff"
    host = Host(monkeypatch, bytes(body))
    assert zip_peek._directory(URL, {})[0] == {}
    assert len(host.asked) == 1


def test_a_host_failing_mid_way_is_none(monkeypatch):
    def broken(*_args, **_kw):
        raise OSError("connection reset")

    monkeypatch.setattr(zip_peek, "fetch", broken)
    assert zip_peek.inner_zips(URL, {}) == []
    assert zip_peek.open_member(URL, {}, "google_bus.zip") is None


def test_an_envelope_downloaded_whole_is_thinned_to_the_member(tmp_path):
    staged = tmp_path / "septa.zip.new"
    staged.write_bytes(_envelope())
    assert zip_peek.member_out_of(str(staged), "google_rail.zip") == str(staged)
    assert staged.read_bytes() == FEED.read_bytes()
    assert not (tmp_path / "septa.zip.new.inner").exists()


def test_a_download_that_is_already_the_feed_goes_through(tmp_path):
    staged = tmp_path / "septa.zip.new"
    staged.write_bytes(FEED.read_bytes())
    assert zip_peek.member_out_of(str(staged), "google_rail.zip") == str(staged)
    assert staged.read_bytes() == FEED.read_bytes()


def test_a_member_that_cannot_be_taken_leaves_the_download(tmp_path, monkeypatch):
    staged = tmp_path / "septa.zip.new"
    staged.write_bytes(_envelope())
    monkeypatch.setattr(zip_peek, "_MEMBER_MAX", 1000)
    assert zip_peek.member_out_of(str(staged), "google_rail.zip") == str(staged)
    assert zipfile.ZipFile(staged).namelist() == ["google_bus.zip", "google_rail.zip"]


def test_a_file_that_is_no_zip_holds_no_network(tmp_path):
    path = tmp_path / "not.zip"
    path.write_bytes(b"nothing")
    assert zip_peek.inner_zips_in_file(str(path)) == []
