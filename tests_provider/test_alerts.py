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

import datetime
import types
from pathlib import Path

import ha_stub
from freezegun import freeze_time

ha_stub.install()

import fixture_db  # noqa: E402

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")
alerts_mod = ha_stub.load("alerts")

FIXTURE = Path(__file__).parent / "fixtures" / "sncf"
# the capture's alerts say when they apply, and most of them ran out at the
# end of that August: read from any later day they are over, which is what
# the feed means. The reading is placed inside their period, the way the
# rest of the tree is placed on the capture's own day.
CAPTURED = datetime.datetime(2026, 8, 26, 8, 0, tzinfo=datetime.timezone.utc)


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
    sncf = fixture_db.shared(str(FIXTURE))
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

    with freeze_time(CAPTURED):
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
        # kept in the list, hung on its trip; the sentence speaks of the
        # next departure, which nothing is announced on
        check.note(not got.get("origin_stop_alert"), "the sentence is left to the next departure",
                   sentence=got.get("origin_stop_alert"))
    # the same feed read a year later: every period is over, and an alert
    # that applied last summer is not published as if it were current
    with freeze_time(CAPTURED + datetime.timedelta(days=365)):
        got = gtfs_rt_helper.get_rt_alerts(follower(named, []))
        check.same(got.get("origin_stop_alerts"), None, "the same alert once its period is over")
    # announced for a day ahead: kept, but never ahead of what runs now.
    # A year earlier, since the capture's alerts were published weeks
    # before they applied and a month back is already inside their period
    with freeze_time(CAPTURED - datetime.timedelta(days=365)):
        got = gtfs_rt_helper.get_rt_alerts(follower(named, []))
        items = got.get("origin_stop_alerts") or []
        # its start says it: later_only is for a later departure only
        check.same([i.get("later_only") for i in items] or None, [None],
                   "an alert announced for a later day is not about a later departure")
        check.note(items and all(p.get("start", "") > "2025-08-26T08:00:00+00:00"
                                 for i in items for p in i.get("periods") or [{}]),
                   "it is marked by its periods, all ahead",
                   periods=[i.get("periods") for i in items])
        check.same((got.get("origin_stop_alert"), got.get("alert_effect")), (None, None),
                   "it takes neither the sentence nor the effect")
    # ranked after what concerns the next departure, whatever the effect
    later = {"text": "later", "effect": "NO_SERVICE", "later_only": True}
    now = {"text": "now", "effect": "NO_EFFECT"}
    check.same([i["text"] for i in alerts_mod._rank_alerts([later, now])], ["now", "later"],
               "what concerns the next departure ranks first")
    # and after it, whatever the effect, what starts after the departure
    ride = ("2026-09-21T20:50:00+00:00", "2026-09-21T21:00:00+00:00")
    ahead = {"text": "ahead", "effect": "NO_SERVICE",
             "periods": [{"start": "2026-09-25T20:00:00+00:00"}]}
    begun = {"text": "begun", "effect": "NO_EFFECT",
             "periods": [{"start": "2026-09-21T20:00:00+00:00"}]}
    check.same([i["text"] for i in alerts_mod._rank_alerts([ahead, begun], ride)],
               ["begun", "ahead"], "what starts after the departure ranks after")
    _done(record_property, check, fixture="sncf", promise="alerts")


def test_alerts_name_their_stops(record_property, monkeypatch):
    """An alert addressed to a station of the journey carries the station's
    name: IDFM closes a station under the header "Travaux" and says which
    only in the stop it addresses, so the sentence alone tells nobody where."""
    from google.transit import gtfs_realtime_pb2 as rt
    sncf = fixture_db.shared(str(FIXTURE))
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


def test_alerts_to_come_say_when(record_property, monkeypatch):
    """Metro 6 on 2026-09-21 at 22:50: IDFM announced a night closure for
    the 25th and five Saturdays of works from 4 October, and the line
    read "Trafic interrompu" that evening. Alerts to come stay in the list
    with every period the feed gives, for the card to sort; the sentence,
    the cause and the effect are those of what applies now."""
    from google.transit import gtfs_realtime_pb2 as rt
    sncf = fixture_db.shared(str(FIXTURE))
    trip = ("OCEEA436011R5235_R:CTE:FR:Line::8440e055-0d15-4156-9e77-017af816441a"
            "::87296442:87296012:5:1327:20260828")
    route = "FR:Line::8440e055-0d15-4156-9e77-017af816441a:"
    now = datetime.datetime(2026, 9, 21, 20, 50, tzinfo=datetime.timezone.utc)

    def at(day, hour, minute=0):
        return int(datetime.datetime(2026, day[1], day[0], hour, minute,
                                     tzinfo=datetime.timezone.utc).timestamp())

    def alert(key, header, cause, effect, periods):
        entity = rt.FeedEntity(id=key)
        entity.alert.header_text.translation.add(text=header, language="fr")
        entity.alert.cause = cause
        entity.alert.effect = effect
        entity.alert.informed_entity.add().route_id = route
        for start, end in periods:
            period = entity.alert.active_period.add()
            period.start = start
            period.end = end
        return entity

    # the Saturdays in the feed's own order, which is not the calendar's
    works = alert("works", "Travaux de modernisation - Trafic interrompu",
                  rt.Alert.CONSTRUCTION, rt.Alert.NO_SERVICE,
                  [(at((11, 10), 2, 45), at((12, 10), 2, 30)),
                   (at((4, 10), 2, 45), at((5, 10), 2, 30))])
    police = alert("police", "Mesures de sécurité - Trafic interrompu",
                   rt.Alert.POLICE_ACTIVITY, rt.Alert.NO_SERVICE,
                   [(at((25, 9), 20), at((26, 9), 2, 30))])
    delay = alert("delay", "Trafic ralenti", rt.Alert.TECHNICAL_PROBLEM,
                  rt.Alert.SIGNIFICANT_DELAYS, [(at((21, 9), 20), at((21, 9), 22))])

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
    with freeze_time(now):
        # only alerts to come: listed, dated, and nothing current is said
        monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities",
                            lambda **_kw: [works, police])
        got = gtfs_rt_helper.get_rt_alerts(follower())
        items = got.get("origin_stop_alerts") or []
        check.same([i["text"] for i in items],
                   ["Travaux de modernisation - Trafic interrompu",
                    "Mesures de sécurité - Trafic interrompu"], "both are listed")
        check.same([i.get("later_only") for i in items], [None, None],
                   "neither is about a later departure")
        check.same([i.get("periods") for i in items],
                   [[{"start": "2026-10-11T02:45:00+00:00", "end": "2026-10-12T02:30:00+00:00"},
                     {"start": "2026-10-04T02:45:00+00:00", "end": "2026-10-05T02:30:00+00:00"}],
                    [{"start": "2026-09-25T20:00:00+00:00", "end": "2026-09-26T02:30:00+00:00"}]],
                   "each gives all its periods, in the feed's order")
        check.same((got.get("origin_stop_alert"), got.get("destination_stop_alert"),
                    got.get("alert_cause"), got.get("alert_effect")),
                   (None, None, None, None), "nothing current is said")
        # with a slowdown now: it takes the sentence, the effect and the head
        monkeypatch.setattr(gtfs_rt_helper, "get_gtfs_feed_entities",
                            lambda **_kw: [works, police, delay])
        got = gtfs_rt_helper.get_rt_alerts(follower())
        items = got.get("origin_stop_alerts") or []
        check.same([i["text"] for i in items][:1], ["Trafic ralenti"], "what applies now comes first")
        check.same((items[0].get("later_only"), items[0].get("periods")),
                   (None, [{"start": "2026-09-21T20:00:00+00:00", "end": "2026-09-21T22:00:00+00:00"}]),
                   "and is under way")
        check.same((got.get("origin_stop_alert"), got.get("alert_effect")),
                   ("Trafic ralenti", "SIGNIFICANT_DELAYS"), "the sentence and effect are its own")
        check.same(len(items), 3, "the two to come are still listed")
    _done(record_property, check, fixture="sncf", promise="alerts to come")
