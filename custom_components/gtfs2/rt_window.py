"""Automatic realtime polling windows, derived from the timetable.

The integration owns the timetable, so nobody has to schedule the polling:
per source and per service day, the feeds are only read between the first
passage of the day minus 10 minutes and the last minus none plus 20 (the
margin only has to cover delay, since the envelope already includes the
terminus arrivals). The envelope spans every line the source carries, which
the filtered database makes meaningful: those are the followed lines, not
the whole network.

GTFS hours pass 24, so a service day's window can end after midnight: the
gate always tests yesterday's window besides today's. A day without service
reads nothing at all, which is where the real gain lives - episodic lines
(TAO's 22 runs 122 days a year) and resting night lines stop being polled
without anyone writing an automation.

At the theoretical close the window stretches while the last fetch still
announces a future stop time for a followed line - a late vehicle is the
one moment realtime matters most - by 10 minutes per re-check, capped two
hours past the close. Nothing moves at the start: a service does not leave
early.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

import homeassistant.util.dt as dt_util
from sqlalchemy.sql import text

from .const import CONF_DEVICE_TRACKER_ID, CONF_ROUTE
from .gtfs_rt_helper import cached_feed_has_future_stop
from .rt_source import journey_entries

_LOGGER = logging.getLogger(__name__)

LEAD = timedelta(minutes=10)
TRAIL = timedelta(minutes=20)
EXTEND = timedelta(minutes=10)
OVERTIME_CAP = timedelta(hours=2)

# (file, date) -> (first, last) gtfs seconds of the service day, or None when
# nothing runs; the gate only ever reads yesterday and today, older keys are
# dropped as it goes
_ENVELOPES: dict[tuple[str, str], tuple[int, int] | None] = {}
# per file: what the gate last decided, read back by the diagnostic entity
_STATE: dict[str, dict] = {}

# Both calendar shapes are read, like get_next_service_date: calendar holds
# weekday flags over a validity window, calendar_dates explicit additions and
# removals, and feeds use either (TAO publishes everything through
# calendar_dates).
_ACTIVE_TRIPS_SQL = """
    with active as (
        select service_id from calendar
        where start_date <= date(:d) and end_date >= date(:d)
          and (case cast(strftime('%w', date(:d)) as int)
                 when 0 then sunday   when 1 then monday
                 when 2 then tuesday  when 3 then wednesday
                 when 4 then thursday when 5 then friday
                 else saturday end) = 1
          and not exists (
              select 1 from calendar_dates cx
              where cx.service_id = calendar.service_id
                and cx.date = date(:d) and cx.exception_type = 2)
        union
        select service_id from calendar_dates
        where date = date(:d) and exception_type = 1
    ),
    day_trips as (
        select trip_id from trips
        where service_id in (select service_id from active)
    )
"""

# The stored stop times are datetimes on the epoch, fixed width and zero
# padded, where a time past midnight lands on 1970-01-02: their string order
# IS their time order, so min/max run on the raw column and only the four
# winners are ever parsed. Converting per row (strftime) cost seconds on a
# full network, this costs milliseconds on the same data.
_ENVELOPE_SQL = _ACTIVE_TRIPS_SQL + """
    select min(st.arrival_time), max(st.arrival_time),
           min(st.departure_time), max(st.departure_time)
    from stop_times st join day_trips dt on dt.trip_id = st.trip_id
"""

# an interned database exposes stop_times as a view that resolves every trip
# key per row: a min/max through it took 2.5 s on the full Orleans network,
# against 24 ms when the interned tables are joined on their integer key
_ENVELOPE_SQL_INTERNED = _ACTIVE_TRIPS_SQL + """
    , day_tk as (
        select k.tk from gtfs2_trip_key k
        join day_trips dt on dt.trip_id = k.trip_id
    )
    select min(st.arrival_time), max(st.arrival_time),
           min(st.departure_time), max(st.departure_time)
    from gtfs2_stop_times st join day_tk on day_tk.tk = st.tk
"""

# frequency-based trips carry template stop_times only: the running span of
# the day is in frequencies' start and end
_FREQUENCIES_SQL = _ACTIVE_TRIPS_SQL + """
    select min(f.start_time), max(f.end_time)
    from frequencies f join day_trips dt on dt.trip_id = f.trip_id
"""


def _gtfs_seconds(value):
    """A stored stop time as gtfs seconds since the service day's midnight.

    Reads the epoch-datetime form the importer writes ('1970-01-02 01:30:00'
    is 25:30), and falls back on plain seconds and on bare HH:MM:SS for
    databases another pygtfs build produced.
    """
    if value is None:
        return None
    text_value = str(value)
    try:
        return int(text_value)
    except ValueError:
        pass
    day = 0
    if text_value.startswith("1970-01-"):
        day = int(text_value[8:10]) - 1
        text_value = text_value[11:]
    parts = text_value.split(".")[0].split(":")
    if len(parts) != 3:
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return day * 86400 + hours * 3600 + minutes * 60 + seconds


def _service_envelope(schedule, date_str):
    """(first, last) gtfs second of the service day, or None when it rests."""
    with schedule.engine.connect() as conn:
        interned = conn.execute(text(
            "select 1 from sqlite_master where type = 'table' "
            "and name = 'gtfs2_trip_key'")).fetchone()
        sql = _ENVELOPE_SQL_INTERNED if interned else _ENVELOPE_SQL
        row = conn.execute(text(sql), {"d": date_str}).fetchone()
        bounds = [_gtfs_seconds(v) for v in (row or ())]
        if conn.execute(text(
                "select 1 from sqlite_master where type in ('table', 'view') "
                "and name = 'frequencies'")).fetchone():
            freq = conn.execute(text(_FREQUENCIES_SQL), {"d": date_str}).fetchone()
            bounds += [_gtfs_seconds(v) for v in (freq or ())]
    bounds = [b for b in bounds if b is not None]
    if not bounds:
        return None
    return min(bounds), max(bounds)


def _window_for(file, schedule, day):
    """The polling window of one service day, in naive local time, or None."""
    key = (file, day.isoformat())
    if key not in _ENVELOPES:
        _ENVELOPES[key] = _service_envelope(schedule, day.isoformat())
    envelope = _ENVELOPES[key]
    if envelope is None:
        return None
    midnight = datetime.combine(day, time())
    return (midnight + timedelta(seconds=envelope[0]) - LEAD,
            midnight + timedelta(seconds=envelope[1]) + TRAIL)


def _followed_routes(hass, file):
    """The route ids of the source's journey sensors.

    Train entries store the marker "train" instead of a route id and local
    stop entries none at all: a source carrying only those yields an empty
    set, and the activity check then listens to the whole feed rather than
    going deaf.
    """
    routes = set()
    for entry in journey_entries(hass, file):
        if entry.data.get(CONF_DEVICE_TRACKER_ID):
            continue
        route = (entry.data.get(CONF_ROUTE) or "").split(": ")[0]
        if route and route != "train":
            routes.add(route)
    return routes


def window_state(file):
    """What the gate last decided for a source, for the diagnostic entity."""
    return _STATE.get(file)


def rt_window_gate(hass, file, schedule, trip_update_url, now=None):
    """None when the realtime feeds should be read now, else the pause reason.

    Reasons: out_of_window (today has service, but not now), no_service_today,
    overtime_cap (the two-hour extension budget ran out). Runs in the executor:
    the envelope costs one query per source and per day, cached after that.

    Fail-open: a source whose timetable cannot be read keeps its realtime,
    the gate only silences what it positively knows is asleep.
    """
    try:
        return _gate(hass, file, schedule, trip_update_url, now)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning(
            "Realtime window for %s could not be derived, leaving realtime on: %s",
            file, ex)
        _STATE.setdefault(file, {})["paused"] = None
        return None


def _gate(hass, file, schedule, trip_update_url, now=None):
    now_aware = now or dt_util.now()
    now_local = now_aware.replace(tzinfo=None)
    today = now_local.date()

    cutoff = (today - timedelta(days=1)).isoformat()
    for key in [k for k in _ENVELOPES if k[1] < cutoff]:
        del _ENVELOPES[key]

    win_yesterday = _window_for(file, schedule, today - timedelta(days=1))
    win_today = _window_for(file, schedule, today)
    windows = [w for w in (win_yesterday, win_today) if w]

    state = _STATE.setdefault(file, {})
    state["checked_at"] = now_aware.isoformat()

    current = next((w for w in windows if w[0] <= now_local <= w[1]), None)
    if current:
        state.update(window_start=current[0].isoformat(),
                     window_end=current[1].isoformat(), paused=None)
        # a real window resets the extension budget of the previous close
        state.pop("extended_until", None)
        return None

    closes = [w[1] for w in windows if w[1] < now_local]
    last_close = max(closes) if closes else None
    if last_close is not None:
        cap = last_close + OVERTIME_CAP
        if now_local <= cap:
            if trip_update_url and cached_feed_has_future_stop(
                    file, trip_update_url, _followed_routes(hass, file),
                    now_aware.timestamp()):
                until = min(now_local + EXTEND, cap)
                state.update(extended_until=until.isoformat(),
                             window_start=last_close.isoformat(),
                             window_end=until.isoformat(), paused=None)
                return None
            extended = state.get("extended_until")
            if extended and now_local <= datetime.fromisoformat(extended):
                # the ten-minute tail of the last fetch that showed activity
                state["paused"] = None
                return None
        else:
            extended = state.get("extended_until")
            if extended and datetime.fromisoformat(extended) >= cap:
                # the budget is what ended the run, and says so until the
                # next window opens
                state["paused"] = "overtime_cap"
                return "overtime_cap"

    reason = "out_of_window" if win_today else "no_service_today"
    state.update(
        paused=reason,
        window_start=win_today[0].isoformat() if win_today else None,
        window_end=win_today[1].isoformat() if win_today else None)
    return reason
