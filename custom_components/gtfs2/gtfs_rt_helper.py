import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, tzinfo
import json
from typing import TYPE_CHECKING, Any

import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text as sql_text


_LOGGER = logging.getLogger(__name__)

from .const import (

    ATTR_STOP_ID,
    ATTR_ROUTE,
    ATTR_TRIP,
    ATTR_DIRECTION_ID,
    ATTR_DUE_IN,
    ATTR_DUE_AT,
    ATTR_DELAY,
    ATTR_NEXT_UP,
    ATTR_NEXT_RT,
    ATTR_NEXT_RT_DELAYS,
    ATTR_NEXT_RT_TRIPS,
    ATTR_RT_CANCELLED,
    ATTR_RT_SKIPPED,
    ATTR_DEVICE_CLASS,

    TIME_STR_FORMAT,
)
from .alerts import journey_alerts
from .rt_feed import (
    CANCELLED_TRIP, NO_DATA_STOP, SKIPPED_STOP, FeedEntities, _Coordinator, _read_feed, _same_route,
    delay_of, stop_relationship, stop_update_clock,
    trip_relationship,
)
from .vehicles import get_rt_vehicle_positions

if TYPE_CHECKING:
    # for the annotations only
    from .coordinator import GTFSUpdateCoordinator

# the departures, delays and trips listed at one stop, in the same order
type _Slot = dict[str, list[Any]]
# {route_id: {direction_id: {stop_id: _Slot}}}
type _DepartureTimes = dict[str, dict[str, dict[str, _Slot]]]


def due_in_minutes(timestamp: datetime) -> int:
    """Get the remaining minutes from now until a given (aware, UTC) datetime object."""
    if timestamp.tzinfo is None:
        timestamp = dt_util.utc_from_timestamp(timestamp.timestamp())
    diff = timestamp - dt_util.utcnow()
    _LOGGER.debug("GTFS RT due in minutes, timestamp: %s, now_utc: %s", timestamp, dt_util.utcnow())
    return int(diff.total_seconds() / 60)


def get_next_services(self: GTFSUpdateCoordinator) -> dict[str, Any]:
    self._stop = self._stop_id
    self._destination = self._destination_id
    self._route = self._route_id
    self._trip = self._trip_id
    self._direction = self._direction
    self._trip_short_name = self._trip_short_name
    _LOGGER.debug("Configuration for RT route: %s, RT trip: %s, RT stop: %s, RT direction: %s, trip short name: %s", self._route, self._trip, self._stop, self._direction, self._trip_short_name)
    self._rt_group = "route"
    rt_departures = get_rt_route_trip_statuses(self)
    at_stop = rt_departures.get(self._route, {}).get(self._direction, {}).get(self._stop, {})
    next_services = at_stop.get("departures", [])
    next_delays = at_stop.get("delays", [])
    next_trips = at_stop.get("trips", [])

    if next_services:
        _LOGGER.debug("Next services: %s", next_services)
    
    due_in = (
        dt_util.as_utc(next_services[0])
        if len(next_services) > 0
        else "-"
    )
    
    attrs = {
        ATTR_DUE_IN: due_in,
        ATTR_STOP_ID: self._stop,
        ATTR_ROUTE: self._route,
        ATTR_TRIP: self._trip,
        ATTR_DIRECTION_ID: self._direction,
        ATTR_NEXT_RT: next_services,
        ATTR_NEXT_RT_DELAYS: next_delays,
        ATTR_NEXT_RT_TRIPS: next_trips,
        # what the feed struck out among the entity's trips: a card can
        # say "cancelled" where the static list would have shown a time
        ATTR_RT_CANCELLED: sorted(getattr(self, "_rt_cancelled", None) or {}),
        ATTR_RT_SKIPPED: sorted(getattr(self, "_rt_skipped", None) or {}),
    }
    
    if len(next_services) > 0:
        attrs[ATTR_DUE_AT] = next_services[0].strftime(TIME_STR_FORMAT)

    if len(next_services) > 1:
        attrs[ATTR_NEXT_UP] = next_services[1].strftime(TIME_STR_FORMAT)
    if len(next_delays) > 0:
        attrs[ATTR_DELAY] = next_delays[0]
    attrs[ATTR_DEVICE_CLASS] = (
        "timestamp"
        if len(next_services) > 0
        else ""
    )
    
    _LOGGER.debug("Next services attributes: %s", attrs)
    return attrs


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
    ''' The line a trip update names '''
    # a json feed leaves out what it does not know, where the
    # protobuf reader writes every field: the line, the stop, the
    # arrival of a first stop are read with their defaults
    return trip.get("route_id") or ""


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


def _stop_time_and_delay(stop: Mapping[str, Any], trip_id: str,
                         scheduled: Mapping[str, int]) -> tuple[int, int | None]:
    ''' When the vehicle leaves the stop, and its delay '''
    delay: int | None
    stop_time, delay = stop_update_clock(stop)

    if not stop_time and delay and scheduled.get(trip_id):
        # the feed gives the delay and no time: read as
        # an epoch that would be 1970, which reads as
        # long past and dropped the departure with it
        stop_time = scheduled[trip_id] + delay
        _LOGGER.debug("Trip %s carries a delay and no time: %s + %ss",
                      trip_id, scheduled[trip_id], delay)
    elif stop_time and scheduled.get(trip_id):
        # a time and no delay, or a zero one: the delay is the gap to the
        # timetable, as the leg file reads it
        delay = delay_of(delay, stop_time, scheduled[trip_id])
    return stop_time, delay


def _departure_slot(departure_times: _DepartureTimes, route_id: str, direction_id: str,
                    stop_id: str) -> _Slot:
    ''' The departures, delays and trips listed for one stop '''
    slot = departure_times.setdefault(route_id, {}).setdefault(direction_id, {}).setdefault(stop_id, {})
    if not slot.get("departures"):
        slot["departures"] = []
        slot["delays"] = []
        # the trip behind each departure, same order
        slot["trips"] = []
    return slot


def _read_stop_updates(self: _Coordinator, entity: Mapping[str, Any], trip_id: str, direction_id: str,
                       start_date: str | None, departure_times: _DepartureTimes,
                       scheduled: Mapping[str, int]) -> None:
    ''' Add the departures a trip update gives at this entity's stop '''
    entity_id = entity.get("id") or ""
    for stop in entity["trip_update"].get("stop_time_update") or []:
        stop_id = stop.get("stop_id") or ""
        stop_sequence = stop.get("stop_sequence")
        if not (stop_id == self._stop_id or (stop_id == "" and stop_sequence == self._stop_sequence)):
            continue
        _LOGGER.debug("Stop found: %s", stop)
        # if the data does not contain a stop_id but only a stop_sequence, assume stop_id being the correct stop based on sequence
        # this does not have to be always correct but best-guess
        if stop_id == "":
            stop_id = self._stop_id
        called = stop_relationship(stop)
        if called == SKIPPED_STOP:
            # the vehicle runs but does not call here
            self._rt_skipped.setdefault(trip_id, set()).add(start_date)
            _LOGGER.debug("Trip %s skips %s on %s, not a departure", trip_id, stop_id, start_date)
            continue
        if called == NO_DATA_STOP:
            # no prediction for this call: the timetable
            # stands, and a zero here is not "on time"
            _LOGGER.debug("Trip %s has no realtime at %s", trip_id, stop_id)
            continue

        if direction_id == "nn" or self._direction in (None, "None") or entity_id == self._trip_short_name or trip_id in getattr(self, "_trip_list", ()): # in this case the trip_id serves as a basis so one can safely set direction to the requesting entity direction; a trip from the entity's own trip list carries the static (possibly repaired) direction, which overrules what the rt feed announces
            direction_id = self._direction

        slot = _departure_slot(departure_times, self._route_id, direction_id, stop_id)
        stop_time, delay = _stop_time_and_delay(stop, trip_id, scheduled)

        # Ignore arrival times in the past
        departure_dt = dt_util.utc_from_timestamp(stop_time)  # aware UTC, epoch is always UTC
        if due_in_minutes(departure_dt) >= 0:
            slot["departures"].append(departure_dt)
            # the delay belongs to this departure: appending it
            # outside this branch kept the delays of departures
            # that were dropped, so delays[n] described some
            # other departure than departures[n]
            slot["delays"].append(delay)
            slot["trips"].append(trip_id)
            _LOGGER.debug("RT stoptime: %s, in utcfromtimestamp: %s", stop_time, departure_dt)
        else:
            _LOGGER.debug("Not using realtime stop data for old due-in-minutes: %s", due_in_minutes(departure_dt))


def _sort_departure_slots(departure_times: _DepartureTimes) -> None:
    ''' Sort by time, carrying each delay and trip with its own departure '''
    # the three lists are appended together (_read_stop_updates): sorting
    # them apart breaks the pairing
    for directions in departure_times.values():
        for stops in directions.values():
            for slot in stops.values():
                paired = sorted(zip(slot["departures"], slot["delays"], slot["trips"]),
                                key=lambda p: p[0])
                slot["departures"] = [p[0] for p in paired]
                slot["delays"] = [p[1] for p in paired]
                slot["trips"] = [p[2] for p in paired]


def get_rt_route_trip_statuses(self: _Coordinator,
                               feed_entities: FeedEntities | None = None) -> _DepartureTimes:
    ''' Get next rt departure for route (multiple) or trip (single) '''
    # explanatory logic
    # sources can provide trip_id with or without route, route with or without direction hence a lot of conditions as the resultset has (!) to include the direction
    # if route-based info is required, for start/end stops, then one needs to cover also for routes without direction_id and thus trip
    # if response does not provide a direction_id then use trip_id, make directon temporarily nn and when the stop is identified make it equal to the requesting direction
    # in this case the trip still covers the direction

    departure_times: _DepartureTimes = {}
    # what the feed struck out among the trips this entity follows:
    # {trip_id: start_date or None}, the day being the service day the
    # feed names (YYYYMMDD) when it names one. A trip cancelled today may
    # well run tomorrow under the same id.
    self._rt_cancelled = {}
    self._rt_skipped = {}

    if self._vehicle_position_url:
        get_rt_vehicle_positions(self)

    # a source can publish alerts or vehicle positions without trip updates
    # (the TTC subway is alerts-only): no times to match then, the vehicles
    # above still land on the map and the static timetable keeps the board
    if not self._trip_update_url:
        self._feed_entities = None
        return {}

    # feed_entities may be passed in by a caller that already fetched/parsed
    # it once for the current refresh cycle (e.g. matching many stops against
    # the same feed), avoiding a re-fetch + re-parse per call.
    if feed_entities is None:
        feed_entities = _read_feed(self, self._trip_update_url, "trip_data")
    self._feed_entities = feed_entities
    
    if not feed_entities:
        _LOGGER.debug("No proper RT feed entities: %s", feed_entities)
        return {}

    # what the timetable says for the trips on the board, to lay a delay on
    # when the feed publishes one without a time, and to read a delay the
    # feed writes as 0; the trips it no longer lists as well
    scheduled = _scheduled_departures(self)
    scheduled.update(_scheduled_off_board(self, feed_entities, scheduled))

    if self._rt_group == "route":
        _LOGGER.debug("Search departure times for route: %s, trip: %s, type: %s, direction: %s, short_name: %s, trip_list: %s", self._route_id, self._trip_id, self._rt_group, self._direction, self._trip_short_name, self._trip_list)
    else:
        _LOGGER.debug("Search departure times for trip: %s, type: %s, short_name: %s", self._trip_id, self._rt_group, self._trip_short_name)

    for entity in feed_entities:

        if not entity.get('trip_update', False):
            continue

        trip = entity["trip_update"]["trip"]
        group, route_id, direction_id = _trip_group_route_direction(self, trip)
        trip_id = trip.get("trip_id") or ""
        entity_id = entity.get("id") or ""

        if not _follows_trip(self, group, route_id, direction_id, trip_id, entity_id):
            continue
        _LOGGER.debug("Entity found params - group: %s, route_id: %s, direction_id: %s, self_trip_id: %s, with rt trip: %s, rt id: %s", group, route_id, direction_id, self._trip_id, trip, entity_id)

        start_date = trip.get("start_date") or None
        relationship = trip_relationship(entity)
        if relationship in CANCELLED_TRIP:
            # no departure at all: the stop updates it may still
            # carry (every stop SKIPPED, a delay left in) say nothing
            # every day the feed strikes this trip out on, not the
            # last one read: a strike over two days publishes the
            # same id twice and today's run used to be forgotten
            self._rt_cancelled.setdefault(trip_id, set()).add(start_date)
            _LOGGER.debug("Trip %s is %s on %s, not a departure", trip_id, relationship, start_date)
            continue

        _read_stop_updates(self, entity, trip_id, direction_id, start_date, departure_times, scheduled)

    _sort_departure_slots(departure_times)

    _LOGGER.debug("Departure times Route Trip: %s", departure_times)
    return departure_times


def struck_trips(self: _Coordinator) -> dict[str, set[str | None]]:
    """{trip_id: start_date or None} of the trips the feed struck out among
    the ones this entity follows, as the last get_rt_route_trip_statuses
    read them: cancelled, or skipping the entity's origin. The day is the
    service day the feed names, None when it names none."""
    return merge_struck(getattr(self, "_rt_skipped", None),
                        getattr(self, "_rt_cancelled", None))


def merge_struck(*sources: Mapping[str, set[str | None] | str | None] | None
                 ) -> dict[str, set[str | None]]:
    """Fold several {trip_id: days} together, keeping every day named.

    A trip can be cancelled one day and skip the origin another, and one
    cycle's reading does not replace the last: both are days it is not a
    departure. A day of None means the feed named none, which stands for
    every day the trip runs.
    """
    merged: dict[str, set[str | None]] = {}
    for source in sources:
        for trip, days in (source or {}).items():
            merged.setdefault(trip, set()).update(
                days if isinstance(days, (set, frozenset, list, tuple)) else {days})
    return merged


    


def get_rt_alerts(self: GTFSUpdateCoordinator) -> dict[str, Any]:
    rt_alerts = {}
    # an entry created before this option existed has no alerts_url at all, and
    # subscripting None raised, which cost that entry its whole realtime block
    url = str(self._alerts_url or "")
    # any url, as the trip updates and the vehicles: a file:// feed is what
    # update_gtfs_rt_local writes, and an "http" prefix left it unread
    if url:
        feed_entities = _read_feed(self, url, "alerts")
        rt_alerts = journey_alerts(self, feed_entities)

    return rt_alerts


    
        
