"""Where the rides leave the order open, one branch at a time, the busiest first.

From a stop where the line splits, the destinations list the stops of one
branch together, then the other's: interleaving them by distance read as
no bus runs (Zou 653). The branch more trips ride comes first. Set here
with the answer written out; the provider sweep checks only that no two
branches are interleaved, whichever comes first.
"""
from __future__ import annotations

import io
import zipfile

import pygtfs
import pytest

import ha_stub

ha_stub.install()

places = ha_stub.load("places")

HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
STOPS = {"O": (45.0, 1.0), "X1": (45.1, 1.2), "X2": (45.2, 1.4), "Y1": (45.1, 0.8), "Y2": (45.2, 0.6)}


def _calls(trip, stops):
    return "".join(f"{trip},08:{n:02d}:00,08:{n:02d}:00,{s},{n}\n" for n, s in enumerate(stops, 1))


def _schedule(tmp_path, x_trips, y_trips):
    trips = [(f"X{n}", "OX") for n in range(x_trips)] + [(f"Y{n}", "OY") for n in range(y_trips)]
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},{s} stop,{lat},{lon}\n" for s, (lat, lon) in STOPS.items()),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,S,{t},0\n" for t, _way in trips),
        "stop_times.txt": HEAD + "".join(
            _calls(t, ["O", "X1", "X2"] if way == "OX" else ["O", "Y1", "Y2"]) for t, way in trips),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in feed.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    return schedule


def test_a_side_is_finished_before_the_other_way_starts(tmp_path):
    # O -> A -> B -> C -> D (three trips), O -> A -> B -> Q (one: the other
    # quay of the terminus), O -> X -> Y -> Z the other way (three trips).
    # Q hangs off B, placed already: it closes its side before X starts,
    # rather than after the whole other way (TAO 40, Chèques Postaux quai C)
    stops = {"O": (45.0, 1.0), "A": (45.1, 1.1), "B": (45.2, 1.2), "C": (45.3, 1.3),
             "D": (45.4, 1.4), "Q": (45.2, 1.25), "X": (44.9, 0.9), "Y": (44.8, 0.8),
             "Z": (44.7, 0.7)}
    rides = [("S1", "OABCD"), ("S2", "OABCD"), ("S3", "OABCD"), ("Q1", "OABQ"),
             ("W1", "OXYZ"), ("W2", "OXYZ"), ("W3", "OXYZ")]
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},{s} stop,{lat},{lon}\n" for s, (lat, lon) in stops.items()),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,S,{t},0\n" for t, _ride in rides),
        "stop_times.txt": HEAD + "".join(_calls(t, list(ride)) for t, ride in rides),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in feed.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    try:
        found = places.get_destination_stop_list(schedule, "R", None, "O")
        assert [str(s).split(":")[0] for s in found] == ["A", "B", "C", "D", "Q", "X", "Y", "Z"]
    finally:
        schedule.engine.dispose()


def test_a_branch_waiting_for_another_is_finished_through_it(tmp_path):
    # O -> P -> T -> U (three trips), O -> C -> D -> T -> U (one), and
    # O -> X -> Y (two), a branch of its own. After P, T waits for D: the
    # branch that joins P's at T comes next, then the other, rather than
    # the busier X cutting in between P and T (Rome 404 from Fabriano)
    stops = {"O": (45.0, 1.0), "P": (45.1, 1.1), "C": (45.1, 1.3), "D": (45.2, 1.3),
             "T": (45.3, 1.2), "U": (45.4, 1.2), "X": (44.9, 0.9), "Y": (44.8, 0.8)}
    rides = [("P1", "OPTU"), ("P2", "OPTU"), ("P3", "OPTU"), ("C1", "OCDTU"),
             ("X1", "OXY"), ("X2", "OXY")]
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},{s} stop,{lat},{lon}\n" for s, (lat, lon) in stops.items()),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,S,{t},0\n" for t, _ride in rides),
        "stop_times.txt": HEAD + "".join(_calls(t, list(ride)) for t, ride in rides),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in feed.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    try:
        found = places.get_destination_stop_list(schedule, "R", None, "O")
        assert [str(s).split(":")[0] for s in found] == ["P", "C", "D", "T", "U", "X", "Y"]
    finally:
        schedule.engine.dispose()


def test_the_branch_that_joins_comes_before_one_off_a_place_listed(tmp_path):
    # O -> A -> B -> T -> U (three trips), O -> A -> C (one), and O -> E ->
    # F -> T -> U (one). After B, T waits for F: E, which goes on to T,
    # comes before C, though C hangs off A, a place listed already; C
    # listed first cut B's side in two (GtfsDe 22884 from Zingster Str.,
    # Zagreb 14 from Zapruđe)
    stops = {"O": (45.0, 1.0), "A": (45.1, 1.1), "B": (45.2, 1.2), "T": (45.3, 1.3),
             "U": (45.4, 1.4), "C": (45.2, 1.0), "E": (45.0, 1.2), "F": (45.1, 1.3)}
    rides = [("B1", "OABTU"), ("B2", "OABTU"), ("B3", "OABTU"), ("C1", "OAC"), ("E1", "OEFTU")]
    feed = {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{s},{s} stop,{lat},{lon}\n" for s, (lat, lon) in stops.items()),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"R,S,{t},0\n" for t, _ride in rides),
        "stop_times.txt": HEAD + "".join(_calls(t, list(ride)) for t, ride in rides),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in feed.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    try:
        found = places.get_destination_stop_list(schedule, "R", None, "O")
        assert [str(s).split(":")[0] for s in found] == ["A", "B", "E", "F", "T", "U", "C"]
    finally:
        schedule.engine.dispose()


@pytest.mark.parametrize(("x_trips", "y_trips", "listed"), [
    (3, 1, ["X1", "X2", "Y1", "Y2"]),
    (1, 3, ["Y1", "Y2", "X1", "X2"]),
])
def test_one_branch_at_a_time_the_busiest_first(tmp_path, x_trips, y_trips, listed):
    schedule = _schedule(tmp_path, x_trips, y_trips)
    try:
        found = places.get_destination_stop_list(schedule, "R", None, "O")
        assert [str(s).split(":")[0] for s in found] == listed
    finally:
        schedule.engine.dispose()
