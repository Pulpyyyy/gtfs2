"""When nothing is left to show today, the sensor says when the line runs next.

A line with nothing to show has five stories to tell apart, and only the
timetable tells them apart:

    none          no service at all ahead: no date, "No scheduled departures"
    later         the next one is days away: that date, and how many days
    tomorrow      the date found is tomorrow and no departure is in hand:
                  that date, one day
    tomorrow_at   the departure in hand is tomorrow's: its time
    today         today runs, but every departure is behind us

The promises:

    dates      get_next_service_date answers the first day on or after the
               day asked, within 90 days, that a trip of the line calls at
               the origin and then at the destination, as the zip's
               calendar_dates say (TAO), and as calendar weekdays, removals,
               additions, the end of validity and the horizon say (a small
               feed made here); at a loop's terminus only the way round
               asked counts; an unusable datasource or a failing read
               gives no date, never an error
    outcomes   down the sensor's own path (get_next_departure at a frozen
               instant; when it holds nothing, the coordinator's
               next_service_date_for from today; then next_service_info, which
               writes the sensor's attributes), each chosen pair and moment
               ends on the outcome, the date, the day count and the sentence
               written in the case table
    no departure in hand
               next_service_info, handed a date and no departure, names the
               date and the day count for tomorrow and for days away

The expected dates and departures are read from the zip's rows in plain
Python, never from the component's SQL; the sentences are literals in the
case tables, never derived by a copy of the sensor's branching.

    pytest tests_provider/test_next_service.py
"""
from __future__ import annotations

import asyncio
import csv
import datetime
import io
import types
import zipfile
import zoneinfo
from pathlib import Path

import pytest
from sqlalchemy import text
from freezegun import freeze_time

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

import fixture_db  # noqa: E402
import test_journeys as tj  # noqa: E402

gtfs_helper = ha_stub.load("gtfs_helper")
refresh_steps = ha_stub.load("refresh_steps")
departure_attributes = ha_stub.load("departure_attributes")

TAO = Path(__file__).parent / "fixtures" / "tao-journeys"
PARIS = zoneinfo.ZoneInfo("Europe/Paris")
HORIZON = 90

L40, L41, LN = "ORLEANS:Line:40", "ORLEANS:Line:41", "ORLEANS:Line:N"
L22, LA = "ORLEANS:Line:22", "ORLEANS:Line:A"
# line 40, Cheques Postaux quai C to Gare d'Orleans quai E: weekdays of
# 2026-08-24 to 08-28 only in this fixture
QUAI_C, QUAI_E = "ORLEANS:StopArea:00007923", "ORLEANS:StopArea:01001712"
# quai D is on the way back only: no trip rides from it to quai E
QUAI_D = "ORLEANS:StopArea:00007924"
# line N, the night bus, Thursdays from 2026-09-03
CITE_U, INTERIVES = "ORLEANS:StopArea:01086001", "ORLEANS:StopArea:01002003"
# line 41 runs one day, 2026-08-24
ESAT, QUAI_G = "ORLEANS:StopArea:00043200", "ORLEANS:StopArea:01001710"
# line 22 is a circle from Zenith to Zenith, three records of one name
ZENITH_OUT, ZENITH_IN = "ORLEANS:StopArea:01001010", "ORLEANS:StopArea:06001010"
# tram A, which runs most days
HOPITAL, JULES_VERNE = "ORLEANS:StopArea:THOPIT2", "ORLEANS:StopArea:TVERNE2"


class Feed:
    """The zip's rows, read the way a rider reads a timetable; the places
    its records group into are the component's, read on the database built
    from the same zip."""

    def __init__(self, archive, schedule):
        self.schedule = schedule
        with zipfile.ZipFile(archive) as zin:
            def rows(name):
                if name not in zin.namelist():
                    return []
                return list(csv.DictReader(io.TextIOWrapper(zin.open(name), encoding="utf-8-sig")))
            self.stops = {s["stop_id"]: s for s in rows("stops.txt")}
            self.trips = rows("trips.txt")
            self.calendar = {c["service_id"]: c for c in rows("calendar.txt")}
            self.dates = rows("calendar_dates.txt")
            self.calls = {}
            for call in rows("stop_times.txt"):
                self.calls.setdefault(call["trip_id"], []).append(call)
            self.zone = zoneinfo.ZoneInfo(rows("agency.txt")[0]["agency_timezone"])
        for calls in self.calls.values():
            calls.sort(key=lambda c: int(c["stop_sequence"]))

    def runs(self, service_id, day):
        """Whether the service runs on that day, by the tests' one reading of
        the GTFS calendar (test_journeys.service_days_of)."""
        if not hasattr(self, "_runs"):
            self._runs = tj.service_days_of(self.calendar.values(), self.dates)
        return day in self._runs.get(service_id, ())

    def place(self, stop_id):
        """The records a rider waits at, as the component groups them
        (_place_group), never by a copy of its rule here."""
        with self.schedule.engine.connect() as conn:
            return {stop_id} | {row[0] for row in conn.execute(
                text("SELECT stop_id FROM stops WHERE stop_id IN " + gtfs_helper._place_group("s")),
                {"s": stop_id})}

    def rides(self, route_id, origin, destination):
        """(service_id, departure clock) of every trip of the line a rider
        boards at the origin and leaves at the destination afterwards."""
        origins, destinations = self.place(origin), self.place(destination)
        found = []
        for trip in self.trips:
            if route_id and trip["route_id"] != route_id:
                continue
            calls = self.calls.get(trip["trip_id"], [])
            for i, on in enumerate(calls):
                if on["stop_id"] not in origins or on.get("pickup_type") == "1":
                    continue
                if any(off["stop_id"] in destinations and off.get("drop_off_type") != "1"
                       for off in calls[i + 1:]):
                    found.append((trip["service_id"], on["departure_time"]))
                    break
        return found

    def next_service(self, route_id, origin, destination, asked):
        """The first day from asked, 90 days on at most, a ride runs."""
        services = {service for service, _ in self.rides(route_id, origin, destination)}
        for n in range(HORIZON + 1):
            day = asked + datetime.timedelta(days=n)
            if any(self.runs(service, day) for service in services):
                return day.isoformat()
        return None

    def next_departure(self, route_id, origin, destination, now):
        """The first ride leaving the origin after now, on the agency's clock."""
        best = None
        for service, clock in self.rides(route_id, origin, destination):
            hours, minutes, seconds = (int(part) for part in clock.split(":"))
            # a clock past 24:00 leaves a day or two after its service day
            for back in range(-1, 3 + HORIZON):
                day = now.date() + datetime.timedelta(days=back - 2)
                if not self.runs(service, day):
                    continue
                leaves = datetime.datetime.combine(day, datetime.time(), self.zone) + datetime.timedelta(
                    hours=hours, minutes=minutes, seconds=seconds)
                if leaves > now and (best is None or leaves < best):
                    best = leaves
        return best


@pytest.fixture(scope="module")
def tao():
    schedule = fixture_db.shared(str(TAO))
    return schedule, Feed(TAO / "static.zip", schedule)


@pytest.fixture(autouse=True)
def paris_clock():
    dt_util.set_default_time_zone(dt_util.get_time_zone("Europe/Paris"))


def _name(feed, stop_id):
    return f"{stop_id}: {feed.stops[stop_id]['stop_name']} (1)"


def _data(schedule, feed, route_id, origin, destination):
    """An entry's data, as the coordinator hands it to the departure reads."""
    return {"schedule": schedule, "gtfs_dir": ".", "file": "fixture",
            "route_type": "3", "route": f"{route_id}: x", "direction": "None",
            "origin": _name(feed, origin), "destination": _name(feed, destination),
            "offset": 0}


def _hass():
    async def run(job):
        return job()
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(Path(".", *parts)),
                                     time_zone="Europe/Paris"),
        async_add_executor_job=run)


# --- dates ----------------------------------------------------------------------

PAIRS = [(L40, QUAI_C, QUAI_E), (L40, QUAI_D, QUAI_E), (LN, CITE_U, INTERIVES),
         (L41, ESAT, QUAI_G), (L22, ZENITH_OUT, ZENITH_IN), (LA, HOPITAL, JULES_VERNE)]
DAYS = ["2026-08-22", "2026-08-23", "2026-08-24", "2026-08-25", "2026-08-28",
        "2026-08-29", "2026-09-02", "2026-09-03", "2026-09-04", "2026-12-24",
        "2026-12-25"]


@pytest.mark.parametrize("route_id, origin, destination", PAIRS,
                         ids=["40", "40_backwards", "N", "41", "22_circle", "A"])
def test_the_next_service_date_is_the_first_day_the_zip_runs_a_ride(tao, route_id, origin, destination):
    schedule, feed = tao
    for asked in DAYS:
        expected = feed.next_service(route_id, origin, destination, datetime.date.fromisoformat(asked))
        got = gtfs_helper.get_next_service_date(schedule, origin, destination, asked,
                                                "3", route=route_id)
        assert got == expected, f"asked on {asked}"


def test_the_chosen_pairs_tell_the_stories_apart(tao):
    # the table below leans on these facts of the zip: if the fixture is
    # rebuilt and they move, this says so before the outcomes do
    _, feed = tao

    def on(route_id, origin, destination, day):
        return feed.next_service(route_id, origin, destination, datetime.date.fromisoformat(day))

    assert on(L40, QUAI_C, QUAI_E, "2026-08-22") == "2026-08-24"
    assert on(L40, QUAI_C, QUAI_E, "2026-08-29") is None
    assert on(L40, QUAI_D, QUAI_E, "2026-08-22") is None
    assert on(LN, CITE_U, INTERIVES, "2026-08-24") == "2026-09-03"
    assert on(LN, CITE_U, INTERIVES, "2026-09-04") == "2026-09-10"


CALENDAR_FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,Europe/Paris\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.01,1.01\n"
                  "S3,Three,47.1,1.1\nS4,Four,47.11,1.11\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\nL,A,2,Late,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,WK,T1,0\nL,LATE,T2,0\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T1,08:00:00,08:00:00,S1,1\nT1,08:20:00,08:20:00,S2,2\n"
                       "T2,09:00:00,09:00:00,S3,1\nT2,09:20:00,09:20:00,S4,2\n"),
    # weekdays through June 2026; LATE runs in October only, out of reach
    # of a question asked before July 3rd
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nWK,1,1,1,1,1,0,0,20260601,20260630\n"
                     "LATE,1,1,1,1,1,1,1,20261001,20261031\n"),
    # Monday June 15th removed, Saturday June 20th added
    "calendar_dates.txt": "service_id,date,exception_type\nWK,20260615,2\nWK,20260620,1\n",
}

CALENDAR_CASES = [  # route, origin, destination, asked, answer
    ("R", "S1", "S2", "2026-06-12", "2026-06-12"),   # a Friday runs by its flag
    ("R", "S1", "S2", "2026-06-13", "2026-06-16"),   # past the weekend and the removed Monday
    ("R", "S1", "S2", "2026-06-20", "2026-06-20"),   # a Saturday added
    ("R", "S1", "S2", "2026-06-21", "2026-06-22"),   # a Sunday rests
    ("R", "S1", "S2", "2026-06-30", "2026-06-30"),   # the last day of validity
    ("R", "S1", "S2", "2026-07-01", None),           # past the validity
    ("R", "S2", "S1", "2026-06-12", None),           # the wrong way round
    ("L", "S3", "S4", "2026-07-02", None),           # October is 91 days on
    ("L", "S3", "S4", "2026-07-03", "2026-10-01"),   # and 90 days on is in reach
]


@pytest.fixture(scope="module")
def calendar_feed(tmp_path_factory):
    root = tmp_path_factory.mktemp("calendar")
    with zipfile.ZipFile(root / "static.zip", "w") as zout:
        for name, body in CALENDAR_FEED.items():
            zout.writestr(name, body)
    schedule = fixture_db.build(str(root))
    return schedule, Feed(root / "static.zip", schedule)


@pytest.mark.parametrize("route_id, origin, destination, asked, answer", CALENDAR_CASES,
                         ids=[f"{c[1]}-{c[2]}-{c[3]}" for c in CALENDAR_CASES])
def test_calendar_weekdays_removals_additions_and_the_horizon(calendar_feed, route_id, origin,
                                                              destination, asked, answer):
    schedule, feed = calendar_feed
    # the literal answer and the zip agree, so the table cannot drift
    assert feed.next_service(route_id, origin, destination, datetime.date.fromisoformat(asked)) == answer
    got = gtfs_helper.get_next_service_date(schedule, origin, destination, asked, "3", route=route_id)
    assert got == answer


def test_a_loops_way_round_holds_the_answer_to_it(calendar_feed):
    # the only trip of R runs direction 0: asked for the other way round,
    # at a loop's terminus, it does not count
    schedule, _ = calendar_feed
    assert gtfs_helper.get_next_service_date(schedule, "S1", "S2", "2026-06-12", "3",
                                             route="R", direction="0") == "2026-06-12"
    assert gtfs_helper.get_next_service_date(schedule, "S1", "S2", "2026-06-12", "3",
                                             route="R", direction="1") is None


@pytest.mark.parametrize("schedule", [None, "extracting"], ids=["none", "sentinel"])
def test_an_unusable_datasource_has_no_next_date(schedule):
    # get_gtfs hands back None or a sentinel string when the datasource
    # cannot be read: no date, and no query sent to it
    assert gtfs_helper.get_next_service_date(schedule, "S1", "S2", "2026-06-12") is None


def test_a_query_that_fails_gives_no_date_rather_than_an_error():
    # the date only enriches an attribute: a failing read must not break
    # the update that asked for it
    def refuse():
        raise RuntimeError("database is locked")
    broken = types.SimpleNamespace(engine=types.SimpleNamespace(connect=refuse))
    assert gtfs_helper.get_next_service_date(broken, "S1", "S2", "2026-06-12") is None


# --- outcomes -------------------------------------------------------------------

OUTCOMES = [
    # outcome, route, origin, destination, now (Paris), departure in hand,
    # date the coordinator finds when it has none, the sensor's attributes
    ("tomorrow_at", L40, QUAI_C, QUAI_E, "2026-08-24 23:50",
     "2026-08-25 06:35:01", None,
     {"next_service_date": "2026-08-25", "next_service_in_days": 1,
      "info": "Next departures tomorrow at 06:35"}),
    ("later", LN, CITE_U, INTERIVES, "2026-08-24 12:00",
     "2026-09-03 00:30:01", None,
     {"next_service_date": "2026-09-03", "next_service_in_days": 10,
      "info": "No departures until 2026-09-03"}),
    ("today", L40, QUAI_C, QUAI_E, "2026-08-28 23:50",
     None, "2026-08-28",
     {"next_service_date": "2026-08-28", "next_service_in_days": 0,
      "info": "No more departures today"}),
    ("none_after_the_last_day", L40, QUAI_C, QUAI_E, "2026-08-29 10:00",
     None, None,
     {"next_service_in_days": -1, "info": "No scheduled departures"}),
    ("none_the_wrong_way", L40, QUAI_D, QUAI_E, "2026-08-25 10:00",
     None, None,
     {"next_service_in_days": -1, "info": "No scheduled departures"}),
]


@pytest.mark.parametrize("outcome, route_id, origin, destination, now, departure, found, attributes",
                         OUTCOMES, ids=[o[0] for o in OUTCOMES])
def test_each_outcome_down_the_sensors_path(tao, outcome, route_id, origin, destination,
                                            now, departure, found, attributes):
    schedule, feed = tao
    instant = datetime.datetime.fromisoformat(now).replace(tzinfo=PARIS)
    expected_departure = (datetime.datetime.fromisoformat(departure).replace(tzinfo=PARIS)
                          if departure else None)
    # the table's facts, read from the zip
    assert feed.next_departure(route_id, origin, destination, instant) == expected_departure
    if departure is None:
        assert feed.next_service(route_id, origin, destination, instant.date()) == found

    data = _data(schedule, feed, route_id, origin, destination)
    with freeze_time(instant.astimezone(datetime.timezone.utc)):
        next_departure = gtfs_helper.get_next_departure(_hass(), data)
        state = next_departure.get("departure_time") if next_departure else None
        assert state == expected_departure
        next_service = None
        if not next_departure:
            # what the coordinator asks when the departures hold nothing
            next_service = asyncio.run(refresh_steps.next_service_date_for(
                _hass(), schedule, data, data["offset"]))
            assert next_service == found
        written = {}
        departure_attributes.next_service_info(written, state, next_service, data["offset"])
    assert written == attributes


@pytest.mark.parametrize("outcome, route_id, now, asked_from, attributes", [
    ("tomorrow", LN, "2026-09-02 12:00", "2026-09-02",
     {"next_service_date": "2026-09-03", "next_service_in_days": 1,
      "info": "No departures until 2026-09-03"}),
    ("later", LN, "2026-08-24 12:00", "2026-08-24",
     {"next_service_date": "2026-09-03", "next_service_in_days": 10,
      "info": "No departures until 2026-09-03"}),
], ids=["tomorrow", "later"])
def test_a_date_and_no_departure_in_hand(tao, outcome, route_id, now, asked_from, attributes):
    # The departure read reaches every day ahead, so on the line path it
    # holds tomorrow's or a later departure itself (tomorrow_at, later
    # above). The branch without one is what the sensor shows when the
    # coordinator holds a date and no departure: handed here the date the
    # zip and the component agree on.
    schedule, feed = tao
    found = gtfs_helper.get_next_service_date(schedule, CITE_U, INTERIVES, asked_from,
                                              "3", route=route_id)
    assert found == feed.next_service(route_id, CITE_U, INTERIVES,
                                      datetime.date.fromisoformat(asked_from))
    instant = datetime.datetime.fromisoformat(now).replace(tzinfo=PARIS)
    with freeze_time(instant.astimezone(datetime.timezone.utc)):
        written = {}
        departure_attributes.next_service_info(written, None, found, 0)
    assert written == attributes
