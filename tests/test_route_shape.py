"""The polyline of the line drawn on a map: read_shape, and the LineString
the route file carries when the zip beside the database still holds
shapes.txt.

shapes.txt is never imported (see gtfs_shape), so the route file reads the
shape of the trip it draws straight out of the zip, and a map card draws
the street or the track between the stops instead of a straight line. The
cases are the feeds' own: TAO ships a shapes.txt, its trips all name a
shape; the SNCF ships none; the historic import strips it out of the zip in
place; and a trip may carry no shape_id at all.

Each test builds a small SQLite database with pygtfs's table and column
names, a zip with or without shapes.txt, and hands update_route_geojson a
coordinator that only exposes what it reads: _data, _route_id, _direction
and hass.config.path.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import types
import zipfile

import pytest

import ha_stub

# Loaded on its own rather than through the package, whose __init__ pulls in
# the coordinator and the platforms, and with them the rest of Home Assistant.
gtfs_helper = ha_stub.load("gtfs_helper")
gtfs_shape = ha_stub.load("gtfs_shape")

ROUTE = "ORLEANS:Line:A"
SHAPE = "VER2-LAM2-BUS2-HOP1"
# three stops of the tram A of Orleans, and a shape that runs past them:
# listed out of sequence, the way a feed is allowed to
STOPS = [
    ("VER1", "Jules Verne", 47.9270, 1.9214),
    ("LAM1", "Lamballe", 47.9280, 1.9170),
    ("BUS1", "Bustière", 47.9300, 1.9120),
]
SHAPE_ROWS = [
    (SHAPE, "47.928364", "1.917259", "2", "0.371"),
    (SHAPE, "47.928112", "1.922139", "0", "0.0"),
    (SHAPE, "47.928146", "1.917786", "1", "0.325"),
    (SHAPE, "47.930100", "1.912000", "3", "0.900"),
    ("OTHER", "47.0", "1.0", "0", "0.0"),
]
SHAPE_POINTS = [[1.922139, 47.928112], [1.917786, 47.928146], [1.917259, 47.928364], [1.912, 47.9301]]

SCHEMA = """
CREATE TABLE stops (feed_id INTEGER NOT NULL, stop_id VARCHAR NOT NULL,
    stop_name VARCHAR, stop_lat FLOAT, stop_lon FLOAT, parent_station VARCHAR,
    PRIMARY KEY (feed_id, stop_id));
CREATE TABLE trips (feed_id INTEGER NOT NULL, route_id VARCHAR,
    service_id VARCHAR, trip_id VARCHAR NOT NULL, direction_id INTEGER,
    shape_id VARCHAR, PRIMARY KEY (feed_id, trip_id));
CREATE TABLE stop_times (feed_id INTEGER NOT NULL, trip_id VARCHAR NOT NULL,
    arrival_time DATETIME, departure_time DATETIME, stop_id VARCHAR NOT NULL,
    stop_sequence INTEGER NOT NULL,
    PRIMARY KEY (feed_id, trip_id, stop_id, stop_sequence));
"""


class _Sqlite3Engine:
    """What the function asks of `schedule.engine`, answered by sqlite3.

    For the CI venv, which has no SQLAlchemy: ha_stub stands in for its
    text(), and the test swaps that for str, so the same SQL reaches sqlite3.
    """

    def __init__(self, path) -> None:
        self._path = path

    @contextlib.contextmanager
    def connect(self):
        conn = sqlite3.connect(self._path)
        try:
            yield _Sqlite3Connection(conn)
        finally:
            conn.close()

    def dispose(self) -> None:
        pass


class _Sqlite3Connection:
    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, statement, params=None):
        return self._conn.execute(str(statement), params or {})


class _Schedule:
    def __init__(self, engine) -> None:
        self.engine = engine


def write_zip(path, shapes=SHAPE_ROWS, columns=("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence", "shape_dist_traveled")):
    """A feed zip holding the tables the function never reads, plus
    shapes.txt unless shapes is None, as the historic import leaves it."""
    with zipfile.ZipFile(path, "w") as zout:
        zout.writestr("routes.txt", "route_id,route_short_name\n" + ROUTE + ",A\n")
        if shapes is not None:
            # the BOM some editors write ahead of the header
            lines = ["﻿" + ",".join(columns)]
            lines += [",".join(row[:len(columns)]) for row in shapes]
            zout.writestr("shapes.txt", "\n".join(lines) + "\n")


@pytest.fixture
def schedule(tmp_path, monkeypatch):
    """Build a feed from one trip calling at STOPS, with or without a shape.

    A real SQLAlchemy engine where one is installed, sqlite3 otherwise.
    """
    engines = []

    def build(trip_id="T1", shape_id=SHAPE, name="gtfs.sqlite"):
        path = tmp_path / name
        db = sqlite3.connect(path)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO stops VALUES (1, ?, ?, ?, ?, NULL)", STOPS)
        db.execute("INSERT INTO trips VALUES (1, ?, 'S', ?, 1, ?)", (ROUTE, trip_id, shape_id))
        db.executemany(
            "INSERT INTO stop_times VALUES (1, ?, '08:00:00', '08:00:00', ?, ?)",
            [(trip_id, stop_id, sequence) for sequence, (stop_id, *_) in enumerate(STOPS, 1)])
        db.commit()
        db.close()
        try:
            from sqlalchemy import create_engine
        except ImportError:
            monkeypatch.setattr(gtfs_helper, "text", str)
            engine = _Sqlite3Engine(path)
        else:
            engine = create_engine(f"sqlite:///{path}")
        engines.append(engine)
        return _Schedule(engine)

    yield build
    for engine in engines:
        engine.dispose()


def coordinator(tmp_path, schedule, file="feed"):
    """What update_route_geojson reads of the coordinator, and a hass whose
    config directory is tmp_path: the zip lives in gtfs2/, the file goes to
    www/gtfs2/."""
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: str(tmp_path.joinpath(*parts))))
    return types.SimpleNamespace(
        hass=hass,
        _data={"schedule": schedule, "gtfs_dir": "gtfs2", "file": file, "next_departure": {}},
        _route_id=ROUTE, _direction="1")


def route_file(tmp_path):
    path = tmp_path / "www" / "gtfs2" / gtfs_helper.route_geojson_name(ROUTE, "1")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --- read_shape ---------------------------------------------------------------

def test_shape_points_come_in_sequence_order_as_lon_lat(tmp_path):
    write_zip(tmp_path / "feed.zip")
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", SHAPE) == SHAPE_POINTS


def test_shape_without_dist_traveled_reads_the_same(tmp_path):
    write_zip(tmp_path / "feed.zip", columns=("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"))
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", SHAPE) == SHAPE_POINTS


def test_shape_columns_may_come_in_any_order(tmp_path):
    rows = [(seq, lon, sid, lat) for sid, lat, lon, seq, _ in SHAPE_ROWS]
    write_zip(tmp_path / "feed.zip", shapes=rows, columns=("shape_pt_sequence", "shape_pt_lon", "shape_id", "shape_pt_lat"))
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", SHAPE) == SHAPE_POINTS


def test_a_bad_row_does_not_lose_the_shape(tmp_path):
    write_zip(tmp_path / "feed.zip", shapes=SHAPE_ROWS + [(SHAPE, "not-a-number", "1.0", "9", ""), (SHAPE, "47.0")])
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", SHAPE) == SHAPE_POINTS


def test_unknown_shape_reads_none(tmp_path):
    write_zip(tmp_path / "feed.zip")
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", "NOPE") is None
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", None) is None
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", "") is None


def test_zip_without_shapes_reads_none(tmp_path):
    # the SNCF ships none, and the historic import strips it out in place
    write_zip(tmp_path / "feed.zip", shapes=None)
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", SHAPE) is None


def test_missing_zip_reads_none(tmp_path):
    assert gtfs_shape.read_shape(tmp_path / "gone.zip", SHAPE) is None
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert gtfs_shape.read_shape(tmp_path / "junk.zip", SHAPE) is None


def test_shapes_without_the_required_columns_read_none(tmp_path):
    write_zip(tmp_path / "feed.zip", columns=("shape_id", "shape_pt_lat"))
    assert gtfs_shape.read_shape(tmp_path / "feed.zip", SHAPE) is None


# --- the route file -----------------------------------------------------------

def test_route_file_carries_the_polyline_ahead_of_the_stops(tmp_path, schedule):
    (tmp_path / "gtfs2").mkdir()
    write_zip(tmp_path / "gtfs2" / "feed.zip")
    gtfs_helper.update_route_geojson(coordinator(tmp_path, schedule()), trip_id="T1")
    written = route_file(tmp_path)
    line, *stops = written["features"]
    assert line["geometry"] == {"type": "LineString", "coordinates": SHAPE_POINTS}
    assert line["properties"] == {"id": ROUTE + "_1_shape", "title": ROUTE + "_shape",
                                  "trip_id": "T1", "shape_id": SHAPE}
    assert written["properties"]["shape_id"] == SHAPE
    # the stops are still there, in riding order, untouched
    assert [s["geometry"]["type"] for s in stops] == ["Point"] * 3
    assert [s["properties"]["stop_id"] for s in stops] == ["VER1", "LAM1", "BUS1"]
    assert stops[0]["geometry"]["coordinates"] == [1.9214, 47.9270]


def test_route_file_keeps_to_the_stops_when_the_zip_has_no_shapes(tmp_path, schedule):
    (tmp_path / "gtfs2").mkdir()
    write_zip(tmp_path / "gtfs2" / "feed.zip", shapes=None)
    gtfs_helper.update_route_geojson(coordinator(tmp_path, schedule()), trip_id="T1")
    written = route_file(tmp_path)
    assert [f["geometry"]["type"] for f in written["features"]] == ["Point"] * 3
    assert written["properties"]["shape_id"] is None


def test_route_file_keeps_to_the_stops_when_the_trip_names_no_shape(tmp_path, schedule):
    (tmp_path / "gtfs2").mkdir()
    write_zip(tmp_path / "gtfs2" / "feed.zip")
    gtfs_helper.update_route_geojson(coordinator(tmp_path, schedule(shape_id=None)), trip_id="T1")
    written = route_file(tmp_path)
    assert [f["geometry"]["type"] for f in written["features"]] == ["Point"] * 3
    assert written["properties"]["shape_id"] is None


def test_route_file_keeps_to_the_stops_when_the_zip_is_gone(tmp_path, schedule):
    # a zip removed by hand: the line is still drawn, from its stops
    gtfs_helper.update_route_geojson(coordinator(tmp_path, schedule()), trip_id="T1")
    written = route_file(tmp_path)
    assert [f["geometry"]["type"] for f in written["features"]] == ["Point"] * 3
    assert written["properties"]["shape_id"] is None


def test_shape_named_by_no_trip_stop_draws_the_stops_alone(tmp_path, schedule):
    # trips.shape_id points at a shape the zip does not carry
    (tmp_path / "gtfs2").mkdir()
    write_zip(tmp_path / "gtfs2" / "feed.zip")
    gtfs_helper.update_route_geojson(coordinator(tmp_path, schedule(shape_id="GONE")), trip_id="T1")
    written = route_file(tmp_path)
    assert [f["geometry"]["type"] for f in written["features"]] == ["Point"] * 3
    assert written["properties"]["shape_id"] is None
