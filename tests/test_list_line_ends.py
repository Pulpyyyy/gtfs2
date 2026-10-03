"""A line's ends are read from the same trip whatever order the rows are in.

The label "A > B" comes from the line's longest trip. Two trips of the
same length, one each way, used to be picked by whichever SQLite met
first: the label turned round after a rebuild. Among the longest trips,
direction 0 first, the one whose ends come first by name is read, and
its ends stay in the order it rides them: a feed publishing each way as
a line of its own (Renfe's Alvia) tells the two lines apart by it.
"""
from __future__ import annotations

import feed_db
import ha_stub

line_ends = ha_stub.load("line_ends")

STOPS = {"A": "Gare", "B": "Centre", "C": "Lac"}


def _schedule(tmp_path, trips):
    """The trips [(trip_id, direction_id, stops)] of line R, imported by pygtfs."""
    return feed_db.build(tmp_path, feed_db.line_feed(
        STOPS, [(trip, "R", direction, calls) for trip, direction, calls in trips]))


def test_the_same_ends_whichever_trip_comes_first(tmp_path):
    back_first = _schedule(tmp_path, [("A1", 1, "CBA"), ("Z9", 0, "ABC")])
    assert line_ends._route_endpoints(back_first, ["R"]) == {"R": "Gare > Lac"}
    back_first.engine.dispose()


def test_the_same_ends_whatever_the_trip_ids(tmp_path):
    # a feed whose trip ids change with each export: the label does not,
    # whichever of the two ids sorts first
    for ids in (("X1", "A0"), ("A0", "X1")):
        folder = tmp_path / ids[0]
        folder.mkdir()
        one = _schedule(folder, [(ids[0], None, "CBA"), (ids[1], None, "ABC")])
        assert line_ends._route_endpoints(one, ["R"]) == {"R": "Gare > Lac"}
        one.engine.dispose()


def test_a_line_each_way_keeps_its_way(tmp_path):
    lines = feed_db.build(tmp_path, feed_db.line_feed(
        STOPS, [("T1", "R", 0, "ABC"), ("T2", "Q", 0, "CBA")]))
    assert line_ends._route_endpoints(lines, ["R", "Q"]) == {"R": "Gare > Lac", "Q": "Lac > Gare"}
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
    ends = line_ends._read_stop_ends(str(tmp_path / "src.zip"), {"LOOP", "R", "NONAME"})
    assert ends == {"R": "Lac > Gare"}
