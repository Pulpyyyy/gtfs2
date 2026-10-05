"""The leg file: one per entry, and what it says of each listed trip.

The leg file is named after the departure's line and direction as well
as the entry: when those changed, the old file stayed until the entry was
removed, and a card still found it.

Its contents are read here on a small built database, the cases the
provider feeds do not hold: a loop, a trip run by frequency, a ride past
midnight from the other platform of a station, calls with no time.
"""
from __future__ import annotations

import datetime
import json
import os
import types
import zoneinfo
from pathlib import Path

from sqlalchemy import create_engine, text

import ha_stub

leg_mod = ha_stub.load("leg")

PARIS = zoneinfo.ZoneInfo("Europe/Paris")


def test_the_leg_of_an_earlier_line_goes(tmp_path):
    old = tmp_path / leg_mod.leg_geojson_name("R1", "0", "a")
    none = tmp_path / leg_mod.leg_geojson_name("R3", "None", "a")
    new = tmp_path / leg_mod.leg_geojson_name("R2", "1", "a")
    other = tmp_path / leg_mod.leg_geojson_name("R1", "0", "x leg a")
    # a name holding what reads as a direction: its file ends like a's
    forged = tmp_path / leg_mod.leg_geojson_name("R1", "0", "tram 1 leg a")
    for path in (old, none, new, other, forged):
        path.write_text("{}")
    leg_mod._drop_other_legs(str(tmp_path), "a", str(new))
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([new.name, other.name, forged.name])


# What the leg file says of each listed trip: its calls, timed on the
# service day the sensor lists the trip on, and the realtime the feed gives
# for each call. T1 is a loop, Gare, Centre, Lac and Gare again; F1 runs by
# frequency; the others ride from the second platform of a station (P1a,
# P1b) past midnight, from a stop the entry does not name, or with no time
# at their first call.
CALLS = [
    ("T1", "S1", 1, "08:00"), ("T1", "S2", 2, "08:10"), ("T1", "S3", 3, "08:20"),
    ("T1", "S1", 4, "08:30"),
    ("F1", "S1", 1, "07:00"), ("F1", "S2", 2, "07:10"),
    ("N1", "P1b", 1, "23:50"), ("N1", "S2", 2, "24:10"),
    ("X1", "X", 1, "06:00"), ("X1", "S2", 2, "06:30"),
    ("U1", "S1", 1, None), ("U1", "S2", 2, "09:10"),
    ("D1", "S1", 1, "10:00"), ("D1", "S2", 2, "10:10"), ("D1", "S3", 3, "10:20"),
    ("D1", "X", 4, "10:30"), ("D1", "P1", 5, "10:40"),
]


def _stored(clock):
    """A stop time as pygtfs stores it, days counted from 1970-01-01."""
    if clock is None:
        return None
    hours, minutes = map(int, clock.split(":"))
    return f"1970-01-{1 + hours // 24:02d} {hours % 24:02d}:{minutes:02d}:00.000000"


def _schedule(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'leg.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("create table agency (agency_id varchar, agency_timezone varchar)"))
        conn.execute(text("create table routes (route_id varchar, agency_id varchar)"))
        conn.execute(text("create table stops (stop_id varchar, stop_name varchar, stop_lat float, "
                          "stop_lon float, parent_station varchar)"))
        conn.execute(text("create table stop_times (trip_id varchar, stop_id varchar, "
                          "stop_sequence integer, arrival_time varchar, departure_time varchar, "
                          "pickup_type integer, drop_off_type integer)"))
        conn.execute(text("insert into agency values ('A', 'Europe/Paris')"))
        conn.execute(text("insert into routes values ('L1', 'A')"))
        for stop, name, parent in (("S1", "Gare", None), ("S2", "Centre", None), ("S3", "Lac", None),
                                   ("P1", "Pont", None), ("P1a", "Pont", "P1"),
                                   ("P1b", "Pont", "P1"), ("X", None, None)):
            conn.execute(text("insert into stops values (:s, :n, 43.0, 7.0, :p)"),
                         {"s": stop, "n": name, "p": parent})
        for trip, stop, seq, clock in CALLS:
            conn.execute(text("insert into stop_times values (:t, :s, :q, :a, :d, 0, 0)"),
                         {"t": trip, "s": stop, "q": seq, "a": _stored(clock), "d": _stored(clock)})
    return types.SimpleNamespace(engine=engine)


def _leg(tmp_path, departure, entities=None, name="leg"):
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: str(Path(tmp_path, *parts)), time_zone="Europe/Paris"))
    data = {"schedule": _schedule(tmp_path), "name": name, "route": "L1: x",
            "direction": "0", "next_departure": departure}
    leg_mod.write_leg_file(hass, data, entities)
    direction = str(departure.get("trip_direction_id", "0"))
    with open(tmp_path / "www" / "gtfs2" / leg_mod.leg_geojson_name("L1", direction, name),
              encoding="utf-8") as handle:
        return json.load(handle)


def _departure(trip_id, origin, leaves, listed=()):
    """The sensor's departure: the trip it rides first, then the listed
    ones, each with when it leaves the origin (None: no time listed)."""
    listed = [(trip_id, leaves), *listed]
    return {"trip_id": trip_id, "departure_time": leaves, "origin_stop_id": origin,
            "route_id": "L1", "trip_direction_id": "0",
            "next_departures_trip_id": [t for t, _ in listed],
            "next_departures": [w.isoformat() if hasattr(w, "isoformat") else w
                                for _, w in listed if w is not None]}


def _utc(day, clock):
    """A Paris clock on a September 2026 day, as the file writes it."""
    hours, minutes = map(int, clock.split(":"))
    return (datetime.datetime(2026, 9, day, tzinfo=PARIS)
            + datetime.timedelta(hours=hours, minutes=minutes)).astimezone(datetime.timezone.utc).isoformat()


def _epoch(day, clock):
    return int(datetime.datetime.fromisoformat(_utc(day, clock)).timestamp())


def _update(trip_id, *stops, start_time=None, relationship=None):
    trip = {"trip_id": trip_id}
    if start_time:
        trip["start_time"] = start_time
    if relationship:
        trip["schedule_relationship"] = relationship
    return {"id": trip_id, "trip_update": {"trip": trip, "stop_time_update": list(stops)}}


def test_a_loop_keeps_the_call_the_ride_makes(tmp_path):
    # boarding at Lac, the ride calls at Gare on its 4th call, not its 1st
    leg = _leg(tmp_path, _departure("T1", "S3", datetime.datetime(2026, 9, 25, 8, 20, tzinfo=PARIS)), [
        "not an entity",
        {"id": "v", "vehicle": {}},
        _update("OTHER", {"stop_id": "S2", "departure": {"delay": 30}}),
        _update("T1",
                {"stop_id": "S1", "stop_sequence": 1, "schedule_relationship": "SKIPPED"},
                # the first pass at Gare, which the ride does not make
                {"stop_id": "S1", "stop_sequence": 1, "departure": {"time": _epoch(25, "08:01")}},
                {"stop_id": "S1", "stop_sequence": 4, "departure": {"delay": 60}},
                # a time and no delay: the delay is the gap to the schedule
                {"stop_sequence": 2, "arrival": {"time": _epoch(25, "08:13")}},
                {"stop_id": "S3", "schedule_relationship": "NO_DATA"},
                {"stop_id": "Z", "departure": {"time": _epoch(25, "08:40")}},
                {"stop_id": "S2", "stop_sequence": 9}),
    ])
    stops = leg["trips"]["T1"]["stops"]
    assert list(stops) == ["S3", "S1", "S2"]
    assert (stops["S1"]["sequence"], stops["S1"]["scheduled"]) == (4, _utc(25, "08:30"))
    assert stops["S1"]["delay"] == 60 and "expected" not in stops["S1"] and "skipped" not in stops["S1"]
    assert "OTHER" not in leg["trips"]
    assert (stops["S2"]["expected"], stops["S2"]["delay"]) == (_utc(25, "08:13"), 180)
    assert stops["S3"]["no_data"] is True and "delay" not in stops["S3"]
    assert [f["properties"]["stop_sequence"] for f in leg["features"]] == [1, 2, 3, 4]
    assert [f["properties"]["scheduled"] for f in leg["features"]] == [
        _utc(25, "08:00"), _utc(25, "08:10"), _utc(25, "08:20"), _utc(25, "08:30")]
    assert leg["properties"]["realtime"] is True
    assert leg["properties"]["timezone"] == "Europe/Paris"


def test_a_frequency_trip_gets_a_run_per_trip_update(tmp_path):
    leg = _leg(tmp_path, _departure("F1", "S1", datetime.datetime(2026, 9, 25, 7, 0, tzinfo=PARIS)), [
        _update("F1", {"stop_id": "S2", "departure": {"delay": 60}}, start_time="07:00:00"),
        _update("F1", {"stop_id": "S1", "schedule_relationship": "SKIPPED"},
                {"stop_id": "S2", "departure": {"delay": 120}}, start_time="07:30:00"),
        _update("F1", relationship="CANCELED"),
    ])
    trips = leg["trips"]
    assert sorted(trips) == ["F1", "F1@07:00:00", "F1@07:30:00", "F1@2"]
    assert "delay" not in trips["F1"]["stops"]["S2"]
    assert (trips["F1@07:00:00"]["start_time"], trips["F1@07:00:00"]["stops"]["S2"]["delay"]) == ("07:00:00", 60)
    assert trips["F1@07:30:00"]["stops"]["S2"]["delay"] == 120
    assert trips["F1@07:30:00"]["stops"]["S1"]["skipped"] is True
    assert "skipped" not in trips["F1"]["stops"]["S1"]
    assert trips["F1@2"]["cancelled"] is True and "start_time" not in trips["F1@2"]
    assert trips["F1@07:00:00"]["stops"]["S2"]["scheduled"] == _utc(25, "07:10")


def test_the_service_day_is_found_from_the_listed_departure(tmp_path):
    leg = _leg(tmp_path, _departure(
        "N1", "P1a", datetime.datetime(2026, 9, 25, 23, 50, tzinfo=PARIS), listed=[
            # from a stop the entry does not name: its first call stands in
            ("X1", datetime.datetime(2026, 9, 26, 6, 0, tzinfo=PARIS)),
            ("U1", datetime.datetime(2026, 9, 26, 9, 0, tzinfo=PARIS)),
            ("T1", "not a time"),
            ("GONE", datetime.datetime(2026, 9, 26, 9, 0, tzinfo=PARIS)),
            ("F1", None),
        ]))
    trips = leg["trips"]
    # the other platform of the entry's station stands for the origin;
    # 24:10 lands on the next calendar day
    assert trips["N1"]["stops"]["S2"]["scheduled"] == _utc(26, "00:10")
    assert trips["N1"]["stops"]["P1b"]["scheduled"] == _utc(25, "23:50")
    assert trips["X1"]["stops"]["S2"]["scheduled"] == _utc(26, "06:30")
    # no time at the first call, an unreadable or missing listed time: the
    # calls stay, untimed
    assert trips["U1"]["stops"]["S2"]["scheduled"] is None
    assert trips["T1"]["stops"]["S1"]["scheduled"] is None
    assert trips["F1"]["stops"]["S2"]["scheduled"] is None
    assert "GONE" not in trips
    assert leg["properties"]["realtime"] is False
    assert [f["properties"]["stop_id"] for f in leg["features"]] == ["P1b", "S2"]


def test_every_stop_of_the_listed_trips_is_named_and_placed_once(tmp_path):
    # the runs listed call at stops of their own: each is drawn as ridden
    leg = _leg(tmp_path, _departure(
        "N1", "P1a", datetime.datetime(2026, 9, 25, 23, 50, tzinfo=PARIS), listed=[
            ("X1", datetime.datetime(2026, 9, 26, 6, 0, tzinfo=PARIS))]))
    called = {stop for trip in leg["trips"].values() for stop in trip["stops"]}
    assert set(leg["stops"]) == called == {"P1b", "S2", "X"}
    assert leg["stops"]["P1b"] == {"name": "Pont", "lat": 43.0, "lon": 7.0}
    # a stop the feed left unnamed reads by its id, as on the map points
    assert leg["stops"]["X"]["name"] == "X"


SHAPED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,Bus,http://a,Europe/Paris\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,43.0,7.0\nS2,Centre,43.1,7.1\nS3,Lac,43.2,7.2\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nL1,A,1,3\n",
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
    # the main road, a variant by the lake, and a run the feed draws not
    "trips.txt": ("route_id,service_id,trip_id,direction_id,shape_id\nL1,D,M1,0,MAIN\n"
                  "L1,D,V1,0,LAKE\nL1,D,N1,0,\n"),
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "M1,08:00:00,08:00:00,S1,1\nM1,08:10:00,08:10:00,S2,2\n"
                       "V1,09:00:00,09:00:00,S1,1\nV1,09:15:00,09:15:00,S3,2\nV1,09:20:00,09:20:00,S2,3\n"
                       "N1,10:00:00,10:00:00,S1,1\nN1,10:10:00,10:10:00,S2,2\n"),
    "shapes.txt": ("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                   "MAIN,43.1,7.1,2\nMAIN,43.0,7.0,1\nLAKE,43.0,7.0,1\nLAKE,43.2,7.2,2\nLAKE,43.1,7.1,3\n"
                   "OTHER,1.0,1.0,1\n"),
}


def test_each_run_is_drawn_on_its_own_road(tmp_path):
    import feed_db

    schedule = feed_db.build(tmp_path, SHAPED)
    try:
        hass = types.SimpleNamespace(config=types.SimpleNamespace(
            path=lambda *parts: str(Path(tmp_path, *parts)), time_zone="Europe/Paris"))
        departure = _departure("M1", "S1", datetime.datetime(2026, 9, 25, 8, 0, tzinfo=PARIS), listed=[
            ("V1", datetime.datetime(2026, 9, 25, 9, 0, tzinfo=PARIS)),
            ("N1", datetime.datetime(2026, 9, 25, 10, 0, tzinfo=PARIS))])
        leg_mod.write_leg_file(hass, {"schedule": schedule, "name": "leg", "route": "L1",
                                      "direction": "0", "next_departure": departure,
                                      "file": "feed", "gtfs_dir": str(tmp_path)})
        with open(tmp_path / "www" / "gtfs2" / leg_mod.leg_geojson_name("L1", "0", "leg"),
                  encoding="utf-8") as handle:
            leg = json.load(handle)
    finally:
        schedule.engine.dispose()
    trips = leg["trips"]
    assert (trips["M1"]["shape_id"], trips["V1"]["shape_id"]) == ("MAIN", "LAKE")
    # no shape named: the stops draw it, as before
    assert "shape_id" not in trips["N1"]
    # each shape once, in its own sequence, and only those the runs ride
    assert leg["shapes"] == {"MAIN": [[7.0, 43.0], [7.1, 43.1]],
                             "LAKE": [[7.0, 43.0], [7.2, 43.2], [7.1, 43.1]]}


def test_the_shapes_of_a_line_are_read_once_an_edition(tmp_path, monkeypatch):
    import feed_db

    geojson = ha_stub.load("geojson")
    schedule = feed_db.build(tmp_path, SHAPED)
    schedule.engine.dispose()
    zip_path = str(tmp_path / "feed.zip")
    passes = []
    read = geojson._trip_shape_ids
    monkeypatch.setattr(geojson, "_trip_shape_ids", lambda path, routes: passes.append(routes) or read(path, routes))
    for _ in range(3):
        shape_of, points = geojson.route_shapes(zip_path, {"L1"})
    assert len(passes) == 1 and shape_of == {"M1": "MAIN", "V1": "LAKE"} and set(points) == {"MAIN", "LAKE"}
    # kept beside the zip, a restart included, and not a source of its own
    assert (tmp_path / "feed.zip.shapes").exists()
    assert not geojson.SHAPES_SUFFIX.endswith((".sqlite", ".zip"))
    # a line the feed does not draw is read once too
    assert geojson.route_shapes(zip_path, {"NONE"}) == ({}, {})
    geojson.route_shapes(zip_path, {"NONE"})
    assert len(passes) == 2
    # a new edition of the zip is read again
    os.utime(zip_path, ns=(3, 3))
    assert geojson.route_shapes(zip_path, {"L1"})[0] == {"M1": "MAIN", "V1": "LAKE"}
    assert len(passes) == 3


def _carried(tmp_path, *updates):
    """{stop: delay} the leg file gives D1, ridden from S1, for its updates."""
    leg = _leg(tmp_path, _departure("D1", "S1", datetime.datetime(2026, 9, 25, 10, 0, tzinfo=PARIS)),
               [_update("D1", *updates)])
    return {stop: call.get("delay") for stop, call in leg["trips"]["D1"]["stops"].items()}


def test_a_delay_holds_for_the_rest_of_the_ride(tmp_path):
    # one update, at the next stop, by its sequence alone: the stops after
    # it are that late too, the one before it is not told
    got = _carried(tmp_path, {"stop_sequence": 2, "arrival": {"delay": 300}})
    assert got == {"S1": None, "S2": 300, "S3": 300, "X": 300, "P1": 300}


def test_a_later_update_takes_over(tmp_path):
    got = _carried(tmp_path, {"stop_sequence": 2, "arrival": {"delay": 300}},
                   {"stop_sequence": 4, "arrival": {"delay": 60}})
    assert got == {"S1": None, "S2": 300, "S3": 300, "X": 60, "P1": 60}


def test_a_skipped_stop_is_passed_over_and_no_data_ends_it(tmp_path):
    got = _carried(tmp_path, {"stop_sequence": 2, "arrival": {"delay": 300}},
                   {"stop_sequence": 3, "schedule_relationship": "SKIPPED"},
                   {"stop_sequence": 4, "schedule_relationship": "NO_DATA"})
    assert got == {"S1": None, "S2": 300, "S3": None, "X": None, "P1": None}


def test_no_departure_writes_an_empty_leg(tmp_path):
    leg = _leg(tmp_path, {})
    assert (leg["trips"], leg["features"], leg["properties"]["trip_id"]) == ({}, [], None)
    assert leg["properties"]["route_id"] == "L1"
