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
early. A restart past the close finds no fetch to ask: the feed is read
once then, and the stretch carries on from what that reading says.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import threading
from typing import Any
from datetime import date, datetime, time, timedelta, tzinfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import dispatcher_send
import homeassistant.util.dt as dt_util
from pygtfs import Schedule
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from ..const import DEFAULT_PATH
from ..feed.files import file_edition, real_path
from .clocks import _removed_on, _runs_on, agency_zone, gtfs_seconds
from ..feed.rt_feed import (CANCELLED_TRIP, SKIPPED_STOP, _FEED_CACHE, _same_route, stop_relationship,
                      trip_relationship)
from ..feed.source_entries import source_readers, source_train_lines

_LOGGER = logging.getLogger(__name__)

LEAD = timedelta(minutes=10)
TRAIL = timedelta(minutes=20)
EXTEND = timedelta(minutes=10)
OVERTIME_CAP = timedelta(hours=2)

# what a source's database is, as file_edition says it, or "unknown"
type _Edition = tuple[int, int, int] | str

# (file, edition, date) -> (first, last) gtfs seconds of the service day, or
# None when nothing runs; the gate only ever reads yesterday and today, older
# keys are dropped as it goes. The edition is what the database was when the
# envelope was read (see _edition_of): a refresh, a line added in the flow or
# a prune changes the hours the source runs, and the answer kept from this
# morning would hold the realtime shut on the line added this afternoon.
_ENVELOPES: dict[tuple[str, _Edition, str], tuple[int, int] | None] = {}
# every coordinator runs the gate in an executor thread, on the first cycle
# of the day all at once: one walking the cache to clean it while another
# added to it raised, and each read the same envelope for itself
_ENVELOPES_LOCK = threading.Lock()
# per file: what the gate last decided, read back by the diagnostic entity
_STATE: dict[str, dict[str, str | None]] = {}
# per-source dispatcher signal: the gate's verdict moved, the diagnostic
# entity writes it at once
SIGNAL_RT_WINDOW = "gtfs2_rt_window_{}"

# Both calendar shapes are read, like get_next_service_date: calendar holds
# weekday flags over a validity window, calendar_dates explicit additions and
# removals, and feeds use either (TAO publishes everything through
# calendar_dates).
_ACTIVE_TRIPS_SQL = f"""
    with active as (
        select service_id from calendar
        where start_date <= date(:d) and end_date >= date(:d)
          and {_runs_on("date(:d)")}
          and not {_removed_on("calendar.service_id", "date(:d)")}
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


def _service_envelope(schedule: Schedule, date_str: str) -> tuple[int, int] | None:
    """(first, last) gtfs second of the service day, or None when it rests."""
    with schedule.engine.connect() as conn:
        interned = conn.execute(text(
            "select 1 from sqlite_master where type = 'table' "
            "and name = 'gtfs2_trip_key'")).fetchone()
        sql = _ENVELOPE_SQL_INTERNED if interned else _ENVELOPE_SQL
        row = conn.execute(text(sql), {"d": date_str}).fetchone()
        bounds = [gtfs_seconds(v) for v in (row or ())]
        if conn.execute(text(
                "select 1 from sqlite_master where type in ('table', 'view') "
                "and name = 'frequencies'")).fetchone():
            freq = conn.execute(text(_FREQUENCIES_SQL), {"d": date_str}).fetchone()
            bounds += [gtfs_seconds(v) for v in (freq or ())]
    found = [b for b in bounds if b is not None]
    if not found:
        return None
    return min(found), max(found)


def _edition_of(hass: HomeAssistant, file: str) -> _Edition:
    """What the source's database is right now, as far as a cache cares.

    Which file it is, its size and the moment it was last written
    (file_edition): every writer changes one or the other, whether it swaps
    a rebuilt file in or writes in place, and no writer has to know this
    cache exists. An unreadable file reads
    as its own edition, so the envelope is asked again rather than served
    from an answer about another file.
    """
    try:
        path = real_path(hass.config.path(DEFAULT_PATH), file)
    except AttributeError:
        path = None
    # no such file, or a caller holding a schedule and no config at all
    # (the offline harnesses): one edition for them all, the cache then
    # behaves as it did before this was read
    return file_edition(path) or "unknown"


# the zone a source's clocks are written in, by (file, edition): one small
# query per source, asked again when the database is rebuilt
_ZONES: dict[tuple[str, _Edition], tzinfo | None] = {}


def _feed_zone(hass: HomeAssistant, file: str, schedule: Schedule) -> tzinfo | None:
    """The zone the source's timetable is written in: its agency's.

    The envelope is made of the feed's own clocks, so the moment to
    compare them with is the local one where the network runs. Read on
    Home Assistant's clock instead, a network an hour away had its
    realtime cut while its buses were still out, or polled for an hour
    after the last one was in. Falls back on Home Assistant's zone, which
    is what a network at home is anyway.
    """
    key = (file, _edition_of(hass, file))
    if key not in _ZONES:
        _ZONES[key] = agency_zone(schedule)
        _LOGGER.debug("Realtime window of %s reads the clocks of %s", file, _ZONES[key] or "this server")
    return _ZONES[key]


def _window_for(hass: HomeAssistant, file: str, schedule: Schedule,
                day: date) -> tuple[datetime, datetime] | None:
    """The polling window of one service day, in naive local time, or None."""
    key = (file, _edition_of(hass, file), day.isoformat())
    with _ENVELOPES_LOCK:
        if key not in _ENVELOPES:
            _ENVELOPES[key] = _service_envelope(schedule, day.isoformat())
        envelope = _ENVELOPES[key]
    if envelope is None:
        return None
    midnight = datetime.combine(day, time())
    return (midnight + timedelta(seconds=envelope[0]) - LEAD,
            midnight + timedelta(seconds=envelope[1]) + TRAIL)


def window_state(file: str) -> dict[str, str | None] | None:
    """What the gate last decided for a source, for the diagnostic entity."""
    return _STATE.get(file)


def rt_window_gate(hass: HomeAssistant, file: str, schedule: Schedule,
                   trip_update_url: str | None, now: datetime | None = None) -> str | None:
    """None when the realtime feeds should be read now, else the pause reason.

    Reasons: out_of_window (today has service, but not now), no_service_today,
    overtime_cap (the two-hour extension budget ran out). Runs in the executor:
    the envelope costs one query per source and per day, cached after that.

    Fail-open: a source whose timetable cannot be read keeps its realtime,
    the gate only silences what it positively knows is asleep.
    """
    before = _verdict(file)
    paused = _decide(hass, file, schedule, trip_update_url, now)
    if _verdict(file) != before:
        # the diagnostic entity hears of it now: read at its next poll only,
        # it said "unknown" for up to half a minute after a start
        dispatcher_send(hass, SIGNAL_RT_WINDOW.format(file))
    return paused


def _verdict(file: str) -> tuple[str | None, ...] | None:
    """What the diagnostic entity shows of the gate's last decision, the
    time of the check aside."""
    state = _STATE.get(file)
    if state is None:
        return None
    return tuple(state.get(k) for k in ("paused", "window_start", "window_end", "extended_until"))


def _decide(hass: HomeAssistant, file: str, schedule: Schedule,
            trip_update_url: str | None, now: datetime | None) -> str | None:
    """rt_window_gate's answer, its state noted for the diagnostic entity."""
    if schedule is None or isinstance(schedule, str):
        # a sentinel of get_gtfs: no timetable to derive a window from
        _STATE.setdefault(file, {})["paused"] = None
        return None
    try:
        paused = _gate(hass, file, schedule, trip_update_url, now)
    except Exception as ex:  # pylint: disable=broad-except
        # a database that cannot answer is said in a line, a mistake with
        # where it happened
        log = _LOGGER.warning if isinstance(ex, SQLAlchemyError) else _LOGGER.exception
        log("Realtime window for %s could not be derived, leaving realtime on: %s",
            file, ex)
        _STATE.setdefault(file, {})["paused"] = None
        return None
    if paused:
        _LOGGER.debug("GTFS RT: %s is outside its service window (%s), feeds not read",
                      file, paused)
    return paused


def _forget_envelopes(file: str, edition: _Edition, cutoff: str) -> None:
    """Drop the envelopes of a day gone, and those of this source read from
    a database it no longer has."""
    with _ENVELOPES_LOCK:
        for key in [k for k in _ENVELOPES
                    if k[2] < cutoff or (k[0] == file and k[1] != edition)]:
            # the day is gone, or the database it was read from is
            del _ENVELOPES[key]


def _after_close(hass: HomeAssistant, file: str, trip_update_url: str | None,
                 state: dict[str, str | None], now_aware: datetime, now_local: datetime,
                 last_close: datetime) -> str | None:
    """Past the last window's close: None while a vehicle still under way,
    or the tail of the last stretch, keeps the feeds read; "overtime_cap"
    once the stretches ran out their budget; "closed" when nothing keeps
    them open."""
    cap = last_close + OVERTIME_CAP
    if now_local <= cap:
        # the lines the source's sensors name; a source read whole by
        # one of them, a train or local stops sensor, names none, and the
        # check then listens to the whole feed rather than going deaf. A
        # train sensor holding to its codes names no route_id either: the
        # feed's trips may run on route_ids it never had (or none, SNCF)
        routes, whole = source_readers(hass, file)
        whole = whole or bool(source_train_lines(hass, file))
        if trip_update_url and cached_feed_has_future_stop(
                file, trip_update_url, set() if whole else routes,
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
        if (trip_update_url and (file, trip_update_url, "trip_data") not in _FEED_CACHE
                and state.get("read_after_close") != last_close.isoformat()):
            # nothing read since a restart: the cache that tells whether a
            # late train still runs is gone, and closed on it the feeds
            # stayed unread until the morning (SNCF, 2026-10-05: a TGV two
            # hours late, a restart at 22:43). One reading tells, once a close
            state["read_after_close"] = last_close.isoformat()
            state["paused"] = None
            return None
    else:
        extended = state.get("extended_until")
        if extended and datetime.fromisoformat(extended) >= cap:
            # the budget is what ended the run, and says so until the
            # next window opens
            state["paused"] = "overtime_cap"
            return "overtime_cap"
    return "closed"


def _gate(hass: HomeAssistant, file: str, schedule: Schedule,
          trip_update_url: str | None, now: datetime | None = None) -> str | None:
    now_aware = now or dt_util.now()
    zone = _feed_zone(hass, file, schedule)
    if zone is not None:
        # the envelope is the feed's own clocks: read the moment in the
        # zone they are written in, not in this server's
        now_aware = now_aware.astimezone(zone)
    now_local = now_aware.replace(tzinfo=None)
    today = now_local.date()

    _forget_envelopes(file, _edition_of(hass, file), (today - timedelta(days=1)).isoformat())

    win_yesterday = _window_for(hass, file, schedule, today - timedelta(days=1))
    win_today = _window_for(hass, file, schedule, today)
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
    if closes:
        verdict = _after_close(hass, file, trip_update_url, state, now_aware, now_local, max(closes))
        if verdict != "closed":
            return verdict

    reason = "out_of_window" if win_today else "no_service_today"
    state.update(
        paused=reason,
        window_start=win_today[0].isoformat() if win_today else None,
        window_end=win_today[1].isoformat() if win_today else None)
    return reason


def _followed_update(entity: object, routes: Iterable[str]) -> Mapping[str, Any]:
    """The trip update of a cached entity a sensor may follow, empty when
    there is none: not a trip update, a cancelled trip, or a line named
    that no sensor follows."""
    if not isinstance(entity, dict):
        return {}
    trip_update = entity.get("trip_update") or {}
    # a cancelled trip is no vehicle under way, as the sensor drops it
    if trip_relationship(entity) in CANCELLED_TRIP:
        return {}
    seen = (trip_update.get("trip") or {}).get("route_id")
    # a trip naming no line (SNCF) may be one the sensor follows by its
    # trip id: only a line named and not followed is left out
    if routes and seen and not any(_same_route(route, seen) for route in routes):
        return {}
    return trip_update


def cached_feed_has_future_stop(owner: str, url: str, routes: Iterable[str],
                                now_epoch: float) -> bool:
    """Whether the last cached trip-updates fetch still announces a stop time
    in the future for one of the routes (any route, when none are named).

    Feeds the automatic polling window: at its theoretical close, a vehicle
    still under way keeps the window open a little longer. The decision rests
    on a future stop time and nothing else - not on a delay field, which some
    feeds never fill, and not on the mere presence of a vehicle, because a
    parked one republished all night is exactly what this must not mistake
    for service (the map's stale-feed lesson).

    Reads the cache only, never fetches: deciding whether to keep polling
    must not itself poll. An empty cache answers no.
    """
    cached = _FEED_CACHE.get((owner, url, "trip_data"))
    if not cached:
        return False
    for entity in cached[1] or []:
        trip_update = _followed_update(entity, routes)
        for stop in trip_update.get("stop_time_update") or []:
            if stop_relationship(stop) == SKIPPED_STOP:
                continue
            # a json feed writes int64 as text, as the departure reader knows
            try:
                when = max(int((stop.get("arrival") or {}).get("time") or 0),
                           int((stop.get("departure") or {}).get("time") or 0))
            except (TypeError, ValueError):
                continue
            if when > now_epoch:
                return True
    return False
