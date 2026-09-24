"""When the realtime feeds are read: the polling window a timetable gives.

The window of a service day runs from its first passage minus ten minutes
to its last passage plus twenty, over every stop time of the services that
run that day. GTFS hours pass 24, so a day ending at 25:30 keeps its feed
read until 01:50 the next civil day, the case a window bounded by the civil
day gets wrong. At the close, a feed still announcing a future stop for a
followed line keeps the window open ten minutes per check, two hours at
most; a feed that only repeats past stops (a parked vehicle republished all
night) does not.

The promises, on a small database laid out the way a datasource stores it
(stop times as datetimes on the 1970 epoch, a time past midnight landing on
1970-01-02, dates as YYYY-MM-DD):

    envelope     a day's envelope is its first and last passage, past
                 midnight included; frequencies stretch it to their span;
                 a calendar weekday has one, the same weekday removed by an
                 exception has none, and a day without service has none
    day window   closed before the first passage minus ten minutes, open
                 from there and through the day, on a calendar_dates day
                 and on a calendar weekday alike
    midnight     yesterday's window still holds past midnight, publishes
                 its end, and closes on time on a day without service
    extension    a future stop of a followed line stretches the window by
                 ten minutes and says until when; past stops alone keep only
                 the tail of the last stretch; another line's stop does not
                 count, unless the source follows no line (a train entry)
    cap          the stretches stop two hours past the close, whatever the
                 feed says, and the reason is published
    diagnostic   the state published before the opening names the reason
                 and the window to come
    fail-open    a timetable that cannot be read leaves realtime on
    zone         the window is read on the agency's clock, not the server's
"""
from __future__ import annotations

import datetime
import sqlite3
import time
import types

import pytest
import sqlalchemy

import ha_stub

rt_window = ha_stub.load("rt_window")
gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

FILE = "winnet"
URL = "http://example.org/trip-updates"
TZ = datetime.timezone(datetime.timedelta(hours=2))

# DAY runs 05:00 to 25:30 (window 04:50 to 01:50 the next day), DAY_AFTER
# rests, FREQ_DAY runs on frequencies, fridays run 07:00 to 19:00 by
# calendar except CAL_REMOVED_DAY
DAY = "2026-09-01"
DAY_AFTER = "2026-09-02"
FREQ_DAY = "2026-09-03"
CAL_DAY = "2026-09-04"
CAL_REMOVED_DAY = "2026-09-11"


def _t(hms, day=1):
    """A stop time the way the datasource stores it."""
    return f"1970-01-0{day} {hms}.000000"


def _build_db(path, agency_zone=None):
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table calendar(service_id text, monday int, tuesday int,
            wednesday int, thursday int, friday int, saturday int, sunday int,
            start_date date, end_date date);
        create table calendar_dates(service_id text, date date, exception_type int);
        create table trips(trip_id text, route_id text, service_id text);
        create table stop_times(trip_id text, stop_sequence int,
            arrival_time timestamp, departure_time timestamp);
        create table frequencies(trip_id text, start_time timestamp, end_time timestamp);
    """)
    if agency_zone:
        conn.execute("create table agency(agency_id text, agency_timezone text)")
        conn.execute("insert into agency values ('A', ?)", (agency_zone,))
    conn.execute("insert into calendar_dates values ('wk', ?, 1)", (DAY,))
    conn.execute("insert into trips values ('t1', 'L1', 'wk')")
    conn.executemany("insert into stop_times values (?, ?, ?, ?)", [
        ("t1", 1, _t("05:00:00"), _t("05:00:00")),
        ("t1", 2, _t("12:00:00"), _t("12:01:00")),
        ("t1", 3, _t("01:30:00", 2), _t("01:30:00", 2)),
    ])
    # frequency-based service: the template stop_times only span
    # 08:00-08:30, the day really runs 06:00 to 26:00
    conn.execute("insert into calendar_dates values ('fr', ?, 1)", (FREQ_DAY,))
    conn.execute("insert into trips values ('t2', 'L1', 'fr')")
    conn.execute("insert into stop_times values ('t2', 1, ?, ?)",
                 (_t("08:00:00"), _t("08:00:00")))
    conn.execute("insert into stop_times values ('t2', 2, ?, ?)",
                 (_t("08:30:00"), _t("08:30:00")))
    conn.execute("insert into frequencies values ('t2', ?, ?)",
                 (_t("06:00:00"), _t("02:00:00", 2)))
    # calendar-shape service, fridays only, one friday removed
    conn.execute("insert into calendar values "
                 "('cal', 0, 0, 0, 0, 1, 0, 0, '2026-08-01', '2026-12-31')")
    conn.execute("insert into trips values ('t3', 'L1', 'cal')")
    conn.execute("insert into stop_times values ('t3', 1, ?, ?)",
                 (_t("07:00:00"), _t("07:00:00")))
    conn.execute("insert into stop_times values ('t3', 2, ?, ?)",
                 (_t("19:00:00"), _t("19:00:00")))
    conn.execute("insert into calendar_dates values ('cal', ?, 2)",
                 (CAL_REMOVED_DAY,))
    conn.commit()
    conn.close()


def _schedule(path):
    return types.SimpleNamespace(engine=sqlalchemy.create_engine(f"sqlite:///{path}"))


class _Entries:
    def __init__(self, entries):
        self._entries = entries

    def async_entries(self, domain=None):
        return list(self._entries)


def _hass(route="L1: Ligne 1"):
    # no config: the database's edition then reads "unknown", one edition
    # for the whole test, which is what a cache keyed on it needs here
    return types.SimpleNamespace(config_entries=_Entries([
        types.SimpleNamespace(data={"file": FILE, "route": route}, options={})]))


def _at(day, hms):
    """An aware instant of the local day, e.g. _at('2026-09-02', '01:40')."""
    return datetime.datetime.fromisoformat(f"{day} {hms}:00").replace(tzinfo=TZ)


def _feed(now, offset_seconds, route="L1"):
    """One trip update in the realtime cache, its stop offset from now."""
    when = int(now.timestamp()) + offset_seconds
    gtfs_rt_helper._FEED_CACHE[(FILE, URL, "trip_data")] = (time.time(), [{
        "id": "e1",
        "trip_update": {
            "trip": {"trip_id": "t1", "route_id": route},
            "stop_time_update": [{
                "stop_id": "s1", "stop_sequence": 3,
                "arrival": {"time": when}, "departure": {"time": when},
            }],
        },
    }])


@pytest.fixture
def schedule(tmp_path):
    path = tmp_path / f"{FILE}.sqlite"
    _build_db(str(path))
    engine_holder = _schedule(path)
    yield engine_holder
    engine_holder.engine.dispose()


@pytest.fixture(autouse=True)
def fresh_state():
    """Every test starts with nothing decided and nothing cached, and
    leaves it so: the window's caches are module-wide."""
    def clear():
        rt_window._STATE.clear()
        rt_window._ENVELOPES.clear()
        rt_window._ZONES.clear()
        gtfs_rt_helper._FEED_CACHE.pop((FILE, URL, "trip_data"), None)
    clear()
    yield
    clear()


def _gate(schedule, now, hass=None):
    return rt_window.rt_window_gate(hass or _hass(), FILE, schedule, URL, now=now)


# --- envelope -----------------------------------------------------------------

@pytest.mark.parametrize("day, envelope", [
    (DAY, (5 * 3600, 25 * 3600 + 30 * 60)),     # a day running past midnight
    (DAY_AFTER, None),                           # a day without service
    (FREQ_DAY, (6 * 3600, 26 * 3600)),           # frequencies, not the template
    (CAL_DAY, (7 * 3600, 19 * 3600)),            # a calendar weekday
    (CAL_REMOVED_DAY, None),                     # that weekday, removed
], ids=["past_midnight", "no_service", "frequencies", "calendar_friday", "removed_friday"])
def test_a_days_envelope_is_its_first_and_last_passage(schedule, day, envelope):
    assert rt_window._service_envelope(schedule, day) == envelope


# --- day window ---------------------------------------------------------------

def test_the_window_is_closed_before_the_first_passage_less_ten_minutes(schedule):
    assert _gate(schedule, _at(DAY, "04:30")) == "out_of_window"


@pytest.mark.parametrize("day, hms", [
    (DAY, "04:55"),        # inside the ten minutes' lead
    (DAY, "12:00"),        # the middle of a calendar_dates day
    (CAL_DAY, "12:00"),    # the middle of a calendar weekday
], ids=["lead_margin", "midday", "calendar_midday"])
def test_the_window_is_open_from_the_lead_and_through_the_day(schedule, day, hms):
    assert _gate(schedule, _at(day, hms)) is None


# --- midnight -----------------------------------------------------------------

def test_yesterdays_window_holds_past_midnight_and_closes_on_time(schedule):
    # the day ends at 25:30, so the next civil day still reads the feed
    # until 01:50, and says so
    assert _gate(schedule, _at(DAY_AFTER, "01:40")) is None
    assert rt_window.window_state(FILE).get("window_end") == f"{DAY_AFTER}T01:50:00"
    # and 02:00 on a day without service of its own is closed
    assert _gate(schedule, _at(DAY_AFTER, "02:00")) == "no_service_today"


# --- extension ----------------------------------------------------------------

def test_a_future_stop_of_a_followed_line_stretches_the_window(schedule):
    now = _at(DAY_AFTER, "02:00")
    _feed(now, +600)
    assert _gate(schedule, now) is None
    assert rt_window.window_state(FILE).get("extended_until") == f"{DAY_AFTER}T02:10:00"


def test_past_stops_alone_keep_only_the_tail_of_the_last_stretch(schedule):
    now = _at(DAY_AFTER, "02:00")
    _feed(now, +600)
    assert _gate(schedule, now) is None
    # the vehicle's stops are behind it now: the ten minutes already
    # granted still hold, nothing more
    _feed(now, -600)
    assert _gate(schedule, _at(DAY_AFTER, "02:05")) is None
    assert _gate(schedule, _at(DAY_AFTER, "02:12")) == "no_service_today"


def test_another_lines_future_stop_does_not_stretch_the_window(schedule):
    now = _at(DAY_AFTER, "02:00")
    _feed(now, +600, route="L9")
    assert _gate(schedule, now) == "no_service_today"


def test_a_source_following_no_line_hears_every_line(schedule):
    # a train entry stores "train" instead of a route id: the activity
    # check listens to the whole feed rather than going deaf
    now = _at(DAY_AFTER, "02:00")
    _feed(now, +600, route="L9")
    assert _gate(schedule, now, hass=_hass(route="train")) is None


# --- cap ----------------------------------------------------------------------

def test_the_stretches_stop_two_hours_past_the_close(schedule):
    # the close is 01:50, so the cap is 03:50
    now = _at(DAY_AFTER, "03:45")
    _feed(now, +600)
    assert _gate(schedule, now) is None
    assert rt_window.window_state(FILE).get("extended_until") == f"{DAY_AFTER}T03:50:00"
    now = _at(DAY_AFTER, "03:55")
    _feed(now, +600)
    assert _gate(schedule, now) == "overtime_cap"
    assert rt_window.window_state(FILE).get("paused") == "overtime_cap"


# --- diagnostic ---------------------------------------------------------------

def test_the_state_before_the_opening_names_the_reason_and_the_window(schedule):
    _gate(schedule, _at(DAY, "04:30"))
    state = rt_window.window_state(FILE)
    assert state.get("paused") == "out_of_window"
    assert state.get("window_start") == f"{DAY}T04:50:00"


# --- fail-open ----------------------------------------------------------------

def test_a_timetable_that_cannot_be_read_leaves_realtime_on(tmp_path):
    # a database without the calendar tables: the query raises, and the
    # gate only silences what it positively knows is asleep
    path = tmp_path / "broken.sqlite"
    sqlite3.connect(str(path)).close()
    broken = _schedule(path)
    try:
        assert _gate(broken, _at(DAY, "04:30")) is None
        assert rt_window.window_state(FILE).get("paused") is None
    finally:
        broken.engine.dispose()


# --- zone ---------------------------------------------------------------------

def test_the_window_is_read_on_the_agencys_clock(tmp_path):
    # the same timetable, its agency writing in UTC: 06:00 at +02:00 is
    # 04:00 there, before the 04:50 opening, where the server's own clock
    # would read 06:00 and open; an hour later it is 05:00 there, open
    path = tmp_path / f"{FILE}.sqlite"
    _build_db(str(path), agency_zone="UTC")
    utc_schedule = _schedule(path)
    try:
        assert _gate(utc_schedule, _at(DAY, "06:00")) == "out_of_window"
        assert _gate(utc_schedule, _at(DAY, "07:00")) is None
    finally:
        utc_schedule.engine.dispose()
