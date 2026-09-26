"""fetch_if_new: the hash decides, and only a refreshing source adopts.

The download answers "is the feed new" by its sha256 against the kept
zip's. A new feed is swapped in for a source that refreshes itself; one
that only notifies keeps its zip, the edition its database was built
from, and hears the new file's sha256 instead.
"""
from __future__ import annotations

import io
import json
import logging
import types
import zipfile

import requests

import ha_stub

freshness = ha_stub.load("freshness")

TABLES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,Europe/Paris\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,0,0\n",
    "routes.txt": "route_id,route_type\nR1,3\n",
    "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\nT1,08:00:00,08:00:00,S1,1\n",
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nWK,1,1,1,1,1,0,0,20260101,20261231\n"),
}


def feed_bytes(edition):
    """The same edition gives the same bytes: every member carries one fixed
    date, where writestr stamps the current time and two calls a second
    apart made two different files."""
    buffer = io.BytesIO()
    members = {**TABLES, "feed_info.txt": f"feed_publisher_name,feed_version\nX,{edition}\n"}
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in members.items():
            zout.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), body)
    return buffer.getvalue()


def answering(body, **headers):
    def fetch(method, url, **kwargs):
        return types.SimpleNamespace(
            status_code=200, url=url, headers=headers, content=body,
            raise_for_status=lambda: None, close=lambda: None)
    return fetch


def kept_zip(tmp_path, edition):
    zip_path = tmp_path / "src.zip"
    zip_path.write_bytes(feed_bytes(edition))
    digest, size = freshness.file_digest(zip_path)
    (tmp_path / "src.zip.meta.json").write_text(json.dumps(
        {"sha256": digest, "size": size, "last_modified": "Mon, 01 Sep 2026 00:00:00 GMT"}))
    return str(zip_path)


DATA = {"url": "https://h/src.zip", "file": "src"}


def test_same_bytes_are_not_new(tmp_path, monkeypatch):
    zip_path = kept_zip(tmp_path, "1")
    monkeypatch.setattr(freshness, "fetch", answering(feed_bytes("1")))
    assert freshness.fetch_if_new(DATA, zip_path) is False
    assert not (tmp_path / "src.zip.new").exists()


def test_new_bytes_are_adopted_by_default(tmp_path, monkeypatch):
    zip_path = kept_zip(tmp_path, "1")
    monkeypatch.setattr(freshness, "fetch", answering(feed_bytes("2")))
    assert freshness.fetch_if_new(DATA, zip_path) is True
    assert (tmp_path / "src.zip").read_bytes() == feed_bytes("2")


def test_new_bytes_are_only_told_without_adopting(tmp_path, monkeypatch):
    zip_path = kept_zip(tmp_path, "1")
    meta_before = (tmp_path / "src.zip.meta.json").read_text()
    monkeypatch.setattr(freshness, "fetch", answering(feed_bytes("2")))
    answer = freshness.fetch_if_new(DATA, zip_path, adopt=False)
    new = tmp_path / "check.zip"
    new.write_bytes(feed_bytes("2"))
    assert answer == freshness.file_digest(new)[0]
    # the kept zip and its record stay the edition the database was built from
    assert (tmp_path / "src.zip").read_bytes() == feed_bytes("1")
    assert (tmp_path / "src.zip.meta.json").read_text() == meta_before
    assert not (tmp_path / "src.zip.new").exists()


def test_same_bytes_keep_the_validators_the_host_now_sends(tmp_path, monkeypatch):
    # a host stamping a fresh Last-Modified on unchanged bytes: the next
    # check asks with the new one, and hears "unchanged" for free
    zip_path = kept_zip(tmp_path, "1")
    monkeypatch.setattr(freshness, "fetch", answering(
        feed_bytes("1"), **{"Last-Modified": "Sun, 21 Sep 2026 03:00:00 GMT"}))
    assert freshness.fetch_if_new(DATA, zip_path) is False
    meta = json.loads((tmp_path / "src.zip.meta.json").read_text())
    assert meta["last_modified"] == "Sun, 21 Sep 2026 03:00:00 GMT"
    assert meta["sha256"] == freshness.file_digest(zip_path)[0]


def _errors(caplog):
    return [r for r in caplog.records if r.levelno == logging.ERROR]


def test_a_host_down_is_one_line_an_error_of_ours_keeps_its_stack(tmp_path, monkeypatch, caplog):
    # a check runs again and again while a host is down: one line each
    # time, where the stack deep in requests added thirty that said nothing
    zip_path = kept_zip(tmp_path, "1")

    def down(*_args, **_kwargs):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr(freshness, "fetch", down)
    with caplog.at_level(logging.ERROR):
        freshness.fetch_if_new(DATA, zip_path)
    [record] = _errors(caplog)
    assert record.exc_info is None and "no route to host" in record.getMessage()

    caplog.clear()

    def broken(*_args, **_kwargs):
        raise KeyError("a bug of ours")
    monkeypatch.setattr(freshness, "fetch", broken)
    with caplog.at_level(logging.ERROR):
        freshness.fetch_if_new(DATA, zip_path)
    [record] = _errors(caplog)
    assert record.exc_info is not None
    assert (tmp_path / "src.zip").read_bytes() == feed_bytes("1")
