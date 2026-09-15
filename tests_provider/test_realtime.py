"""What the realtime feed strikes out is struck out on the board.

GTFS-RT says more than a delay: schedule_relationship on a trip reads
CANCELED (or DELETED) when it does not run, ADDED when the static feed
never had it; on a stop update it reads SKIPPED when the vehicle does not
call there, NO_DATA when there is no prediction for that call. The fork
used to read none of it, so a cancelled train stood on the board as on
time. The promises, on the SNCF fixture's own capture of 2026-08-26
(fixtures/sncf/trip_updates.pb: two trips cancelled with every stop
SKIPPED, one of them with a delay left in; a TER skipping its first two
stops; two trains ADDED under ids the static feed has not):

    converter   every converted entity names the trip's and each stop
                update's schedule_relationship, spelled out
    statuses    a cancelled trip gives no departure; a call the trip
                skips gives none; a call without data gives none, while
                the calls with a prediction keep theirs, each with its trip
    board       the departures read again without the struck trips move
                on to the next one that runs, on the service day the feed
                names and no other
    leg_file    the leg file says which run is cancelled and which call is
                skipped, so a card can say so instead of timing it
    local_stop  a cancelled trip is not listed among the departures of a
                stop nearby

    pytest tests_provider/test_realtime.py
"""
from __future__ import annotations

import datetime
import json
import types
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
gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

FIXTURE = Path(__file__).parent / "fixtures" / "sncf"
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
UTC = datetime.timezone.utc
DAY = datetime.datetime(2026, 8, 26, 7, 50, tzinfo=PARIS)
# the C3 Grasse to Cannes, its 08:08 cancelled that morning; the P9 out of
# Béziers, skipping Béziers and Magalas that morning
C3 = "FR:Line::0c436faa-ccd8-4cb5-9b79-1c8a701364c4:"
P9 = "FR:Line::3093CF67-E390-48A9-89CB-70DDAAFB5057:"
GRASSE = "StopPoint:OCETrain TER-87757724"
CANNES = "StopPoint:OCETrain TER-87757625"
BEZIERS = "StopPoint:OCETrain TER-87781005"
BEDARIEUX = "StopPoint:OCETrain TER-87781609"


@pytest.fixture(scope="module")
def sncf():
    return fixture_db.build(str(FIXTURE))


@pytest.fixture(scope="module")
def entities():
    """The capture, through the converter the feed goes through."""
    return gtfs_rt_helper.convert_gtfs_realtime_to_json(
        (FIXTURE / "trip_updates.pb").read_bytes())["entity"]


@pytest.fixture(autouse=True)
def paris_clock():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))


def _trip_id(entities, prefix):
    return next(e["trip_update"]["trip"]["trip_id"] for e in entities
                if e["trip_update"]["trip"]["trip_id"].startswith(prefix))


def _hass(root=".", where=None):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(Path(root, *parts)),
                                     time_zone="Europe/Paris"),
        states=types.SimpleNamespace(get=lambda _entity: types.SimpleNamespace(
            attributes={"latitude": where[0], "longitude": where[1]} if where else {})))


def _follower(route_id, direction, trip_id, stop_id, trip_list=()):
    """What get_rt_route_trip_statuses reads off a coordinator following
    one departure: the entity's line, direction, trip and origin."""
    return types.SimpleNamespace(
        _vehicle_position_url=None, _trip_update_url="unused", _headers=None,
        _route_delimiter=None, _rt_group="route", _route_id=route_id,
        _direction=str(direction), _trip_id=trip_id, _trip_short_name=None,
        _trip_list=list(trip_list), _stop_id=stop_id, _stop_sequence=None,
        _destination_id=None, _relative=False, info={})


class Check:
    def __init__(self):
        self.records = []

    def note(self, ok, text, **fields):
        self.records.append({"ok": bool(ok), "text": text, **fields})

    def same(self, got, want, text, **fields):
        self.note(got == want, f"{text}: expected {want!r}, got {got!r}",
                  expected=want, got=got, **fields)

    @property
    def failures(self):
        return [r["text"] for r in self.records if not r["ok"]]


def _done(record_property, check, **case):
    record_property("case", case)
    record_property("checks", check.records)
    assert not check.failures, "\n".join(check.failures)


def test_the_converter_spells_out_what_the_feed_struck(record_property, entities):
    check = Check()
    trips = [e["trip_update"]["trip"]["schedule_relationship"] for e in entities]
    stops = [s["schedule_relationship"] for e in entities for s in e["trip_update"]["stop_time_update"]]
    check.same(sorted(trips), sorted(["SCHEDULED"] * 5 + ["ADDED"] * 2 + ["CANCELED"] * 2),
               "the trips' relationships")
    check.same((stops.count("SCHEDULED"), stops.count("SKIPPED"), stops.count("NO_DATA")),
               (80, 40, 0), "the stop updates' relationships (scheduled, skipped, no data)")
    _done(record_property, check, fixture="sncf", promise="converter")


def test_struck_trips_give_no_departure(record_property, entities):
    check = Check()
    cancelled = _trip_id(entities, "OCESA86017F5111")
    me = _follower(C3, 1, cancelled, GRASSE)
    statuses = gtfs_rt_helper.get_rt_route_trip_statuses(me, entities)
    slot = statuses.get(C3, {}).get("1", {}).get(GRASSE, {})
    check.same(slot.get("departures", []), [], "departures of the cancelled trip at Grasse")
    check.same(gtfs_rt_helper.struck_trips(me), {cancelled: "20260826"},
               "what the feed struck out for the C3 follower")
    # the P9 runs but does not call at Béziers that morning
    p9 = _trip_id(entities, "OCESN878950F1187")
    me = _follower(P9, 1, p9, BEZIERS)
    statuses = gtfs_rt_helper.get_rt_route_trip_statuses(me, entities)
    check.same(statuses.get(P9, {}).get("1", {}).get(BEZIERS, {}).get("departures", []), [],
               "departures of the P9 at Béziers, skipped")
    check.same(gtfs_rt_helper.struck_trips(me), {p9: "20260826"},
               "what the feed struck out for the Béziers follower")
    # and calls at Bédarieux, 35 minutes late (07:14 + 35 = 07:49), the trip
    # named beside the time; asked before that, a call gone by is not listed
    me = _follower(P9, 1, p9, BEDARIEUX)
    with freeze_time(DAY.replace(hour=7, minute=30).astimezone(UTC)):
        statuses = gtfs_rt_helper.get_rt_route_trip_statuses(me, entities)
    slot = statuses.get(P9, {}).get("1", {}).get(BEDARIEUX, {})
    check.same(slot.get("delays"), [2100], "the P9's delay at Bédarieux")
    check.same(slot.get("trips"), [p9], "the trip behind the departure at Bédarieux")
    check.same(gtfs_rt_helper.struck_trips(me), {}, "nothing struck for the Bédarieux follower")
    # a call without data gives no departure, and a zero delay is not on time
    without = json.loads(json.dumps(entities))
    for e in without:
        if e["trip_update"]["trip"]["trip_id"] == p9:
            for s in e["trip_update"]["stop_time_update"]:
                s["schedule_relationship"] = "NO_DATA"
                s["arrival"] = s["departure"] = {"time": 0, "delay": 0}
    with freeze_time(DAY.replace(hour=7, minute=30).astimezone(UTC)):
        statuses = gtfs_rt_helper.get_rt_route_trip_statuses(me, without)
    check.same(statuses.get(P9, {}).get("1", {}).get(BEDARIEUX, {}).get("departures", []), [],
               "departures at Bédarieux when the feed has no data there")
    check.same(gtfs_rt_helper.struck_trips(me), {}, "no data is not a strike")
    _done(record_property, check, fixture="sncf", promise="statuses")


def test_the_board_moves_on_without_the_struck_trip(record_property, sncf, entities):
    check = Check()
    cancelled = _trip_id(entities, "OCESA86017F5111")
    data = {"schedule": sncf, "gtfs_dir": ".", "file": "fixture",
            "route_type": "2", "route": "train", "direction": 0,
            "origin": "Grasse", "destination": "Cannes", "line": "C3",
            "offset": 0, "include_tomorrow": True}
    with freeze_time(DAY.astimezone(UTC)):
        departure = gtfs_helper.get_next_departure(_hass(), data)
        check.same(departure.get("trip_id") if departure else None, cancelled,
                   "the next departure the timetable gives at 07:50")
        check.note(bool(data.get("departure_rows")), "the rows are kept beside the departure",
                   rows=len(data.get("departure_rows") or []))
        data["next_departure"] = departure
        today = datetime.date(2026, 8, 26)
        # the SNCF files one trip id per train number, running every day:
        # the feed cancels today's run, and the board moves on to tomorrow's
        moved = gtfs_helper.drop_departure_trips(_hass(), data, {cancelled: "20260826"})
        shown = moved.get("departure_time") if moved else None
        check.note(shown is not None and shown.date() > today,
                   f"without today's run the board shows {shown}",
                   got=shown.isoformat() if shown else None)
        days = sorted({datetime.datetime.fromisoformat(d).astimezone(PARIS).date().isoformat()
                       for d in (moved or {}).get("next_departures") or []})
        check.note(today.isoformat() not in days, "today's run is out of the list too", days=days)
        # struck on another service day: today's departure stands
        same = gtfs_helper.drop_departure_trips(_hass(), data, {cancelled: "20260827"})
        check.same(same.get("departure_time").date().isoformat() if same else None,
                   today.isoformat(), "struck on another day, today's run still shows")
        # struck with no day named: dropped on every day, nothing left here
        every = gtfs_helper.drop_departure_trips(_hass(), data, {cancelled: None})
        check.same(every, {}, "struck with no day named, the trip is out on every day")
    check.same([gtfs_rt_helper.on_service_day("20260826", "2026-08-26"),
                gtfs_rt_helper.on_service_day("20260826", "2026-08-27"),
                gtfs_rt_helper.on_service_day(None, "2026-08-27"),
                gtfs_rt_helper.on_service_day("20260826", "2026-08-26 08:08:00")],
               [True, False, True, True], "the service day rule")
    _done(record_property, check, fixture="sncf", promise="board")


def test_the_leg_file_says_what_is_struck(record_property, sncf, entities, tmp_path):
    check = Check()
    cancelled = _trip_id(entities, "OCESA86017F5111")
    p9 = _trip_id(entities, "OCESN878950F1187")
    for route_id, trip_id, origin, leaves in (
            (C3, cancelled, GRASSE, datetime.datetime(2026, 8, 26, 8, 8, tzinfo=PARIS)),
            (P9, p9, BEZIERS, datetime.datetime(2026, 8, 26, 6, 37, tzinfo=PARIS))):
        me = types.SimpleNamespace(
            hass=_hass(tmp_path), _route_id=route_id, _direction="1",
            _data={"schedule": sncf, "gtfs_dir": "gtfs2", "file": "fixture", "name": "leg",
                   "route": f"{route_id}: x", "direction": "1", "origin": f"{origin}: x (1)",
                   "next_departure": {
                       "trip_id": trip_id, "departure_time": leaves,
                       "origin_stop_id": origin, "route_id": route_id, "trip_direction_id": "1",
                       "next_departures_trip_id": [trip_id],
                       "next_departures": [leaves.isoformat()]}})
        gtfs_helper.update_leg_geojson(me, entities)
        with open(tmp_path / "www" / "gtfs2" / gtfs_helper.leg_geojson_name(route_id, "1", "leg"),
                  encoding="utf-8") as handle:
            leg = json.load(handle)
        run = leg["trips"][trip_id]
        if trip_id == cancelled:
            check.same(run.get("cancelled"), True, "the cancelled run is marked")
            check.note(all("expected" not in s for s in run["stops"].values()),
                       "a cancelled run is not timed")
            check.same(leg["properties"].get("realtime"), True, "the file knows it read realtime")
        else:
            check.same((run["stops"][BEZIERS].get("skipped"), run["stops"][BEDARIEUX].get("skipped")),
                       (True, None), "Béziers skipped, Bédarieux not")
            check.same(run["stops"][BEDARIEUX].get("delay"), 2100, "Bédarieux timed, 35 min late")
            check.note("expected" not in run["stops"][BEZIERS], "a skipped call is not timed")
            check.same(run.get("cancelled"), None, "the P9 runs")
    _done(record_property, check, fixture="sncf", promise="leg_file")


def test_a_cancelled_trip_is_not_a_local_departure(record_property, sncf, entities, monkeypatch):
    check = Check()
    cancelled = _trip_id(entities, "OCESA86017F5111")
    monkeypatch.setattr(gtfs_helper, "get_gtfs_rt", lambda *_args, **_kw: "ok")
    monkeypatch.setattr(gtfs_helper, "get_gtfs_feed_entities", lambda **_kw: entities)
    listed = {}
    for realtime in (False, True):
        me = types.SimpleNamespace(
            hass=_hass(where=(43.653344, 6.925549)), _realtime=realtime,
            _trip_update_url="file://unused", _headers={},
            _vehicle_position_url=None, _route_delimiter=None,
            _data={"schedule": sncf, "offset": 0, "file": "fixture", "gtfs_dir": ".",
                   "device_tracker_id": "person.rider", "radius": 100,
                   "timerange": 60, "timerange_history": 15, "name": "grasse"})
        with freeze_time(DAY.astimezone(UTC)):
            listed[realtime] = [d["trip_id"] for entry in gtfs_helper.get_local_stops_next_departures(me) or []
                                for d in entry.get("departure", [])]
    check.note(cancelled in listed[False], "without realtime the timetable lists the 08:08",
               listed=listed[False])
    check.note(cancelled not in listed[True], "with realtime the cancelled 08:08 is not listed",
               listed=listed[True])
    _done(record_property, check, fixture="sncf", promise="local_stop")


# --- alerts on the listed departures ---------------------------------------

def test_alerts_reach_the_listed_trips(record_property, sncf, monkeypatch):
    """An alert naming a later departure of the board is read too, hung on
    the trip it names, and ranked after what concerns the next one."""
    from google.transit import gtfs_realtime_pb2 as rt
    feed = rt.FeedMessage()
    feed.ParseFromString((FIXTURE / "service_alerts.pb").read_bytes())
    alerts = list(feed.entity)
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities", lambda **_kw: alerts)
    check = Check()
    # the capture's own scenarios: a trip an alert names by its number, and
    # a trip nothing is announced on
    named = ("OCESN853603F1187_F:TER:FR:Line::1f647a2c-138d-47de-8fb5-333f230e16f7"
             "::87444711:87444000:7:802:20261211")
    quiet = ("OCEEA436011R5235_R:CTE:FR:Line::8440e055-0d15-4156-9e77-017af816441a"
             "::87296442:87296012:5:1327:20260828")

    def follower(head, listed):
        return types.SimpleNamespace(
            hass=types.SimpleNamespace(config=types.SimpleNamespace(language="fr")),
            _alerts_url="http://alerts.test/feed", _headers=None,
            _route_id="FR:Line::8440e055-0d15-4156-9e77-017af816441a:",
            _stop_id="StopPoint:OCECar TER-87296442",
            _destination_id="StopPoint:OCECar TER-87296012",
            _trip_id=head, _trip_list=listed,
            _data={"file": "fixture", "schedule": sncf,
                   "next_departure": {"trip_id": head, "origin_stop_sequence": 0,
                                      "destination_stop_time": {"Sequence": 4}}})

    # the quiet trip alone: nothing
    got = gtfs_rt_helper.get_rt_alerts(follower(quiet, []))
    check.same(got.get("origin_stop_alerts"), None, "alerts on the quiet trip alone")
    # the named trip as the next departure: found, hung on it
    got = gtfs_rt_helper.get_rt_alerts(follower(named, []))
    items = got.get("origin_stop_alerts") or []
    check.same(len(items), 1, "alerts on the named trip as the next departure")
    check.same([i.get("trips") for i in items], [[named]], "the alert names that trip")
    check.same([i.get("later_only") for i in items], [None], "it concerns the next departure")
    # the named trip listed behind the quiet one: found too, marked as later
    got = gtfs_rt_helper.get_rt_alerts(follower(quiet, [named]))
    items = got.get("origin_stop_alerts") or []
    check.same(len(items), 1, "alerts with the named trip listed second")
    check.same([i.get("trips") for i in items], [[named]], "the alert names the listed trip")
    check.same([i.get("later_only") for i in items], [True], "it concerns a later departure only")
    check.note(bool(got.get("origin_stop_alert")), "the sentence is still published",
               sentence=got.get("origin_stop_alert"))
    # ranked after what concerns the next departure, whatever the effect
    later = {"text": "later", "effect": "NO_SERVICE", "later_only": True}
    now = {"text": "now", "effect": "NO_EFFECT"}
    check.same([i["text"] for i in gtfs_rt_helper._rank_alerts([later, now])], ["now", "later"],
               "what concerns the next departure ranks first")
    _done(record_property, check, fixture="sncf", promise="alerts")
