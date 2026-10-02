"""Which trips of a realtime feed an entity follows, and when the timetable
has them: whether a feed's trip id names a watched trip (_names_trip), the
route and way a trip update rides (_trip_group_route_direction,
_follows_trip), and the scheduled times a delay is laid on, for the trips
on the board (_scheduled_departures) and those that left it
(_scheduled_off_board).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, tzinfo
import json
import logging
from typing import Any

import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text as sql_text

from .rt_feed import FeedEntities, _Coordinator, _same_route, stop_update_clock

_LOGGER = logging.getLogger(__name__)


def _as_epoch(value: object) -> int | None:
    """A departure time, as the coordinator publishes it, in epoch seconds."""
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        return None


def _scheduled_departures(self: _Coordinator) -> dict[str, int]:
    """When the board's trips are due at the entity's stop, by trip id.

    Read off the departure the coordinator already holds: the next one and
    the ones listed behind it. A feed is free to publish a delay and no
    time at all, which the spec allows and plenty of them do; laid on the
    time the timetable announces, that delay is a departure like any other.
    """
    due: dict[str, int] = {}
    departure = (getattr(self, "_data", None) or {}).get("next_departure") or {}
    for trip, when in zip(departure.get("next_departures_trip_id") or [],
                          departure.get("next_departures") or []):
        stamp = _as_epoch(when)
        if trip and stamp:
            due.setdefault(str(trip), stamp)
    stamp = _as_epoch(departure.get("departure_time"))
    if departure.get("trip_id") and stamp:
        due.setdefault(str(departure["trip_id"]), stamp)
    return due


def _off_board_times(self: _Coordinator, feed_entities: FeedEntities,
                     scheduled: Mapping[str, int]) -> dict[str, tuple[str | None, int]]:
    """{trip_id: (the service day the feed names or None, the time it
    gives at this entity's stop)} of the followed trips the board does not
    list."""
    found = {}
    for entity in feed_entities:
        if not entity.get("trip_update"):
            continue
        trip = entity["trip_update"]["trip"]
        group, route_id, direction_id = _trip_group_route_direction(self, trip)
        trip_id = trip.get("trip_id") or ""
        if (not trip_id or trip_id in scheduled
                or not _follows_trip(self, group, route_id, direction_id, trip_id, entity.get("id") or "")):
            continue
        for stop in entity["trip_update"].get("stop_time_update") or []:
            if (stop.get("stop_id") or "") == self._stop_id:
                when, _delay = stop_update_clock(stop)
                if when:
                    found[trip_id] = (trip.get("start_date") or None, when)
                break
    return found


def _due_on_its_day(start_date: str | None, seconds: int, near: int, zone: tzinfo) -> int:
    """Epoch seconds of a stop time `seconds` past its service day's
    midnight, on the day the feed names (YYYYMMDD), else on the day before,
    the day or the day after `near` that puts it nearest to `near`."""
    if start_date:
        try:
            days = [datetime.strptime(str(start_date), "%Y%m%d").date()]
        except ValueError:
            days = []
    else:
        days = []
    if not days:
        today = datetime.fromtimestamp(near, zone).date()
        days = [today - timedelta(days=1), today, today + timedelta(days=1)]
    due = [int((datetime.combine(day, datetime.min.time(), tzinfo=zone) + timedelta(seconds=seconds)).timestamp())
           for day in days]
    return min(due, key=lambda when: abs(when - near))


def _scheduled_off_board(self: _Coordinator, feed_entities: FeedEntities,
                         scheduled: Mapping[str, int]) -> dict[str, int]:
    """When the timetable has the followed trips the board does not list,
    at this entity's stop, by trip id, epoch seconds.

    The board lists the departures still to come by the timetable: a train
    late past its own time has left it while the feed still announces it,
    and its delay was then the feed's alone, 0 on IDFM for a train four
    minutes late. Read from the database for those trips only.
    """
    realtime = _off_board_times(self, feed_entities, scheduled)
    schedule = (getattr(self, "_data", None) or {}).get("schedule")
    if not realtime or schedule is None or not hasattr(schedule, "engine"):
        return {}
    sql = """
    SELECT trip_id, (julianday(departure_time) - julianday('1970-01-01')) * 86400
    FROM stop_times
    WHERE stop_id = :stop AND trip_id IN (SELECT value FROM json_each(:trips))
    """
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(sql_text(sql), {"stop": self._stop_id,
                                                "trips": json.dumps(sorted(realtime))}).fetchall()
    except SQLAlchemyError as ex:
        _LOGGER.debug("Could not read the timetable of the trips off the board: %s", ex)
        return {}
    # the clocks the board's own departures are written in
    shown = (self._data.get("next_departure") or {}).get("departure_time")
    zone = getattr(shown, "tzinfo", None) or dt_util.DEFAULT_TIME_ZONE
    found = {}
    for trip_id, seconds in rows:
        if seconds is not None and str(trip_id) in realtime:
            start_date, when = realtime[str(trip_id)]
            found[str(trip_id)] = _due_on_its_day(start_date, round(seconds), when, zone)
    return found


def _names_trip(watched: str | None, seen: str | None) -> bool:
    """Whether a realtime trip id names the trip being watched.

    Exact, or the watched id standing whole inside a longer one, between
    separators: a feed may qualify its ids with an agency before or a date
    after, which is why a containment test was used at all. Plain, that
    test let trip 100 take the delays of trip 2100, or of 1005, calling at
    the same stop.
    """
    watched, seen = str(watched or ""), str(seen or "")
    if not watched or not seen:
        return False
    if watched == seen:
        return True
    start = seen.find(watched)
    while start != -1:
        end = start + len(watched)
        before = seen[start - 1] if start else ""
        after = seen[end] if end < len(seen) else ""
        if not before.isalnum() and not after.isalnum():
            return True
        start = seen.find(watched, start + 1)
    return False


def _feed_route_id(self: _Coordinator, trip: Mapping[str, Any]) -> str:
    ''' The line a trip update names, cut at the source's delimiter '''
    # a json feed leaves out what it does not know, where the
    # protobuf reader writes every field: the line, the stop, the
    # arrival of a first stop are read with their defaults
    feed_route_id = trip.get("route_id") or ""
    # If delimiter specified split the route ID in the gtfs rt feed
    if self._route_delimiter is not None:
        route_id_split = feed_route_id.split(
            self._route_delimiter
        )
        if route_id_split[0] == self._route_delimiter:
            return feed_route_id
        return route_id_split[0]
    return feed_route_id


def _trip_group_route_direction(self: _Coordinator, trip: Mapping[str, Any]) -> tuple[str, str, str]:
    ''' How a trip update is matched (route or trip), its line and direction '''
    route_id = _feed_route_id(self, trip)

    if trip.get("direction_id") not in ("", None):
        # text, as the protobuf converter writes it and the sensor asks for
        # it: a json feed writes the number, and the departures it gave were
        # filed under 0 where the sensor looked for "0"
        direction_id = str(trip["direction_id"])
    else:
        direction_id = "nn"

    # for route-based requests, if the rt-data has no route (ex. TER) then the selection should be on matching trip_id or matching RT-id with short_name (ex. MTA Metro North RR)
    # result will be that only one RT value will be collected
    # how THIS entity can be matched, not how the sensor asks: an
    # entity naming no line (a TER, a SIRI feed) can only be read by
    # trip, and that used to be written on the coordinator, so every
    # entity read after it was matched by trip too. On a feed that
    # never names its lines the board then kept its head trip alone
    group = self._rt_group
    if not route_id:
        group = "trip"
        route_id = self._route_id

    if group == "trip":
        direction_id = self._direction
    return group, route_id, direction_id


def _follows_trip(self: _Coordinator, group: str, route_id: str, direction_id: str,
                  trip_id: str, entity_id: str) -> bool:
    ''' Whether a trip update is one of the trips this entity follows '''
    # first part covers start/end and thus multiple RT are possible for the same stop, also, for SIRI route_id do not match so a 'in' is used
    # the second part covers local stops, i.e. per trip, so only one RT possible for that stop
    if group == "route":
        # route-mode, between predefined start/stop
        if direction_id != "nn":
            return (
                str(direction_id) == str(self._direction)
                and _same_route(self._route_id, route_id)
            )  or trip_id in self._trip_list
        return _names_trip(self._trip_id, trip_id) or (trip_id in self._trip_list)
    # trip-mode, for local stops which can have multiple routes,
    # and for the entities of a feed that names no line: the
    # board's own trips count there too, or a journey on such a
    # feed would only ever hear about its next departure
    # a local stops context carries no list of its own
    return (trip_id == self._trip_id
            or entity_id == self._trip_short_name
            or trip_id in (getattr(self, "_trip_list", None) or ()))
