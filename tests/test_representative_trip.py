"""Which trip draws a line: get_representative_trip.

The route file (update_route_geojson) writes the stops of the trip this
picks, and a map card places the sensor's boarding and alighting stops on
it. A trip the sensor does not ride puts them where nothing it lists ever
stops. The cases are the ones the SNCF feed produced, cut down to a few
stops: substitution coaches filed under the train line, on stops of their
own that share the station's name and code, and both ways of a line filed
under one direction_id.

Each test builds a small SQLite database with pygtfs's table and column
names, and hands the function a schedule that only exposes `.engine`, which
is all it reads.
"""
from __future__ import annotations

import contextlib
import sqlite3

import pytest

import ha_stub

# Loaded on its own rather than through the package, whose __init__ pulls in
# the coordinator and the platforms, and with them the rest of Home Assistant.
gtfs_helper = ha_stub.load("gtfs_helper")

K8 = "FR:Line::1BF2D66F-09EF-4CB8-A003-1417C1EA6532:"
K5 = "FR:Line::13DADBDA-4FB1-4AA3-8DAB-60E24EF4AAFF:"

# the same three places, one stop per mode: Orleans shares its name and its
# UIC code between the two, only the prefix tells them apart
TRAIN_ORLEANS = "StopPoint:OCETrain TER-87543009"
TRAIN_AUBRAIS = "StopPoint:OCETrain TER-87543017"
TRAIN_PARIS = "StopPoint:OCETrain TER-87547000"
COACH_ORLEANS = "StopPoint:OCECar TER-87543009"
COACH_AUBRAIS = "StopPoint:OCECar TER-87737411"
COACH_PARIS = "StopPoint:OCECar TER-87737429"

STOPS = [
    (TRAIN_ORLEANS, "Orléans", "StopArea:OCE87543009"),
    (COACH_ORLEANS, "Orléans", "StopArea:OCE87543009"),
    (TRAIN_AUBRAIS, "Les Aubrais", "StopArea:OCE87543017"),
    (COACH_AUBRAIS, "Les Aubrais Gare Routière", "StopArea:OCE87737411"),
    (TRAIN_PARIS, "Paris Austerlitz", "StopArea:OCE87547000"),
    (COACH_PARIS, "Paris-Austerlitz Routiere", "StopArea:OCE87737429"),
]

SCHEMA = """
CREATE TABLE stops (feed_id INTEGER NOT NULL, stop_id VARCHAR NOT NULL,
    stop_name VARCHAR, parent_station VARCHAR, PRIMARY KEY (feed_id, stop_id));
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


@pytest.fixture
def schedule(tmp_path, monkeypatch):
    """Build a feed from (trip_id, route_id, direction_id, stop_ids) rows;
    a trip gets a shape unless its row carries shaped=False as a fifth item.

    A real SQLAlchemy engine where one is installed, sqlite3 otherwise.
    """
    engines = []

    def build(trips, name="gtfs.sqlite"):
        path = tmp_path / name
        db = sqlite3.connect(path)
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO stops VALUES (1, ?, ?, ?)", STOPS)
        for row in trips:
            trip_id, route_id, direction_id, calls = row[:4]
            shaped = row[4] if len(row) > 4 else True
            db.execute("INSERT INTO trips VALUES (1, ?, 'S', ?, ?, ?)",
                       (route_id, trip_id, direction_id, f"shape_{trip_id}" if shaped else None))
            db.executemany(
                "INSERT INTO stop_times VALUES (1, ?, '08:00:00', '08:00:00', ?, ?)",
                [(trip_id, stop_id, sequence) for sequence, stop_id in enumerate(calls, 1)])
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


def pick(schedule, route_id, direction, origin_id=None, destination_id=None):
    return gtfs_helper.get_representative_trip(
        schedule, route_id, direction, origin_id=origin_id, destination_id=destination_id)


def test_train_beats_coach_at_equal_stops(schedule):
    """K8+ both ways: trains and coaches have 3 stops each, and the coach
    trip_ids (OCESN425...) sort before the train ones (OCESN860...)."""
    feed = schedule([
        ("OCESN425R", K8, 1, [COACH_ORLEANS, COACH_AUBRAIS, COACH_PARIS]),
        ("OCESN860F1", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("OCESN860F2", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("OCESN425R0", K8, 0, [COACH_PARIS, COACH_AUBRAIS, COACH_ORLEANS]),
        ("OCESN860F0", K8, 0, [TRAIN_PARIS, TRAIN_AUBRAIS, TRAIN_ORLEANS]),
    ])
    assert pick(feed, K8, "1", TRAIN_ORLEANS, TRAIN_PARIS) in ("OCESN860F1", "OCESN860F2")
    assert pick(feed, K8, "0", TRAIN_PARIS, TRAIN_ORLEANS) == "OCESN860F0"


def test_coach_on_a_stop_of_the_same_name_and_code_is_left_out(schedule):
    """Orleans: OCETrain TER-87543009 and OCECar TER-87543009 are both named
    Orléans, under one station. The coach calls at one stop more and runs
    more often, so every later criterion would take it: only the sensor's
    own stop_id, compared whole, leaves it out."""
    feed = schedule([
        ("A_COACH_1", K8, 1, [COACH_ORLEANS, COACH_AUBRAIS, "StopPoint:OCECar TER-87000001", COACH_PARIS]),
        ("A_COACH_2", K8, 1, [COACH_ORLEANS, COACH_AUBRAIS, "StopPoint:OCECar TER-87000001", COACH_PARIS]),
        ("B_TRAIN", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
    ])
    assert pick(feed, K8, "1") == "A_COACH_1"
    assert pick(feed, K8, "1", TRAIN_ORLEANS, TRAIN_PARIS) == "B_TRAIN"
    # the boarding stop alone is enough to tell the modes apart
    assert pick(feed, K8, "1", TRAIN_ORLEANS) == "B_TRAIN"
    assert pick(feed, K8, "1", COACH_ORLEANS, COACH_PARIS) == "A_COACH_1"


def test_both_ways_under_one_direction_id(schedule):
    """K5+ direction 1 holds Nevers -> Paris and Paris -> Nevers, with the
    same number of stops. The way the sensor rides wins, even against the
    way more trips follow."""
    nevers, bourges, paris = "StopPoint:OCETrain TER-87696005", "StopPoint:OCETrain TER-87576207", TRAIN_PARIS
    feed = schedule([
        ("N1", K5, 1, [nevers, bourges, paris]),
        ("N2", K5, 1, [nevers, bourges, paris]),
        ("P1", K5, 1, [paris, bourges, nevers]),
        ("P2", K5, 1, [paris, bourges, nevers]),
        ("P3", K5, 1, [paris, bourges, nevers]),
    ])
    assert pick(feed, K5, "1", nevers, paris) == "N1"
    assert pick(feed, K5, "1", paris, nevers) == "P1"
    # the destination has to come after the origin, not just be on the trip
    assert pick(feed, K5, "1", bourges, nevers) == "P1"
    assert pick(feed, K5, "1", bourges, paris) == "N1"
    # without the sensor's stops, the way most trips follow
    assert pick(feed, K5, "1") == "P1"


def test_no_trip_serves_the_pair(schedule):
    """A station configured instead of a stop, or ids the provider renamed:
    nothing matches, and the line is still drawn, from every trip."""
    feed = schedule([
        ("SHORT", K8, 1, [TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("FULL", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
    ])
    assert pick(feed, K8, "1", "StopArea:OCE87543009", "StopArea:OCE87547000") == "FULL"
    # the right stops the wrong way round serve nothing either
    assert pick(feed, K8, "1", TRAIN_PARIS, TRAIN_ORLEANS) == "FULL"


def test_complete_tie_is_deterministic(schedule):
    """Same stops, same count, one trip each: the smallest trip_id, whatever
    order the rows were written in."""
    trips = [
        ("T2", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("T1", K8, 1, [TRAIN_ORLEANS, COACH_AUBRAIS, TRAIN_PARIS]),
        ("T3", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
    ]
    one_way = schedule(trips, "a.sqlite")
    other_way = schedule(list(reversed(trips)), "b.sqlite")
    # two sequences, one ridden twice: T2 and T3 follow it, T2 sorts first
    assert pick(one_way, K8, "1", TRAIN_ORLEANS, TRAIN_PARIS) == "T2"
    assert pick(other_way, K8, "1", TRAIN_ORLEANS, TRAIN_PARIS) == "T2"
    tied = [("T2", K8, 1, [TRAIN_ORLEANS, TRAIN_PARIS]), ("T1", K8, 1, [TRAIN_ORLEANS, TRAIN_PARIS])]
    assert pick(schedule(tied, "c.sqlite"), K8, "1") == "T1"
    assert pick(schedule(list(reversed(tied)), "d.sqlite"), K8, "1") == "T1"


def test_without_stop_ids(schedule):
    """No stop ids: the most stops, then the sequence most trips follow,
    then the smallest trip_id. The other direction never counts."""
    feed = schedule([
        ("A_SHORT", K8, 1, [TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("B_ONCE", K8, 1, [COACH_ORLEANS, COACH_AUBRAIS, COACH_PARIS]),
        ("C_TWICE", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("D_TWICE", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS]),
        ("0_OTHER_WAY", K8, 0, [TRAIN_PARIS, TRAIN_AUBRAIS, TRAIN_ORLEANS, COACH_ORLEANS]),
    ])
    assert pick(feed, K8, "1") == "C_TWICE"
    assert gtfs_helper.get_representative_trip(feed, K8, "1") == "C_TWICE"
    # an empty id is no id
    assert pick(feed, K8, "1", "", "") == "C_TWICE"
    assert pick(feed, K8, "0") == "0_OTHER_WAY"
    assert pick(feed, None, "1") is None
    assert pick(feed, "FR:Line::NONE:", "1") is None


def test_a_shape_counts_after_the_sensor_stops(schedule):
    """A trip with a shape is still preferred, as before, but not over the
    sensor's own stops."""
    feed = schedule([
        ("A_SHAPED", K8, 1, [COACH_ORLEANS, COACH_AUBRAIS, COACH_PARIS]),
        ("B_BARE", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS, "StopPoint:OCETrain TER-87000002"], False),
        ("C_BARE", K8, 1, [TRAIN_ORLEANS, TRAIN_AUBRAIS, TRAIN_PARIS], False),
    ])
    assert pick(feed, K8, "1") == "A_SHAPED"
    assert pick(feed, K8, "1", TRAIN_ORLEANS, TRAIN_PARIS) == "B_BARE"
