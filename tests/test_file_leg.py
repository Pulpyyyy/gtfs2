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
import types
import zoneinfo
from pathlib import Path

from sqlalchemy import create_engine, text

import ha_stub

geojson = ha_stub.load("geojson")

PARIS = zoneinfo.ZoneInfo("Europe/Paris")


def test_the_leg_of_an_earlier_line_goes(tmp_path):
    old = tmp_path / geojson.leg_geojson_name("R1", "0", "a")
    none = tmp_path / geojson.leg_geojson_name("R3", "None", "a")
    new = tmp_path / geojson.leg_geojson_name("R2", "1", "a")
    other = tmp_path / geojson.leg_geojson_name("R1", "0", "x leg a")
    # a name holding what reads as a direction: its file ends like a's
    forged = tmp_path / geojson.leg_geojson_name("R1", "0", "tram 1 leg a")
    for path in (old, none, new, other, forged):
        path.write_text("{}")
    geojson._drop_other_legs(str(tmp_path), "a", str(new))
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
    geojson.write_leg_file(hass, data, entities)
    direction = str(departure.get("trip_direction_id", "0"))
    with open(tmp_path / "www" / "gtfs2" / geojson.leg_geojson_name("L1", direction, name),
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


def test_no_departure_writes_an_empty_leg(tmp_path):
    leg = _leg(tmp_path, {})
    assert (leg["trips"], leg["features"], leg["properties"]["trip_id"]) == ({}, [], None)
    assert leg["properties"]["route_id"] == "L1"
