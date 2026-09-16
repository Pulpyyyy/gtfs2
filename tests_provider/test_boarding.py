"""Where the feed says the rider cannot get on, or off, nothing is offered.

stop_times flags every call: pickup_type and drop_off_type read 0 for a
regular stop, 1 for no way on / off, 2 and 3 for a phone call ahead or a
word to the driver. Real feeds use the 1 mid-route, not only at the ends of
a trip: a night train sets down only at its morning stops, a coach takes
nobody on on its way into town. The promises, on a made-up line under
fixtures/boarding whose calls carry every shape of the rule, and on the
SNCF fixture's night train (Paris to Latour-de-Carol, set-down only from
Auterive on, passing Toulouse without a stop):

    stop_list      the origin list holds the places some trip takes riders
                   on at, and no other; the terminus, where nobody gets on,
                   is not offered as a departure
    destinations   from an origin, the places some trip through it sets
                   riders down at afterwards, and no other; a 2 or a 3 is a
                   way on or off, only the 1 is none
    pairs          get_next_departure answers a pair when some trip boards
                   at the origin and alights at the destination, and answers
                   nothing when the feed forbids either end; so do
                   has_trip_between and get_next_service_date where the
                   tree has them
    local_stop     a stop nobody can get on at lists no departure
    files          the route file says, for every call, how the trip makes
                   it, so a card chaining legs picks its ends among the
                   calls the rider can make; the leg file too where the
                   tree writes one. The route file also says, per stop,
                   whether ANY trip of the line takes riders on or sets
                   them down there (boards / alights): the drawn trip's own
                   1 is not the line's word
    line_flags     a stop the drawn trip does not board at, that another
                   trip of the line does, reads boards true: a card that
                   filtered its lists on the drawn trip would shut out a
                   journey that works
    stations       the train path: the departures hold to the same rule,
                   by name; so do the station list, the arrival list and the
                   pair test where the tree has them

A reader this tree has not is recorded as not checked here, so the same
promises read on a tree that has it.

    pytest tests_provider/test_boarding.py
"""
from __future__ import annotations

import datetime
import json
import types
import zipfile
import zoneinfo
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy.sql import text

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

import fixture_db  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")
try:
    geojson = ha_stub.load("geojson")
except FileNotFoundError:  # a tree without the fork's map files
    geojson = None

FIXTURES = Path(__file__).parent / "fixtures"
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
UTC = datetime.timezone.utc
ROUTE = "B1"
# the SNCF fixture's night train: one trip, Paris Austerlitz to
# Latour-de-Carol, set-down only from Auterive on, through Toulouse
NIGHT_ROUTE = "OCESN-87547000-87611483"
NIGHT_DAY = "2026-08-27"


@pytest.fixture(scope="module")
def bus():
    return fixture_db.build(str(FIXTURES / "boarding"))


@pytest.fixture(scope="module")
def sncf():
    return fixture_db.build(str(FIXTURES / "sncf"))


@pytest.fixture(autouse=True)
def paris_clock():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))


def _hass(root=".", where=None):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(Path(root, *parts)),
                                     time_zone="Europe/Paris"),
        states=types.SimpleNamespace(get=lambda _entity: types.SimpleNamespace(
            attributes={"latitude": where[0], "longitude": where[1]} if where else {})))


def _ids(entries):
    return [entry.split(": ", 1)[0] for entry in entries]


def _entry(schedule, stop_id):
    """The picker's value for a stop: "id: Name (sequence)"."""
    with schedule.engine.connect() as conn:
        name = conn.execute(text("SELECT stop_name FROM stops WHERE stop_id = :s"),
                            {"s": stop_id}).scalar()
    return f"{stop_id}: {name} (1)"


def _bus_data(schedule, origin, destination):
    return {"schedule": schedule, "gtfs_dir": ".", "file": "fixture",
            "route_type": "3", "route": f"{ROUTE}: B1", "direction": "None",
            "origin": _entry(schedule, origin), "destination": _entry(schedule, destination),
            "offset": 0, "include_tomorrow": False}


def _train_data(schedule, origin, destination):
    return {"schedule": schedule, "gtfs_dir": ".", "file": "fixture",
            "route_type": "2", "route": "train", "direction": 0,
            "origin": origin, "destination": destination,
            "offset": 0, "include_tomorrow": False}


def _reader(name):
    """The helper under test, or None where this tree has no such reader."""
    return getattr(gtfs_helper, name, None) or getattr(geojson, name, None)


class Check:
    def __init__(self):
        self.records = []

    def note(self, ok, text, **fields):
        self.records.append({"ok": bool(ok), "text": text, **fields})

    def same(self, got, want, text, **fields):
        self.note(got == want, f"{text}: expected {want!r}, got {got!r}",
                  expected=want, got=got, **fields)

    def not_here(self, name, text):
        """A promise this tree cannot be asked: kept on record, not judged."""
        self.note(True, f"{text}: {name} is not in this tree, not checked here",
                  not_checked=name)

    @property
    def failures(self):
        return [r["text"] for r in self.records if not r["ok"]]


def _done(record_property, check, **case):
    record_property("case", case)
    record_property("checks", check.records)
    assert not check.failures, "\n".join(check.failures)


# --- the made-up line ---------------------------------------------------------

def test_stop_list_offers_the_places_with_a_way_on(record_property, bus):
    check = Check()
    offered = _ids(gtfs_helper.get_stop_list(bus, ROUTE, None))
    # Mairie (B) takes nobody on on any trip, the terminus (E) neither;
    # Hameau (H) wants a phone call, which is a way on
    check.same(offered, ["A", "C", "H", "D"], "the origin list")
    _done(record_property, check, fixture="boarding", promise="stop_list")


def test_destinations_offer_the_places_with_a_way_off(record_property, bus):
    check = Check()
    for origin, want in (("A", ["B", "C", "H", "E"]),   # Zone (D) sets down nobody
                         ("C", ["H", "E"]),
                         ("H", ["E"]),
                         ("D", ["E"])):
        offered = _ids(gtfs_helper.get_destination_stop_list(bus, ROUTE, None, origin))
        check.same(offered, want, f"the destinations from {origin}", origin=origin)
    _done(record_property, check, fixture="boarding", promise="destinations")


PAIRS = (  # origin, destination, whether some trip boards at one and alights at the other
    ("A", "B", True),    # a set-down only call is a place to get off
    ("A", "C", True),
    ("B", "C", False),   # nobody gets on at Mairie
    ("A", "D", False),   # nobody gets off at Zone
    ("C", "H", True),    # a phone call ahead is a way off
    ("H", "E", True),    # and a way on
    ("D", "E", True),
    ("A", "E", True),
)


def test_pairs_hold_to_the_feed(record_property, bus):
    check = Check()
    between, next_date = _reader("has_trip_between"), _reader("get_next_service_date")
    for origin, destination, exists in PAIRS:
        who = f"{origin} -> {destination}"
        if between:
            check.same(between(bus, ROUTE, origin, destination), exists,
                       f"has_trip_between {who}", origin=origin, destination=destination)
        else:
            check.not_here("has_trip_between", f"has_trip_between {who}")
        if next_date:
            got = next_date(bus, origin, destination, "2026-06-15")
            check.same(got, "2026-06-15" if exists else None,
                       f"next service date {who}", origin=origin, destination=destination)
        else:
            check.not_here("get_next_service_date", f"next service date {who}")
    # the first trip of the day, from just after midnight
    with freeze_time(datetime.datetime(2026, 6, 15, 0, 5, tzinfo=PARIS).astimezone(UTC)):
        for origin, destination, exists in PAIRS:
            who = f"{origin} -> {destination}"
            result = gtfs_helper.get_next_departure(_hass(), _bus_data(bus, origin, destination))
            if exists:
                got = (result.get("trip_id"), result.get("origin_stop_id"),
                       result.get("destination_stop_id")) if result else None
                check.same(got, ("T1", origin, destination), f"next departure {who}",
                           origin=origin, destination=destination)
                # the list runs over the days ahead; T2 skips Zone, so the
                # trips listed are the ones that ride the pair
                listed = sorted(set(result.get("next_departures_trip_id") or [])) if result else None
                want = ["T1", "T3"] if "D" in (origin, destination) else ["T1", "T2", "T3"]
                check.same(listed, want, f"trips listed {who}",
                           origin=origin, destination=destination)
            else:
                check.same(result, {}, f"next departure {who}",
                           origin=origin, destination=destination)
    _done(record_property, check, fixture="boarding", promise="pairs")


def test_a_stop_with_no_way_on_lists_no_local_departure(record_property, bus):
    check = Check()
    with freeze_time(datetime.datetime(2026, 6, 15, 7, 30, tzinfo=PARIS).astimezone(UTC)):
        for stop_id, where, want in (("B", (45.010, 5.010), 0),   # Mairie: nobody gets on
                                     ("H", (45.030, 5.030), 1),   # Hameau: phone ahead, T1 at 08:15
                                     ("A", (45.000, 5.000), 1)):  # Gare: T1 at 08:00
            me = types.SimpleNamespace(
                hass=_hass(where=where), _realtime=False,
                _data={"schedule": bus, "offset": 0, "file": "fixture", "gtfs_dir": ".",
                       "device_tracker_id": "person.rider", "radius": 100,
                       "timerange": 60, "timerange_history": 15, "name": "boarding"})
            listed = [d["trip_id"] for entry in gtfs_helper.get_local_stops_next_departures(me) or []
                      for d in entry.get("departure", []) if d["stop_id"] == stop_id]
            check.same(len(listed), want, f"departures listed at {stop_id} in the next hour",
                       stop=stop_id, listed=listed)
    _done(record_property, check, fixture="boarding", promise="local_stop")


def _route_file_name(route_id, direction):
    """The route file's name: the helper that names it, or the name itself
    on a tree that spells it where it writes it."""
    named = _reader("route_geojson_name")
    if named:
        return named(route_id, direction)
    safe = gtfs_helper.safe_file_part
    return f"{safe(route_id)}_{safe(direction)}_route.json"


def _route_file(schedule, tmp_path):
    """The sensor A -> E riding T1 at 08:00, its route file written under
    tmp_path and read back: (me, route)."""
    leaves = datetime.datetime(2026, 6, 15, 8, 0, tzinfo=PARIS)
    me = types.SimpleNamespace(
        hass=_hass(tmp_path), _route_id=ROUTE, _direction="0",
        _data={"schedule": schedule, "gtfs_dir": "gtfs2", "file": "fixture", "name": "boarding",
               "route": f"{ROUTE}: B1", "direction": "0",
               "origin": _entry(schedule, "A"), "destination": _entry(schedule, "E"),
               "next_departure": {
                   "trip_id": "T1", "departure_time": leaves,
                   "origin_stop_id": "A", "destination_stop_id": "E",
                   "route_id": ROUTE, "trip_direction_id": "0",
                   "next_departures_trip_id": ["T1"],
                   "next_departures": [leaves.isoformat()]}})
    geojson.write_route_file(me)
    with open(tmp_path / "www" / "gtfs2" / _route_file_name(ROUTE, "0"),
              encoding="utf-8") as handle:
        return me, json.load(handle)


def _points(route, *keys):
    """(stop_id, *keys) of every Point of a route file, in order."""
    return [tuple(f["properties"][k] for k in ("stop_id",) + keys)
            for f in route["features"] if f["geometry"]["type"] == "Point"]


def test_the_files_say_how_each_call_is_made(record_property, bus, tmp_path):
    check = Check()
    me, route = _route_file(bus, tmp_path)
    calls = _points(route, "pickup_type", "drop_off_type")
    want = [("A", 0, 1), ("B", 1, 0), ("C", 0, 0), ("H", 2, 2), ("D", 0, 1), ("E", 1, 0)]
    check.same(calls, want, "the route file's calls")
    # the line's word, over its three trips: the first stop sets nobody
    # down, Mairie takes nobody on, Zone sets nobody down, the terminus
    # takes nobody on; Hameau's phone call is a way on and off
    check.same(_points(route, "boards", "alights"),
               [("A", True, False), ("B", False, True), ("C", True, True),
                ("H", True, True), ("D", True, False), ("E", False, True)],
               "the route file's line flags")
    legs = _reader("write_leg_file")
    if legs:
        legs(me)
        with open(tmp_path / "www" / "gtfs2" / geojson.leg_geojson_name(ROUTE, "0", "boarding"),
                  encoding="utf-8") as handle:
            leg = json.load(handle)
        stops = leg["trips"]["T1"]["stops"]
        calls = [(s, stops[s]["pickup_type"], stops[s]["drop_off_type"])
                 for s in sorted(stops, key=lambda s: stops[s]["sequence"])]
        check.same(calls, want, "the leg file's calls, trip T1")
        calls = [(f["properties"]["stop_id"], f["properties"]["pickup_type"], f["properties"]["drop_off_type"])
                 for f in leg["features"]]
        check.same(calls, want, "the leg file's stop features")
    else:
        check.not_here("write_leg_file", "the leg file's calls")
    _done(record_property, check, fixture="boarding", promise="files")


@pytest.fixture(scope="module")
def bus_with_t4(tmp_path_factory):
    """The made-up line plus a fourth trip, T4 at 11:00, which takes riders
    on at Mairie and sets them down at Zone where the three others do not:
    the way a TER boards at the station the night train only sets down at."""
    root = tmp_path_factory.mktemp("boarding_t4")
    with zipfile.ZipFile(FIXTURES / "boarding" / "static.zip") as source, \
            zipfile.ZipFile(root / "static.zip", "w") as target:
        for name in source.namelist():
            data = source.read(name).decode("utf-8")
            if name == "trips.txt":
                data = data.rstrip("\n") + "\nB1,S,T4,Terminus,0\n"
            elif name == "stop_times.txt":
                data = data.rstrip("\n") + "\n" + "\n".join((
                    "T4,11:00:00,11:00:00,A,1,0,1",
                    "T4,11:05:00,11:05:00,B,2,0,0",
                    "T4,11:10:00,11:10:00,C,3,0,0",
                    "T4,11:15:00,11:15:00,H,4,0,0",
                    "T4,11:20:00,11:20:00,D,5,0,0",
                    "T4,11:25:00,11:25:00,E,6,1,0")) + "\n"
            target.writestr(name, data)
    return fixture_db.build(str(root))


def test_the_route_file_flags_the_line_not_the_drawn_trip(record_property, bus_with_t4, tmp_path):
    check = Check()
    _me, route = _route_file(bus_with_t4, tmp_path)
    # the drawn trip is still T1, and still says it takes nobody on at
    # Mairie and sets nobody down at Zone
    check.same(_points(route, "pickup_type", "drop_off_type"),
               [("A", 0, 1), ("B", 1, 0), ("C", 0, 0), ("H", 2, 2), ("D", 0, 1), ("E", 1, 0)],
               "the drawn trip's calls, unchanged by T4")
    # but the line now boards at Mairie and sets down at Zone, on T4: a
    # list filtered on the drawn trip would shut out a journey that works.
    # The first stop and the terminus stay what they are on every trip.
    check.same(_points(route, "boards", "alights"),
               [("A", True, False), ("B", True, True), ("C", True, True),
                ("H", True, True), ("D", True, True), ("E", False, True)],
               "the route file's line flags with T4")
    # the lists follow: Mairie is a departure, Zone an arrival from Gare
    check.same(_ids(gtfs_helper.get_stop_list(bus_with_t4, ROUTE, None)),
               ["A", "B", "C", "H", "D"], "the origin list with T4")
    check.same(_ids(gtfs_helper.get_destination_stop_list(bus_with_t4, ROUTE, None, "A")),
               ["B", "C", "H", "D", "E"], "the destinations from A with T4")
    _done(record_property, check, fixture="boarding", promise="line_flags")


# --- the SNCF night train -----------------------------------------------------

def test_train_stations_hold_to_the_feed(record_property, sncf):
    check = Check()
    # where the train takes riders on: Paris and Les Aubrais, nowhere south
    stations, arrivals = _reader("get_station_list"), _reader("get_train_destination_list")
    if stations:
        check.same(stations(sncf, NIGHT_ROUTE),
                   ["Les Aubrais", "Paris Austerlitz"], "the departure stations")
    else:
        check.not_here("get_station_list", "the departure stations")
    if arrivals:
        reached = arrivals(sncf, NIGHT_ROUTE, "Les Aubrais")
        check.same(len(reached), 12, "how many arrival stations from Les Aubrais",
                   reached=sorted(reached))
        check.note("Toulouse Matabiau" not in reached,
                   "Toulouse, passed without a stop, is not an arrival", reached=sorted(reached))
        check.note({"Auterive", "Ax-les-Thermes"} <= set(reached),
                   "the morning stops, set-down only, are arrivals", reached=sorted(reached))
        check.same(arrivals(sncf, NIGHT_ROUTE, "Auterive"), {},
                   "arrivals from Auterive, where nobody gets on")
    else:
        check.not_here("get_train_destination_list", "the arrival stations")
    # Les Aubrais takes riders on and sets nobody down: a way on to the
    # south, not a way off from Paris
    between, next_date = _reader("has_train_trip_between"), _reader("get_next_service_date")
    for origin, destination, exists in (("Les Aubrais", "Auterive", True),
                                        ("Les Aubrais", "Toulouse Matabiau", False),
                                        ("Auterive", "Ax-les-Thermes", False),
                                        ("Paris Austerlitz", "Les Aubrais", False),
                                        ("Paris Austerlitz", "Latour-de-Carol - Enveitg", True)):
        who = f"{origin} -> {destination}"
        if between:
            check.same(between(sncf, origin, destination), exists,
                       f"has_train_trip_between {who}", origin=origin, destination=destination)
        else:
            check.not_here("has_train_trip_between", f"has_train_trip_between {who}")
        if next_date:
            got = next_date(sncf, origin, destination, NIGHT_DAY, "2")
            check.same(got, NIGHT_DAY if exists else None, f"next service date {who}",
                       origin=origin, destination=destination)
        else:
            check.not_here("get_next_service_date", f"next service date {who}")
    with freeze_time(datetime.datetime(2026, 8, 27, 0, 5, tzinfo=PARIS).astimezone(UTC)):
        result = gtfs_helper.get_next_departure(_hass(), _train_data(sncf, "Les Aubrais", "Auterive"))
        check.same(result.get("destination_stop_time", {}).get("Sequence") if result else None, 3,
                   "Les Aubrais -> Auterive departs on the night train")
        result = gtfs_helper.get_next_departure(_hass(), _train_data(sncf, "Auterive", "Ax-les-Thermes"))
        check.same(result, {}, "Auterive -> Ax-les-Thermes, where nobody gets on")
    _done(record_property, check, fixture="sncf", promise="stations")
