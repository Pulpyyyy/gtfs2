"""An alert naming a departure listed behind the next one is read too.

The SNCF addresses most of its alerts to trips, by train number, and the
reader used to match them against the next departure alone: an alert on the
second train of the board, the one the rider may well be waiting for, was
invisible until that train became the next one. The promise, on the SNCF
fixture's own capture (fixtures/sncf/service_alerts.pb):

    alerts   the alert naming a trip is found whether that trip is the next
             departure or listed behind it, hung on the trip it names
             (item["trips"]), marked later_only when it names a later
             departure and nothing else of the journey, and ranked after
             whatever concerns the next departure

    pytest tests_provider/test_alerts.py
"""
from __future__ import annotations

import types
from pathlib import Path

import ha_stub

ha_stub.install()

import fixture_db  # noqa: E402

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")
alerts_mod = ha_stub.load("alerts")

FIXTURE = Path(__file__).parent / "fixtures" / "sncf"


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


def test_alerts_reach_the_listed_trips(record_property, monkeypatch):
    """An alert naming a later departure of the board is read too, hung on
    the trip it names, and ranked after what concerns the next one."""
    from google.transit import gtfs_realtime_pb2 as rt
    sncf = fixture_db.build(str(FIXTURE))
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
    check.same([i["text"] for i in alerts_mod._rank_alerts([later, now])], ["now", "later"],
               "what concerns the next departure ranks first")
    _done(record_property, check, fixture="sncf", promise="alerts")


def test_alerts_name_their_stops(record_property, monkeypatch):
    """An alert addressed to a station of the journey carries the station's
    name: IDFM closes a station under the header "Travaux" and says which
    only in the stop it addresses, so the sentence alone tells nobody where."""
    from google.transit import gtfs_realtime_pb2 as rt
    sncf = fixture_db.build(str(FIXTURE))
    trip = ("OCEEA436011R5235_R:CTE:FR:Line::8440e055-0d15-4156-9e77-017af816441a"
            "::87296442:87296012:5:1327:20260828")
    route = "FR:Line::8440e055-0d15-4156-9e77-017af816441a:"

    def works(stop_id):
        entity = rt.FeedEntity(id=f"works-{stop_id}")
        entity.alert.header_text.translation.add(text="Travaux", language="fr")
        entity.alert.cause = rt.Alert.CONSTRUCTION
        informed = entity.alert.informed_entity.add()
        informed.route_id = route
        informed.stop_id = stop_id
        return entity

    def follower():
        return types.SimpleNamespace(
            hass=types.SimpleNamespace(config=types.SimpleNamespace(language="fr")),
            _alerts_url="http://alerts.test/feed", _headers=None,
            _route_id=route,
            _stop_id="StopPoint:OCECar TER-87296442",
            _destination_id="StopPoint:OCECar TER-87296012",
            _trip_id=trip, _trip_list=[],
            _data={"file": "fixture", "schedule": sncf,
                   "next_departure": {"trip_id": trip, "origin_stop_sequence": 0,
                                      "destination_stop_time": {"Sequence": 4}}})

    check = Check()
    # Versigny, passed on the way, named by its station as IDFM does
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities",
                        lambda **_kw: [works("StopArea:OCE87296608")])
    got = gtfs_rt_helper.get_rt_alerts(follower())
    items = got.get("origin_stop_alerts") or []
    check.same([i.get("stops") for i in items], [["Versigny"]], "the station passed is named")
    check.same(got.get("origin_stop_alert"), "Travaux", "the sentence stays the feed's")
    # the departure itself, named by its platform: the station's name
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities",
                        lambda **_kw: [works("StopPoint:OCECar TER-87296442")])
    items = gtfs_rt_helper.get_rt_alerts(follower()).get("origin_stop_alerts") or []
    check.same([i.get("stops") for i in items], [["Tergnier"]], "the departure is named")
    # a station off the journey: no alert at all, as before
    monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities",
                        lambda **_kw: [works("StopArea:OCE99999999")])
    check.same(gtfs_rt_helper.get_rt_alerts(follower()).get("origin_stop_alerts"), None,
               "a station off the journey")
    # the same sentence at two stations is two alerts
    two = [{"text": "Travaux", "stops": ["A"]}, {"text": "Travaux", "stops": ["B"]}]
    check.same(len(alerts_mod._rank_alerts(two)), 2, "works at two stations")
    _done(record_property, check, fixture="sncf", promise="alert stops")
