"""Every way a refresh of a datasource ends, the current data kept or replaced.

refresh_datasource is what the update entity, the update service and the
scheduled check all end in. It either swaps a whole new database in or
leaves the current one exactly as it was: a database it cannot read, a
download that failed, a zip gone, an edition that only starts later, an
import that failed, a line a sensor reads gone from the new edition, a
swap refused. Each is played here on the boarding fixture, a one line
feed, with the database checked afterwards.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import types
import zipfile
from pathlib import Path

import requests

import ha_stub

source_zip = ha_stub.load("source_zip")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "boarding" / "static.zip"


def _hass(gtfs_dir):
    return types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(gtfs_dir)))


def _edition(path, **replace):
    """The fixture feed with some text of its tables replaced, written at path."""
    with zipfile.ZipFile(FEED) as zin, zipfile.ZipFile(path, "w") as zout:
        for name in zin.namelist():
            text = zin.read(name).decode()
            for old, new in replace.get(name.replace(".txt", ""), ()):
                text = text.replace(old, new)
            zout.writestr(name, text)
    return path


# a second line on the same trips' stops: B2 runs T9 as T1 does
TWO_LINES = {
    "routes": [("B1,AG,B1,Gare - Terminus,3\n",
                "B1,AG,B1,Gare - Terminus,3\nB2,AG,B2,Gare - Ecole,3\n")],
    "trips": [("B1,S,T1,Terminus,0\n", "B1,S,T1,Terminus,0\nB2,S,T9,Ecole,0\n")],
    "stop_times": [("T1,08:00:00,08:00:00,A,1,0,1\n",
                    "T1,08:00:00,08:00:00,A,1,0,1\nT9,10:00:00,10:00:00,A,1,0,0\n"
                    "T9,10:10:00,10:10:00,C,2,0,0\n")],
}


def _built(tmp_path, zip_source=FEED):
    """A source whose database was built from zip_source, the zip then kept."""
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    shutil.copy(zip_source, gtfs_dir / "src.zip")
    assert source_zip.refresh_datasource(
        _hass(gtfs_dir), "gtfs2", {"file": "src", "extract_from": "zip"})
    return gtfs_dir


def _routes(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in conn.execute("select distinct route_id from trips"))
    finally:
        conn.close()


def _refresh(gtfs_dir, **data):
    return source_zip.refresh_datasource(
        _hass(gtfs_dir), "gtfs2", {"file": "src", "extract_from": "zip", **data})


def _no_leftovers(gtfs_dir):
    return not list(gtfs_dir.glob("src.refresh*")) and not list(gtfs_dir.glob("*.new"))


# --- route by route ----------------------------------------------------------

def test_a_followed_line_is_refreshed_route_by_route(tmp_path):
    gtfs_dir = _built(tmp_path)
    got = _refresh(gtfs_dir)
    # route by route answers with the stop_times each line brought
    assert list(got) == ["B1"] and got["B1"] > 0
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]
    assert _no_leftovers(gtfs_dir)


def test_a_source_read_whole_takes_the_whole_new_edition(tmp_path):
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", **TWO_LINES)
    assert _refresh(gtfs_dir, whole_feed=True) == {"B1": None, "B2": None}
    assert _routes(gtfs_dir / "src.sqlite") == ["B1", "B2"]


def test_a_line_a_sensor_reads_gone_keeps_the_current_data(tmp_path):
    gtfs_dir = _built(tmp_path, _edition(tmp_path / "two.zip", **TWO_LINES))
    shutil.copy(FEED, gtfs_dir / "src.zip")
    data = {"read_routes": ["B2"]}
    assert _refresh(gtfs_dir, **data) is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1", "B2"]
    assert _no_leftovers(gtfs_dir)


def test_the_lines_missing_are_named_for_the_caller(tmp_path):
    gtfs_dir = _built(tmp_path, _edition(tmp_path / "two.zip", **TWO_LINES))
    shutil.copy(FEED, gtfs_dir / "src.zip")
    data = {"file": "src", "extract_from": "zip", "read_routes": ["B1", "B2"]}
    assert source_zip.refresh_datasource(_hass(gtfs_dir), "gtfs2", data) is False
    # only the line a sensor reads and the edition lost
    assert data["lines_missing"] == ["B2"]


def test_a_line_nobody_reads_just_goes(tmp_path):
    gtfs_dir = _built(tmp_path, _edition(tmp_path / "two.zip", **TWO_LINES))
    shutil.copy(FEED, gtfs_dir / "src.zip")
    got = _refresh(gtfs_dir, read_routes=["B1"])
    assert got["B1"] > 0 and got["B2"] == 0
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]


def test_a_deleted_database_gets_back_the_lines_its_sensors_read(tmp_path):
    # a source cut down to B1, its database deleted by hand: the lines it
    # followed went with it, the sensors still name theirs. Built whole,
    # a few lines of TAO or IDFM came back as the whole network
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", **TWO_LINES)
    (gtfs_dir / "src.sqlite").unlink()
    got = _refresh(gtfs_dir, read_routes=["B1"])
    assert list(got) == ["B1"] and got["B1"] > 0
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]
    assert _no_leftovers(gtfs_dir)


def test_a_deleted_database_read_whole_comes_back_whole(tmp_path):
    # a train or local stops sensor beside the line sensor reads every line
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", **TWO_LINES)
    (gtfs_dir / "src.sqlite").unlink()
    assert _refresh(gtfs_dir, read_routes=["B1"], whole_feed=True) == {"B1": None, "B2": None}
    assert _routes(gtfs_dir / "src.sqlite") == ["B1", "B2"]


def test_a_deleted_database_with_no_sensor_comes_back_whole(tmp_path):
    # nothing names a line: nothing says what the source followed
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", **TWO_LINES)
    (gtfs_dir / "src.sqlite").unlink()
    assert _refresh(gtfs_dir, read_routes=[]) == {"B1": None, "B2": None}


def test_a_deleted_database_whose_line_the_edition_lost_stays_unbuilt(tmp_path):
    # the one line the sensors read is not in the new edition: nothing to
    # build that any sensor could show, and the loss is named
    gtfs_dir = _built(tmp_path)
    (gtfs_dir / "src.sqlite").unlink()
    data = {"file": "src", "extract_from": "zip", "read_routes": ["B2"]}
    assert source_zip.refresh_datasource(_hass(gtfs_dir), "gtfs2", data) is False
    assert data["lines_missing"] == ["B2"]
    assert not (gtfs_dir / "src.sqlite").exists()
    assert _no_leftovers(gtfs_dir)


def test_every_line_gone_says_the_file_is_broken(tmp_path):
    # the edition renumbered its only line: nothing any sensor reads, but
    # a swap would leave the source with nothing at all
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", routes=[("B1,", "X1,")], trips=[("B1,", "X1,")])
    data = {"file": "src", "extract_from": "zip", "read_routes": []}
    assert source_zip.refresh_datasource(_hass(gtfs_dir), "gtfs2", data) is False
    assert data["lines_missing"] == ["B1"]
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]


def test_an_import_that_fails_keeps_the_current_data(tmp_path, monkeypatch):
    gtfs_dir = _built(tmp_path)
    monkeypatch.setattr(source_zip, "build_scratch_database", lambda *a, **k: False)
    assert _refresh(gtfs_dir) is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]
    assert _no_leftovers(gtfs_dir)


def test_a_swap_refused_keeps_the_current_data_and_no_leftover(tmp_path, monkeypatch):
    gtfs_dir = _built(tmp_path)
    before = (gtfs_dir / "src.sqlite").read_bytes()
    monkeypatch.setattr(source_zip, "swap_in", lambda new, real: False)
    assert _refresh(gtfs_dir) is False
    assert (gtfs_dir / "src.sqlite").read_bytes() == before
    assert _no_leftovers(gtfs_dir)


def test_a_staging_file_left_by_an_earlier_run_is_replaced(tmp_path):
    gtfs_dir = _built(tmp_path)
    (gtfs_dir / "src.refresh.sqlite").write_bytes(b"half of an interrupted refresh")
    assert list(_refresh(gtfs_dir)) == ["B1"]
    assert _no_leftovers(gtfs_dir)


# --- what stops a refresh before any import ------------------------------------

def test_a_database_that_does_not_answer_is_left_alone(tmp_path, monkeypatch):
    # no trips table to ask: unreadable, which is not "follows no line"
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    shutil.copy(FEED, gtfs_dir / "src.zip")
    conn = sqlite3.connect(gtfs_dir / "src.sqlite")
    conn.execute("create table marker (x)")
    conn.commit()
    conn.close()
    asked = []
    monkeypatch.setattr(source_zip, "_open_source", lambda *a: asked.append(a))
    assert _refresh(gtfs_dir, extract_from="url", url="https://h/src.zip") is False
    # not even downloaded: the host is not asked for a feed nothing can take
    assert asked == []
    assert "marker" in {r[0] for r in sqlite3.connect(gtfs_dir / "src.sqlite").execute(
        "select name from sqlite_master")}


def test_no_zip_to_refresh_from(tmp_path):
    gtfs_dir = _built(tmp_path)
    (gtfs_dir / "src.zip").unlink()
    assert _refresh(gtfs_dir) is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]


def test_an_edition_that_only_starts_later_is_refused_when_asked(tmp_path):
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", calendar=[("20260101", "20990101"), ("20271231", "20991231")],
             calendar_dates=[("20261225", "20991225")])
    before = (gtfs_dir / "src.sqlite").read_bytes()
    assert _refresh(gtfs_dir, check_source_dates=True) is False
    assert (gtfs_dir / "src.sqlite").read_bytes() == before
    # the same edition goes in when the check is not asked for
    assert list(_refresh(gtfs_dir)) == ["B1"]


def test_an_edition_already_running_passes_the_dates_check(tmp_path):
    gtfs_dir = _built(tmp_path)
    assert list(_refresh(gtfs_dir, check_source_dates=True)) == ["B1"]


# --- the download --------------------------------------------------------------

def _download_fails(tmp_path, monkeypatch, caplog, error):
    gtfs_dir = _built(tmp_path)
    kept = (gtfs_dir / "src.zip").read_bytes()
    (gtfs_dir / "src.zip.new").write_bytes(b"the start of a download")

    def fail(data, url, headers):
        raise error

    monkeypatch.setattr(source_zip, "_open_source", fail)
    with caplog.at_level(logging.ERROR, logger=source_zip.__name__):
        got = _refresh(gtfs_dir, extract_from="url", url="https://h/src.zip")
    assert got is False
    # the kept zip is the only full record of the feed: it stays, and the
    # partial download goes
    assert (gtfs_dir / "src.zip").read_bytes() == kept
    assert not (gtfs_dir / "src.zip.new").exists()
    return [r for r in caplog.records if r.message.startswith("Could not download")]


def test_a_host_that_does_not_answer_says_so_in_one_line(tmp_path, monkeypatch, caplog):
    logged = _download_fails(tmp_path, monkeypatch, caplog,
                             requests.ConnectionError("no route to host"))
    assert len(logged) == 1 and logged[0].exc_info is None


def test_an_error_of_our_own_keeps_its_stack(tmp_path, monkeypatch, caplog):
    logged = _download_fails(tmp_path, monkeypatch, caplog, KeyError("url"))
    assert len(logged) == 1 and logged[0].exc_info is not None


def test_an_error_status_is_a_failed_download(tmp_path, monkeypatch):
    gtfs_dir = _built(tmp_path)

    def refused():
        raise requests.HTTPError("403 Forbidden")

    monkeypatch.setattr(source_zip, "_open_source", lambda data, url, headers:
                        types.SimpleNamespace(raise_for_status=refused))
    assert _refresh(gtfs_dir, extract_from="url", url="https://h/src.zip") is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]


# --- the whole build's own refusals --------------------------------------------

def _whole(gtfs_dir, data=None):
    return source_zip._refresh_whole_feed(str(gtfs_dir), "src", "src.zip",
                                          str(gtfs_dir / "src.zip"), data or {"file": "src"})


def test_an_edition_naming_no_line_is_not_built(tmp_path):
    gtfs_dir = _built(tmp_path)
    _edition(gtfs_dir / "src.zip", routes=[("B1,AG,B1,Gare - Terminus,3\n", "")])
    assert _whole(gtfs_dir) is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]


def test_a_whole_import_that_fails_is_not_swapped(tmp_path, monkeypatch):
    gtfs_dir = _built(tmp_path)
    monkeypatch.setattr(source_zip, "build_scratch_database", lambda *a, **k: False)
    assert _whole(gtfs_dir) is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"] and _no_leftovers(gtfs_dir)


def test_a_whole_build_with_no_trip_is_not_swapped(tmp_path, monkeypatch):
    gtfs_dir = _built(tmp_path)
    real_routes_in = source_zip.routes_in
    monkeypatch.setattr(source_zip, "routes_in", lambda path: (
        set() if path.endswith(".refresh.sqlite") else real_routes_in(path)))
    assert _whole(gtfs_dir) is False
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"] and _no_leftovers(gtfs_dir)


def test_a_whole_swap_refused_leaves_no_leftover(tmp_path, monkeypatch):
    gtfs_dir = _built(tmp_path)
    (gtfs_dir / "src.refresh.sqlite").write_bytes(b"half of an interrupted refresh")
    before = (gtfs_dir / "src.sqlite").read_bytes()
    monkeypatch.setattr(source_zip, "swap_in", lambda new, real: False)
    assert _whole(gtfs_dir) is False
    assert (gtfs_dir / "src.sqlite").read_bytes() == before
    assert _no_leftovers(gtfs_dir)


# --- what cannot be cleaned up -------------------------------------------------

def test_a_partial_download_that_cannot_go_is_left(tmp_path, monkeypatch):
    # the failure is still a plain False, not an error escaping the refresh
    gtfs_dir = _built(tmp_path)
    (gtfs_dir / "src.zip.new").mkdir()

    def fail(data, url, headers):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(source_zip, "_open_source", fail)
    assert _refresh(gtfs_dir, extract_from="url", url="https://h/src.zip") is False


def _stuck_journal(new, real):
    """A swap refused, with a journal beside the new file that will not go."""
    Path(new + "-journal").mkdir()
    return False


def test_a_leftover_that_will_not_go_is_said(tmp_path, monkeypatch, caplog):
    gtfs_dir = _built(tmp_path)
    monkeypatch.setattr(source_zip, "swap_in", _stuck_journal)
    with caplog.at_level(logging.WARNING, logger=source_zip.__name__):
        assert _refresh(gtfs_dir) is False
    assert any(r.message.startswith("Could not remove") for r in caplog.records)
    assert _routes(gtfs_dir / "src.sqlite") == ["B1"]


def test_a_whole_leftover_that_will_not_go_is_said(tmp_path, monkeypatch, caplog):
    gtfs_dir = _built(tmp_path)
    monkeypatch.setattr(source_zip, "swap_in", _stuck_journal)
    with caplog.at_level(logging.WARNING, logger=source_zip.__name__):
        assert _whole(gtfs_dir) is False
    assert any(r.message.startswith("Could not remove") for r in caplog.records)
