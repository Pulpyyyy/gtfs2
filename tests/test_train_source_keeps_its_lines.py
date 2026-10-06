"""A source a train sensor reads keeps the rail lines of its code, not the network.

A train sensor holds to the lines it was made for, by their codes (K8+).
Its source used to be refreshed whole, every line of the network, and
never pruned: the first refresh after the flow imported two lines brought
the national feed back. Now the refresh takes, from the new edition, the
rail lines wearing those codes, a route_id the edition is new to
included, and refuses the swap when a code has no line, or no trip, left.
A train sensor holding to no code still reads the source whole.
"""
from __future__ import annotations

import asyncio
import types
import zipfile

import feed_db
import ha_stub

shrink = ha_stub.load("shrink")
source_entries = ha_stub.load("feed.source_entries")
source_zip = ha_stub.load("source_zip")

HEAD = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nS,SNCF,http://s,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nSO,Orleans,47.9,1.9\nSP,Paris,48.8,2.3\n",
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260101,20271231\n"),
}


def _edition(path, lines):
    """A feed of one trip a line, lines [(route_id, short name, route_type)],
    written at path."""
    tables = dict(HEAD)
    tables["routes.txt"] = "route_id,agency_id,route_short_name,route_long_name,route_type\n" + "".join(
        f"{route},S,{code},{code},{kind}\n" for route, code, kind in lines)
    tables["trips.txt"] = "route_id,service_id,trip_id\n" + "".join(
        f"{route},D,T{route}\n" for route, _code, _kind in lines)
    tables["stop_times.txt"] = feed_db.STOP_TIMES + "".join(
        feed_db.calls(f"T{route}", ["SO", "SP"]) for route, _code, _kind in lines)
    with zipfile.ZipFile(path, "w") as zout:
        for name, body in tables.items():
            zout.writestr(name, body)


# the network: two rail lines and a bus; K8+ under one route_id
FIRST = [("RK", "K8+", 2), ("RP", "P8", 2), ("B", "8", 3)]
# the next edition files K8+ under a second route_id too
NEXT = [("RK", "K8+", 2), ("RK2", "K8+", 2), ("RP", "P8", 2), ("B", "8", 3)]


def _refresh(gtfs_dir, lines, **data):
    _edition(gtfs_dir / "src.zip", lines)
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(gtfs_dir)))
    data = {"file": "src", "extract_from": "zip", "read_routes": [], "whole_feed": False, **data}
    return source_zip.refresh_datasource(hass, "gtfs2", data), data


def _routes(gtfs_dir):
    return {r[0] for r in feed_db.rows(gtfs_dir / "src.sqlite", "select distinct route_id from trips")}


def _source(tmp_path, read_routes=()):
    gtfs_dir = tmp_path / "gtfs2"
    gtfs_dir.mkdir()
    got, _data = _refresh(gtfs_dir, FIRST, train_lines=["K8+"], read_routes=list(read_routes))
    assert set(got) == {"RK", *read_routes}
    return gtfs_dir


def test_a_train_source_takes_its_code_from_each_edition(tmp_path):
    gtfs_dir = _source(tmp_path)
    assert _routes(gtfs_dir) == {"RK"}
    got, data = _refresh(gtfs_dir, NEXT, train_lines=["K8+"])
    assert set(got) == {"RK", "RK2"}
    assert _routes(gtfs_dir) == {"RK", "RK2"}
    assert "lines_missing" not in data


def test_a_code_the_edition_dropped_keeps_the_data(tmp_path):
    gtfs_dir = _source(tmp_path)
    got, data = _refresh(gtfs_dir, [("RP", "P8", 2), ("B", "8", 3)], train_lines=["K8+"])
    assert got is False
    assert data["lines_missing"] == ["K8+"]
    assert _routes(gtfs_dir) == {"RK"}


def test_a_code_whose_lines_lost_their_trips_keeps_the_data(tmp_path):
    # a bus line followed too: every line gone would be a broken file
    gtfs_dir = _source(tmp_path, read_routes=["B"])
    _edition(gtfs_dir / "src.zip", NEXT)
    # K8+ still listed, its trips gone
    edition = gtfs_dir / "next.zip"
    with zipfile.ZipFile(gtfs_dir / "src.zip") as zin, zipfile.ZipFile(edition, "w") as zout:
        for name in zin.namelist():
            text = zin.read(name).decode()
            if name in ("trips.txt", "stop_times.txt"):
                text = "".join(line for line in text.splitlines(keepends=True) if "TRK" not in line)
            zout.writestr(name, text)
    edition.replace(gtfs_dir / "src.zip")
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: str(gtfs_dir)))
    data = {"file": "src", "extract_from": "zip", "read_routes": ["B"], "whole_feed": False,
            "train_lines": ["K8+"]}
    assert source_zip.refresh_datasource(hass, "gtfs2", data) is False
    assert data["lines_missing"] == ["K8+"]
    assert _routes(gtfs_dir) == {"RK", "B"}


def test_a_source_also_read_whole_still_checks_the_code(tmp_path):
    gtfs_dir = _source(tmp_path)
    got, data = _refresh(gtfs_dir, [("RP", "P8", 2), ("B", "8", 3)], train_lines=["K8+"],
                         whole_feed=True)
    assert got is False and data["lines_missing"] == ["K8+"]


def _entry(entry_id, **data):
    return types.SimpleNamespace(entry_id=entry_id, data={"file": "src", **data}, options={})


def _hass(gtfs_dir, *entries):
    async def executor(fn, *args):
        return fn(*args)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda p: str(gtfs_dir)),
        config_entries=types.SimpleNamespace(async_entries=lambda domain: list(entries)),
        async_add_executor_job=executor)


def test_who_reads_a_source_whole():
    with_code = _entry("t1", route="train", line="K8+", lines=["K8+"])
    with_two = _entry("t2", route="train", lines=["K8+", "P8"])
    no_code = _entry("t3", route="train", lines=[])
    bus = _entry("b1", route="B: 8")
    hass = _hass(None, with_code, with_two, bus)
    assert source_entries.source_readers(hass, "src") == ({"B"}, False)
    assert source_entries.source_train_lines(hass, "src") == {"K8+", "P8"}
    assert source_entries.source_train_lines(hass, "src", exclude="t2") == {"K8+"}
    # a train sensor of no code rides every rail line: the source stays whole
    hass = _hass(None, with_code, no_code)
    assert source_entries.source_readers(hass, "src") == (set(), True)


def test_a_prune_keeps_the_lines_of_the_code(tmp_path):
    gtfs_dir = _source(tmp_path)
    _refresh(gtfs_dir, NEXT, train_lines=["K8+"])
    db = str(gtfs_dir / "src.sqlite")
    assert shrink.routes_of_lines(db, {"K8+"}) == {"RK", "RK2"}
    # a bus line of the same number is no rail line of that code
    assert shrink.routes_of_lines(db, {"8"}) == set()
    assert shrink.routes_of_lines(db, set()) == set()
    hass = _hass(gtfs_dir, _entry("t1", route="train", lines=["K8+"]))
    assert asyncio.run(shrink.async_train_routes(hass, str(gtfs_dir), "src")) == {"RK", "RK2"}
    assert asyncio.run(shrink.async_train_routes(hass, str(gtfs_dir), "src", exclude="t1")) == set()
