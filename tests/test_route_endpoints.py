"""A line's ends are read from the same trip whatever order the rows are in.

The label "A > B" comes from the line's longest trip. Two trips of the
same length, one each way, used to be picked by whichever SQLite met
first: the label turned round after a rebuild. Among the longest trips,
direction 0 first, the one whose ends come first by name is read, and
its ends stay in the order it rides them: a feed publishing each way as
a line of its own (Renfe's Alvia) tells the two lines apart by it.
"""
from __future__ import annotations

import types

from sqlalchemy import create_engine, text

import ha_stub

route_names = ha_stub.load("route_names")

STOPS = {"A": "Gare", "B": "Centre", "C": "Lac"}


def _schedule(tmp_path, trips):
    engine = create_engine(f"sqlite:///{tmp_path / 'ends.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar)"))
        conn.execute(text("create table trips (trip_id varchar, route_id varchar, direction_id integer)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, stop_sequence integer)"))
        for stop_id, name in STOPS.items():
            conn.execute(text("insert into stops values (:s, :n)"), {"s": stop_id, "n": name})
        for trip_id, direction, calls in trips:
            conn.execute(text("insert into trips values (:t, 'R', :d)"), {"t": trip_id, "d": direction})
            for seq, stop_id in enumerate(calls, 1):
                conn.execute(text("insert into stop_times values (:t, :s, :q)"),
                             {"t": trip_id, "s": stop_id, "q": seq})
    return types.SimpleNamespace(engine=engine)


def test_the_same_ends_whichever_trip_comes_first(tmp_path):
    back_first = _schedule(tmp_path, [("A1", 1, "CBA"), ("Z9", 0, "ABC")])
    assert route_names._route_endpoints(back_first, ["R"]) == {"R": "Gare > Lac"}
    back_first.engine.dispose()


def test_the_same_ends_whatever_the_trip_ids(tmp_path):
    # a feed whose trip ids change with each export: the label does not,
    # whichever of the two ids sorts first
    for ids in (("X1", "A0"), ("A0", "X1")):
        folder = tmp_path / ids[0]
        folder.mkdir()
        one = _schedule(folder, [(ids[0], None, "CBA"), (ids[1], None, "ABC")])
        assert route_names._route_endpoints(one, ["R"]) == {"R": "Gare > Lac"}
        one.engine.dispose()


def test_a_line_each_way_keeps_its_way(tmp_path):
    lines = _schedule(tmp_path, [("T1", 0, "ABC")])
    with lines.engine.begin() as conn:
        conn.execute(text("insert into trips values ('T2', 'Q', 0)"))
        for seq, stop in enumerate("CBA", 1):
            conn.execute(text("insert into stop_times values ('T2', :s, :q)"), {"s": stop, "q": seq})
    assert route_names._route_endpoints(lines, ["R", "Q"]) == {"R": "Gare > Lac", "Q": "Lac > Gare"}
    lines.engine.dispose()


def test_the_zip_ends_follow_the_same_rules(tmp_path):
    import zipfile
    with zipfile.ZipFile(tmp_path / "src.zip", "w") as zout:
        zout.writestr("stops.txt", "stop_id,stop_name\nS1,Lac\nS2,Gare\nS3,Stade\nS4,\n")
        zout.writestr("trips.txt", "route_id,trip_id\nLOOP,L1\nR,B1\nNONAME,N1\n")
        zout.writestr("stop_times.txt", "trip_id,stop_id,stop_sequence\n"
                      "L1,S1,1\nL1,S3,2\nL1,S1,3\n"          # a loop: Lac > Lac
                      "B1,S1,1\nB1,S2,2\n"                  # Lac then Gare
                      "N1,S3,1\nN1,S4,2\n")                 # ends on a stop with no name
    ends = route_names._read_stop_ends(str(tmp_path / "src.zip"), {"LOOP", "R", "NONAME"})
    assert ends == {"R": "Lac > Gare"}
