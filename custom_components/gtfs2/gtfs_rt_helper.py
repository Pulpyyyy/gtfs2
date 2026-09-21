import logging
import re
from datetime import datetime, timedelta
from urllib.parse import quote
import json
import os

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import requests
import voluptuous as vol
from google.transit import gtfs_realtime_pb2
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE, CONF_NAME
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
import threading
import time
from homeassistant.util import Throttle
import binascii
import base64

from .requests_testadapter import Resp

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
    ATTR_ICON,
    ATTR_UNIT_OF_MEASUREMENT,
    ATTR_DEVICE_CLASS,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,

    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_API_KEY_LOCATION,
    CONF_ACCEPT_HEADER_PB,
    CONF_STOP_ID,
    CONF_ROUTE,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_ROUTE_DELIMITER,
    CONF_ICON,
    CONF_SERVICE_TYPE,

    DEFAULT_SERVICE,
    DEFAULT_ICON,
    DEFAULT_DIRECTION,
    DEFAULT_PATH,
    DEFAULT_PATH_GEOJSON,

    TIME_STR_FORMAT
)
from .alerts import journey_alerts

_UNSAFE_FILE_PART = re.compile(r"[^a-z0-9._-]+")


def safe_file_part(value) -> str:
    """A route or direction id, made safe to put in a file name.

    Both geojson files are named after ids that come out of the datasource,
    that is to say out of a url the user pasted: an id like ZOP:653 makes a
    file no Windows share can read, a percent sign has to be escaped in the
    /local/ url that serves the file, and an id carrying a slash writes into
    a directory that does not exist and loses the file to an OSError.

    Rather than list the separators a feed may bring, keep letters, digits,
    dot, dash and underscore, replace every run of the rest with a single
    underscore and lowercase, so one route always lands on one file.
    """
    return re.sub(r"\.\.+", "_", _UNSAFE_FILE_PART.sub("_", str(value).lower()))


def due_in_minutes(timestamp):
    """Get the remaining minutes from now until a given (aware, UTC) datetime object."""
    if timestamp.tzinfo is None:
        timestamp = dt_util.utc_from_timestamp(timestamp.timestamp())
    diff = timestamp - dt_util.utcnow()
    _LOGGER.debug(f"GTFS RT due in minutes, timestamp: %s, now_utc: %s", timestamp, dt_util.utcnow())
    return int(diff.total_seconds() / 60)

# One GTFS-RT feed covers a whole network, so every sensor reading the same
# provider asks for the same bytes. Each coordinator used to download and parse
# it for itself, once a minute: on a 1.6 MiB feed with six entries that is
# about 13.8 GiB a day, and the protobuf to json conversion dominates the CPU.
#
# The feed publishes neither ETag nor Last-Modified, so conditional requests
# are impossible and a local cache is the only way to avoid the repeat.
_FEED_CACHE: dict[tuple[str, str, str], tuple[float, object]] = {}
_FEED_CACHE_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_FEED_CACHE_GUARD = threading.Lock()
# short enough that a delay stays fresh, long enough to cover a wave of
# coordinators: they were measured starting 12 ms apart
FEED_CACHE_TTL = 30
# when a feed failed, how long its sensors take the answer as read rather
# than each waiting out the timeout again: short enough that a host coming
# back is read within the minute
_FEED_FAILED: dict[tuple[str, str, str], float] = {}
FEED_FAIL_TTL = 15
# how long a feed nobody asks for any more is kept: a key holds the url it
# was read from, so a rotating query key, a source removed or a feed url
# edited left its last answer in memory for the life of the process
FEED_CACHE_KEEP = 600

RT_USER_AGENT = "GTFS2-HomeAssistant/1.0 (+https://github.com/vingerha/gtfs2)"


def _with_user_agent(headers):
    """The request headers with a User-Agent naming the integration.

    requests announces itself as python-requests, which some agency gateways
    refuse outright: the Azure Application Gateway in front of the TTC feeds
    answers 403 to it. Naming the client is enough to pass, and the address
    lets an operator see who is calling.
    """
    merged = {"User-Agent": RT_USER_AGENT}
    if headers:
        merged.update(headers)
    return merged


def get_gtfs_feed_entities(url: str, headers, label: str, owner: str = ""):
    """Return the feed entities, fetching at most once per TTL and per feed.

    Holds a per-feed lock across the fetch: without it the coordinators, which
    wake within milliseconds of each other, would all miss the cache and
    download in parallel before the first one filled it.

    owner is the datasource file name: with the feeds configured per source,
    a source's headers can never differ under one url, but two sources could
    share a url with different keys - keying on the owner keeps their
    responses apart.
    """
    key = (owner, url, label)
    with _FEED_CACHE_GUARD:
        lock = _FEED_CACHE_LOCKS.setdefault(key, threading.Lock())
        _forget_old_feeds(key)

    with lock:
        cached = _FEED_CACHE.get(key)
        if cached is not None:
            age = time.time() - cached[0]
            if age < FEED_CACHE_TTL:
                _LOGGER.debug("GTFS RT cache hit for %s (%s), age %.1fs", label, url, age)
                return cached[1]

        failed = _FEED_FAILED.get(key)
        if failed is not None and time.time() - failed < FEED_FAIL_TTL:
            # the last attempt just failed: a host that is down answers every
            # sensor of the source the same way, and each of them waiting out
            # the timeout in turn holds an executor thread for nothing
            _LOGGER.debug("GTFS RT %s (%s) failed %.1fs ago, not asked again yet",
                          label, url, time.time() - failed)
            return None

        entities = _fetch_gtfs_feed_entities(url, headers, label)
        # a failed fetch returns None: it is not kept as data, only as the
        # memory of a failure, so the next caller gets a real attempt once
        # the short wait is over rather than a stale answer
        if entities is not None:
            _FEED_CACHE[key] = (time.time(), entities)
            _FEED_FAILED.pop(key, None)
        else:
            _FEED_FAILED[key] = time.time()
        return entities


def _forget_old_feeds(current):
    """Drop the feeds nothing has asked for in a while.

    Read under the guard, so the caller's own key is spared whatever its
    age: a feed read once an hour must find its lock where it left it.
    """
    old = time.time() - FEED_CACHE_KEEP
    for key, (when, _entities) in list(_FEED_CACHE.items()):
        if key != current and when < old:
            del _FEED_CACHE[key]
            _FEED_CACHE_LOCKS.pop(key, None)
            _FEED_FAILED.pop(key, None)
            _LOGGER.debug("Forgot the feed nothing reads any more: %s", key[2])
    for key, when in list(_FEED_FAILED.items()):
        if key != current and when < old:
            del _FEED_FAILED[key]
            _FEED_CACHE_LOCKS.pop(key, None)


def _fetch_gtfs_feed_entities(url: str, headers, label: str):
    # Imported here and not at module level: the class lives in protobuf,
    # which arrives with gtfs-realtime-bindings, and the synthetic suite
    # stubs those bindings out while replacing this whole function.
    from google.protobuf.message import DecodeError
    _LOGGER.debug(f"GTFS RT get_feed_entities for url: {url} , headers: {headers}, label: {label}")
    feed = gtfs_realtime_pb2.FeedMessage()  # type: ignore

    try:
        if url.startswith('file'):
            requests_session = requests.session()
            requests_session.mount('file://', LocalFileAdapter())
            response = requests_session.get(url)
        else:
            response = requests.get(url, headers=_with_user_agent(headers), timeout=20)
    except requests.RequestException as ex:
        # a host that is down, a name that no longer resolves, a certificate
        # that expired: the caller reads None as "no realtime this cycle",
        # which is what it already did for a bad response
        _LOGGER.error("Could not reach %s for %s: %s", url, label, type(ex).__name__)
        return None

    # Success is the status code plus a body that parses below. Grepping the
    # decoded body for error phrases rejected valid feeds whose own free text
    # carried them, e.g. an alert quoting "Not Found".
    if response.status_code == 200:
        _LOGGER.debug("Successfully updated %s", label)
    else:
        # the first line of the body says what went wrong; a maintenance page
        # in full says it again in a hundred lines of html
        _LOGGER.error("Trying to update %s, and got RT response(code): %s with text: %s",
                      label, response.status_code, response.text[:200])
        return None

    if label == "alerts":
        _LOGGER.debug("Feed : %s", feed)

    try:
        json_object = json.loads(response.text)
        feed = json.loads(response.text)
    except ValueError as e:
        _LOGGER.debug("GTFS RT data is not providing format json")
        # a maintenance or error page served with a 200 lands here and is not
        # protobuf either: degrade to no data instead of an uncaught traceback
        try:
            if label == "vehicle_positions":
                feed = convert_gtfs_realtime_positions_to_json(response.content)
            elif label == "trip_data":
                feed = convert_gtfs_realtime_to_json(response.content)
            else: # not yet converted to json
                feed.ParseFromString(response.content)
                return feed.entity
        except DecodeError:
            _LOGGER.error("Trying to update %s, and got a 200 whose body is neither json nor GTFS-RT protobuf", label)
            return None

    return feed.get('entity')

def get_next_services(self):
    self._stop = self._stop_id
    self._destination = self._destination_id
    self._route = self._route_id
    self._trip = self._trip_id
    self._direction = self._direction
    self._trip_short_name = self._trip_short_name
    _LOGGER.debug("Configuration for RT route: %s, RT trip: %s, RT stop: %s, RT direction: %s, trip short name: %s", self._route, self._trip, self._stop, self._direction, self._trip_short_name)
    self._rt_group = "route"
    rt_departures = get_rt_route_trip_statuses(self)
    next_services = rt_departures.get(self._route, {}).get(self._direction, {}).get(self._stop, {}).get("departures", [])
    next_delays = rt_departures.get(self._route, {}).get(self._direction, {}).get(self._stop, {}).get("delays", [])
    next_trips = rt_departures.get(self._route, {}).get(self._direction, {}).get(self._stop, {}).get("trips", [])

    if next_services:
        _LOGGER.debug("Next services: %s", next_services)
    
    if self._relative :
        due_in = (
            due_in_minutes(next_services[0])
            if len(next_services) > 0
            else "-"
        )
    else:
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
        attrs[ATTR_DUE_AT] = (
            next_services[0].strftime(TIME_STR_FORMAT)
            if len(next_services) > 0
            else "-"
        )

    if len(next_services) > 1:
        attrs[ATTR_NEXT_UP] = (
            next_services[1].strftime(TIME_STR_FORMAT)
            if len(next_services) > 1
            else "-"
        )
    if len(next_delays) > 0:
        attrs[ATTR_DELAY] = (
            next_delays[0]
            if len(next_delays) > 0
            else "-"
        )                 
    if self._relative :
        attrs[ATTR_UNIT_OF_MEASUREMENT] = "min"
    else :
        attrs[ATTR_DEVICE_CLASS] = (
            "timestamp" 
            if len(next_services) > 0
            else ""
        )
    
    _LOGGER.debug("Next services attributes: %s", attrs)
    return attrs
    
def cached_feed_has_future_stop(owner, url, routes, now_epoch):
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
        if not isinstance(entity, dict):
            continue
        trip_update = entity.get("trip_update")
        if not trip_update:
            continue
        seen = (trip_update.get("trip") or {}).get("route_id")
        if routes and not any(_same_route(route, seen) for route in routes):
            continue
        for stop in trip_update.get("stop_time_update") or []:
            when = max((stop.get("arrival") or {}).get("time") or 0,
                       (stop.get("departure") or {}).get("time") or 0)
            if when > now_epoch:
                return True
    return False


def _as_epoch(value):
    """A departure time, as the coordinator publishes it, in epoch seconds."""
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except (TypeError, ValueError):
        return None


def _scheduled_departures(self):
    """When the board's trips are due at the entity's stop, by trip id.

    Read off the departure the coordinator already holds: the next one and
    the ones listed behind it. A feed is free to publish a delay and no
    time at all, which the spec allows and plenty of them do; laid on the
    time the timetable announces, that delay is a departure like any other.
    """
    due = {}
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


def _names_trip(watched, seen):
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


def _same_route(configured, seen):
    """Whether a realtime route_id designates the configured route.

    Some feeds qualify their ids, so an exact match alone is too strict and a
    plain substring test was used instead. That test makes "Line:1" swallow
    "Line:11", and "Line:4" swallow 40, 41, 43 and 45: the sensor then reports
    departures of a line the user never asked for.

    A qualified id still has to end on the configured one, at a separator, so
    a longer number cannot pass for a shorter one.
    """
    configured, seen = str(configured or ""), str(seen or "")
    if not configured or not seen:
        return False
    if configured == seen:
        return True
    if not seen.endswith(configured):
        return False
    # the character before must be a separator, never a digit or a letter
    return not seen[-len(configured) - 1].isalnum()


def get_rt_route_trip_statuses(self, feed_entities=None):
    ''' Get next rt departure for route (multiple) or trip (single) '''
    # explanatory logic
    # sources can provide trip_id with or without route, route with or without direction hence a lot of conditions as the resultset has (!) to include the direction
    # if route-based info is required, for start/end stops, then one needs to cover also for routes without direction_id and thus trip
    # if response does not provide a direction_id then use trip_id, make directon temporarily nn and when the stop is identified make it equal to the requesting direction
    # in this case the trip still covers the direction

    departure_times = {}
    # what the feed struck out among the trips this entity follows:
    # {trip_id: start_date or None}, the day being the service day the
    # feed names (YYYYMMDD) when it names one. A trip cancelled today may
    # well run tomorrow under the same id.
    self._rt_cancelled = {}
    self._rt_skipped = {}

    if self._vehicle_position_url:
        vehicle_positions = get_rt_vehicle_positions(self)

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
        feed_entities = get_gtfs_feed_entities(
            url=self._trip_update_url, headers=self._headers, label="trip_data",
            owner=self._data.get("file", ""),
        )
    self._feed_entities = feed_entities
    
    if not feed_entities:
        _LOGGER.debug("No proper RT feed entities: %s", feed_entities)
        return {}

    # what the timetable says for the trips on the board, to lay a delay on
    # when the feed publishes one without a time
    scheduled = _scheduled_departures(self)

    if self._rt_group == "route":
        _LOGGER.debug("Search departure times for route: %s, trip: %s, type: %s, direction: %s, short_name: %s, trip_list: %s", self._route_id, self._trip_id, self._rt_group, self._direction, self._trip_short_name, self._trip_list)
    else:
        _LOGGER.debug("Search departure times for trip: %s, type: %s, short_name: %s", self._trip_id, self._rt_group, self._trip_short_name)

    for entity in feed_entities:

        if entity.get('trip_update', False):
            
            # If delimiter specified split the route ID in the gtfs rt feed
            if self._route_delimiter is not None:
                route_id_split = entity["trip_update"]["trip"]["route_id"].split(
                    self._route_delimiter
                )
                if route_id_split[0] == self._route_delimiter:
                    route_id = entity["trip_update"]["trip"]["route_id"]
                else:
                    route_id = route_id_split[0]
            else:
                route_id = entity["trip_update"]["trip"]["route_id"]

            if "direction_id" in entity["trip_update"]["trip"] and entity["trip_update"]["trip"]["direction_id"] not in ("", None):
                    direction_id = entity["trip_update"]["trip"]["direction_id"]
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

            trip_id = entity["trip_update"]["trip"]["trip_id"]
            entity_id = entity["id"]
            
            #_LOGGER.debug("Search for entity with params - group: %s, route_id: %s, direction_id: %s, self_trip_id: %s, with rt trip: %s, rt id: %s", self._rt_group, route_id, direction_id, self._trip_id, entity["trip_update"]["trip"], entity_id)            
                
            # first part covers start/end and thus multiple RT are possible for the same stop, also, for SIRI route_id do not match so a 'in' is used 
            # the second part covers local stops, i.e. per trip, so only one RT possible for that stop         
            if group == "route":
                # route-mode, between predefined start/stop
                if direction_id != "nn":
                    matched = (
                        str(direction_id) == str(self._direction)
                        and _same_route(self._route_id, route_id)
                    )  or trip_id in self._trip_list
                else:
                    matched = _names_trip(self._trip_id, trip_id) or (trip_id in self._trip_list)
            else:
                # trip-mode, for local stops which can have multiple routes,
                # and for the entities of a feed that names no line: the
                # board's own trips count there too, or a journey on such a
                # feed would only ever hear about its next departure
                # a local stops context carries no list of its own
                matched = (trip_id == self._trip_id
                           or entity_id == self._trip_short_name
                           or trip_id in (getattr(self, "_trip_list", None) or ()))

            if matched:
                _LOGGER.debug("Entity found params - group: %s, route_id: %s, direction_id: %s, self_trip_id: %s, with rt trip: %s, rt id: %s", group, route_id, direction_id, self._trip_id, entity["trip_update"]["trip"], entity_id)

                start_date = entity["trip_update"]["trip"].get("start_date") or None
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

                for stop in entity["trip_update"]["stop_time_update"]:
                    stop_id = stop["stop_id"]
                    stop_sequence = stop["stop_sequence"]
                    if stop_id == self._stop_id or (stop_id == "" and stop_sequence == self._stop_sequence):
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

                        if self._route_id not in departure_times:
                            departure_times[self._route_id] = {}
                                               
                        if direction_id == "nn" or self._direction in (None, "None") or entity_id == self._trip_short_name or trip_id in getattr(self, "_trip_list", ()): # in this case the trip_id serves as a basis so one can safely set direction to the requesting entity direction; a trip from the entity's own trip list carries the static (possibly repaired) direction, which overrules what the rt feed announces
                            direction_id = self._direction                   

                        if direction_id not in departure_times[self._route_id]:
                            departure_times[self._route_id][direction_id] = {}
                            
                        if not departure_times[self._route_id][direction_id].get(
                            stop_id
                        ):
                            departure_times[self._route_id][direction_id][stop_id] = {}
                        
                        if not departure_times[self._route_id][direction_id][stop_id].get(
                            "departures"
                        ):                 
                            departure_times[self._route_id][direction_id][stop_id]["departures"] = []
                            departure_times[self._route_id][direction_id][stop_id]["delays"] = []
                            # the trip behind each departure, same order
                            departure_times[self._route_id][direction_id][stop_id]["trips"] = []

                        # the later of the two 'time' attributes is the one to announce
                        # e.g. at a terminus/layover where the vehicle stands several
                        # minutes at its bay
                        stop_time = max(stop["arrival"]["time"],
                                        stop["departure"]["time"])
                            
                        if stop["departure"].get("delay",0) >= stop["arrival"].get("delay",0):
                            delay = stop["departure"].get("delay",0)
                        else:
                            delay = stop["arrival"].get("delay",0)

                        if not stop_time and delay and scheduled.get(trip_id):
                            # the feed gives the delay and no time: read as
                            # an epoch that would be 1970, which reads as
                            # long past and dropped the departure with it
                            stop_time = scheduled[trip_id] + delay
                            _LOGGER.debug("Trip %s carries a delay and no time: %s + %ss",
                                          trip_id, scheduled[trip_id], delay)

                        # Ignore arrival times in the past
                        departure_dt = dt_util.utc_from_timestamp(stop_time)  # aware UTC, epoch is always UTC
                        if due_in_minutes(departure_dt) >= 0:
                            departure_times[self._route_id][direction_id][stop_id]["departures"].append(departure_dt)
                            # the delay belongs to this departure: appending it
                            # outside this branch kept the delays of departures
                            # that were dropped, so delays[n] described some
                            # other departure than departures[n]
                            departure_times[self._route_id][direction_id][stop_id]["delays"].append(delay)
                            departure_times[self._route_id][direction_id][stop_id]["trips"].append(trip_id)
                            _LOGGER.debug("RT stoptime: %s, in utcfromtimestamp: %s", stop_time, departure_dt)
                        else:
                            _LOGGER.debug("Not using realtime stop data for old due-in-minutes: %s", due_in_minutes(departure_dt))

    # Sort by time, carrying each delay with its own departure: sorting the two
    # lists independently, or only one of them, breaks the pairing again
    for route in departure_times:
        for direction in departure_times[route]:
            for stop in departure_times[route][direction]:
                slot = departure_times[route][direction][stop]
                trips = slot.get("trips") or []
                if len(slot["delays"]) == len(slot["departures"]) == len(trips):
                    paired = sorted(zip(slot["departures"], slot["delays"], trips),
                                    key=lambda p: p[0])
                    slot["departures"] = [p[0] for p in paired]
                    slot["delays"] = [p[1] for p in paired]
                    slot["trips"] = [p[2] for p in paired]
                elif len(slot["delays"]) == len(slot["departures"]):
                    paired = sorted(zip(slot["departures"], slot["delays"]),
                                    key=lambda p: p[0])
                    slot["departures"] = [p[0] for p in paired]
                    slot["delays"] = [p[1] for p in paired]
                else:
                    slot["departures"].sort()

    self.info = departure_times
    _LOGGER.debug("Departure times Route Trip: %s", departure_times)
    return departure_times


def struck_trips(self):
    """{trip_id: start_date or None} of the trips the feed struck out among
    the ones this entity follows, as the last get_rt_route_trip_statuses
    read them: cancelled, or skipping the entity's origin. The day is the
    service day the feed names, None when it names none."""
    return merge_struck(getattr(self, "_rt_skipped", None),
                        getattr(self, "_rt_cancelled", None))


def merge_struck(*sources):
    """Fold several {trip_id: days} together, keeping every day named.

    A trip can be cancelled one day and skip the origin another, and one
    cycle's reading does not replace the last: both are days it is not a
    departure. A day of None means the feed named none, which stands for
    every day the trip runs.
    """
    merged = {}
    for source in sources:
        for trip, days in (source or {}).items():
            merged.setdefault(trip, set()).update(
                days if isinstance(days, (set, frozenset, list, tuple)) else {days})
    return merged


def on_service_day(start_date, service_day):
    """Whether a feed's start_date is the service day.

    start_date is what the feed named, YYYYMMDD, or None for "unsaid",
    or the set of days it named for that trip: a strike lasts more than a
    day and a feed then publishes the same trip id once per day it hits.
    service_day is YYYY-MM-DD, or a datetime string starting with it.
    """
    if isinstance(start_date, (set, frozenset, list, tuple)):
        return any(on_service_day(day, service_day) for day in start_date) if start_date else True
    if not start_date:
        return True
    return str(service_day or "")[:10].replace("-", "") == str(start_date)[:8]

def get_rt_vehicle_positions(self):
    feed_entities = get_gtfs_feed_entities(
        url=self._vehicle_position_url,
        headers=self._headers,
        label="vehicle_positions",
        owner=self._data.get("file", ""),
    )
    geojson_body = []
    geojson_element = {"geometry": {"coordinates":[],"type": "Point"}, "properties": {"id": "", "title": "", "trip_id": "", "route_id": "", "direction_id": "", "vehicle_id": "", "vehicle_label": ""}, "type": "Feature"}
    if feed_entities is None:
        # a failed fetch returns None: iterating it raises, and the caller's
        # broad except then abandons the whole realtime block, so a hiccup on
        # vehicle-positions used to take the departure times down with it.
        # The file is left as it was: the last known positions beat none at
        # all while a host has a hiccup
        _LOGGER.debug("No proper RT feed entities for vehicle positions")
        return geojson_body
    if not feed_entities:
        # a feed that answers with nothing means the vehicles are off the
        # road, which is an answer: written as such, the map empties. Left
        # alone, the last buses of the evening sat on it all night
        _LOGGER.debug("The vehicle feed is empty, taking the vehicles off the map")
    for entity in feed_entities:
        vehicle = entity["vehicle"]
        
        if not vehicle["trip"]["trip_id"]:
            # Vehicle is not in service
            continue
        if vehicle["trip"]["trip_id"] == self._trip_id: 
            _LOGGER.debug('Adding position for TripId: %s, RouteId: %s, DirectionId: %s, Lat: %s, Lon: %s, crc_trip_id: %s', vehicle["trip"]["trip_id"],vehicle["trip"]["route_id"],vehicle["trip"]["direction_id"],vehicle["position"]["latitude"],vehicle["position"]["longitude"], binascii.crc32((vehicle["trip"]["trip_id"]).encode('utf8')))  
            
        # add data if trip found or if route in the selected direction
        if ( 
            str(vehicle["trip"]["trip_id"]) == str(self._trip_id)
            or 
            ( str(self._route_id) == str(vehicle["trip"]["route_id"])  and str(self._direction) == str(vehicle["trip"]["direction_id"] ))
            ):
            _LOGGER.debug("Found vehicle on route with attributes: %s", vehicle)
            _LOGGER.debug("crc : %s", binascii.crc32((vehicle["trip"]["trip_id"]).encode('utf8')))
            geojson_element = {"geometry": {"coordinates":[],"type": "Point"}, "properties": {"id": "", "title": "", "trip_id": "", "route_id": "", "direction_id": "", "vehicle_id": "", "vehicle_label": ""}, "type": "Feature"}
            geojson_element["geometry"]["coordinates"] = []
            geojson_element["geometry"]["coordinates"].append(vehicle["position"]["longitude"])
            geojson_element["geometry"]["coordinates"].append(vehicle["position"]["latitude"])
            # Altered to use vehicle_id (if existing) to create the unique indicator instead of trip_id
            # to reduce number of entities created by geojson. 
            _crc = str(binascii.crc32((vehicle["trip"]["trip_id"]).encode('utf8')))[-3:]
            _veh = str(vehicle.get("vehicle", {}).get("id", "") or vehicle.get("vehicle", {}).get("label", "")).strip()
            _dir = str(vehicle["trip"]["direction_id"])
            try:
                _line = str(self._data.get("next_departure", {}).get("route_short_name") or "").strip()
                _dest = self.config_entry.data.get("destination", "").split(": ")[-1].split(" (")[0].split(" - ")[0].strip()
            except Exception: 
                _line, _dest = "", ""
            if _line and _dest:
                _label = _line + " → " + _dest + " " + (_veh or _crc) + "_" + self._icon.split(':')[1]
            else:
                _label = str(self._route_id) + "(" + _dir + ")" + _crc + "_" + self._icon.split(':')[1]
            geojson_element["properties"]["id"] = str(self._route_id) + "_" + _dir + "_" + (_veh or _crc)
            geojson_element["properties"]["title"] = _label
            geojson_element["properties"]["trip_id"] = vehicle["trip"]["trip_id"]
            geojson_element["properties"]["route_id"] = str(self._route_id)
            geojson_element["properties"]["direction_id"] = vehicle["trip"]["direction_id"]
            geojson_element["properties"]["vehicle_id"] = vehicle["vehicle"]["id"]
            geojson_element["properties"]["vehicle_label"] = vehicle["vehicle"]["label"]
            geojson_element["properties"][vehicle["trip"]["trip_id"]] = geojson_element["geometry"]["coordinates"]
            geojson_body.append(geojson_element)
    
    self.geojson = {"features": geojson_body, "type": "FeatureCollection"}
        
    _LOGGER.debug("Vehicle geojson: %s", json.dumps(self.geojson))
    # named the same way as the route file next to it, see safe_file_part
    self._route_dir = safe_file_part(self._route_id) + "_" + safe_file_part(self._direction)
    update_geojson(self)
    return geojson_body
    


def get_rt_alerts(self):
    rt_alerts = {}
    # an entry created before this option existed has no alerts_url at all, and
    # subscripting None raised, which cost that entry its whole realtime block
    if str(self._alerts_url or "")[:4] == "http":
        feed_entities = get_gtfs_feed_entities(
            url=self._alerts_url,
            headers=self._headers,
            label="alerts",
            owner=self._data.get("file", ""),
        )
        rt_alerts = journey_alerts(self, feed_entities)

    return rt_alerts
    
def update_geojson(self):    
    geojson_dir = self.hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    file = os.path.join(geojson_dir, self._route_dir + ".json")
    _LOGGER.debug("Creating geojson file: %s", file)
    with open(file, "w") as outfile:
        json.dump(self.geojson, outfile)
    
def get_gtfs_rt(hass, path, data):
    """Get gtfs rt data."""
    _LOGGER.debug("Getting gtfs rt locally with data: %s", data)
    _headers = data.get('headers','')
    _source_format = data.get('source_format',None)                                                  
    gtfs_dir = hass.config.path(path)
    os.makedirs(gtfs_dir, exist_ok=True)
    url = data["url"]
    file = data["file"] + ".rt"
    if data.get(CONF_API_KEY_LOCATION, None) == "query_string":
      if data.get(CONF_API_KEY, None):
        url = url + "?" + data.get(CONF_API_KEY_NAME, "api_key") + "=" + data[CONF_API_KEY]
    # NOTE: Accept asks the server for a response format and the api key
    # authenticates, so they are unrelated, yet the header is only sent when
    # the key travels in a header. A feed that needs the header and takes its
    # key in the url, or one that needs it with no key at all, never gets it.
    # Left as is for now: changing it changes behaviour for existing setups.
    if data.get(CONF_API_KEY_LOCATION, None) == "header":
        _headers = {data.get(CONF_API_KEY_NAME, "api_key"): data[CONF_API_KEY]}
        if data.get(CONF_ACCEPT_HEADER_PB, False):
            _headers["Accept"] = "application/x-protobuf"
    
    if data.get('entity_for_siri',None):
        _LOGGER.debug("Getting siri RT departures with data: %s", data)
        entity_registry = er.async_get(hass)
        entity = er.async_get(hass).async_get(data["entity_for_siri"])
        _LOGGER.debug("entity: %s", entity)
        _LOGGER.debug("entity cfg id: %s", entity.config_entry_id)
        config_entry = hass.config_entries.async_get_entry(entity.config_entry_id)
        cf_data = config_entry.data
        cf_options = config_entry.options
        _stop_id = cf_data["origin"].split(':')[0]
        _LOGGER.debug("_stop_id: %s", _stop_id)
        _LOGGER.debug("config entry data: %s, options: %s", cf_data, cf_options)
        file = data["file"] + "_rt.json"
        try:
            r = convert_realtime_siri_trips_to_json(url,_headers,_stop_id)
            open(os.path.join(gtfs_dir, file), "w").write(json.dumps(r))
            return "ok"
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Ìssues with downloading GTFS RT SIRI data to: %s with error: %s", os.path.join(gtfs_dir, file), ex)
            return "no_rt_data_file" 
        return "ok"                                
    try:
        r = requests.get(url, headers=_with_user_agent(_headers), allow_redirects=True, timeout=20)
        if r.status_code != 200:
            # written first, an error page replaced the last good feed on
            # disk and the readers parsed that instead
            _LOGGER.error("Ìssues with downloading GTFS RT data, error: %s, content: %s",
                          r.status_code, r.content[:200])
            return "no_rt_data_file"
        open(os.path.join(gtfs_dir, file), "wb").write(r.content)
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.error("Ìssues with downloading GTFS RT data to: %s", os.path.join(gtfs_dir, file))
        return "no_rt_data_file"

    
    if data.get("debug_output", False):
        try:
            data_out = ""
            feed_entities = get_gtfs_feed_entities(
                url=data.get("url", None),
                headers=_headers,
                label=data.get("rt_type", "-"),
                owner=data.get("file", ""),
            )
            file_all = data["file"] + "_converted.txt"
            # check if content is json else write without format            
            try:
                open(os.path.join(gtfs_dir, file_all), "w").write(json.dumps(feed_entities, indent=4)) 
            except Exception as ex:
                _LOGGER.debug("Not writing to file as json because of error: %s", ex)
                open(os.path.join(gtfs_dir, file_all), "w").write(str(feed_entities))              
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.info("Ìssues with converting GTFS RT data to JSON, output to string") 
    return "ok"   
        
class LocalFileAdapter(requests.adapters.HTTPAdapter):
    """Used to allow requests.get for local file"""
    def build_response_from_file(self, request):
        file_path = request.url[7:]
        with open(file_path, 'rb') as file:
            buff = bytearray(os.path.getsize(file_path))
            file.readinto(buff)
            resp = Resp(buff)
            r = self.build_response(request, resp)
            return r

    def send(self, request, stream=False, timeout=None,
             verify=True, cert=None, proxies=None):
        return self.build_response_from_file(request)   

# the names of the GTFS-RT enums, spelled out so a reader (and a card
# reading the leg file) never sees a bare number; the SIRI path writes
# none of them, so every reader takes SCHEDULED for a missing key
CANCELLED_TRIP = ("CANCELED", "DELETED")
SKIPPED_STOP = "SKIPPED"
NO_DATA_STOP = "NO_DATA"


def _trip_relationship(trip):
    try:
        return trip.ScheduleRelationship.Name(trip.schedule_relationship)
    except (AttributeError, ValueError):
        return "SCHEDULED"


def _stop_relationship(stop_time_update):
    try:
        return stop_time_update.ScheduleRelationship.Name(stop_time_update.schedule_relationship)
    except (AttributeError, ValueError):
        return "SCHEDULED"


def trip_relationship(entity):
    """The trip's schedule_relationship out of a converted entity, SCHEDULED
    when the feed (or the SIRI path) says nothing."""
    return ((entity.get("trip_update") or {}).get("trip") or {}).get(
        "schedule_relationship") or "SCHEDULED"


def stop_relationship(stop_time_update):
    """The stop update's schedule_relationship, SCHEDULED when unsaid."""
    return (stop_time_update or {}).get("schedule_relationship") or "SCHEDULED"


def convert_gtfs_realtime_to_json(gtfs_realtime_data):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(gtfs_realtime_data)

    json_data = {
        "header": {
            "gtfs_realtime_version": feed.header.gtfs_realtime_version,
            "timestamp": feed.header.timestamp,
            "incrementality": feed.header.incrementality
        },
        "entity": []
    }

    for entity in feed.entity:
        entity_dict = {
            "id": entity.id,
            "trip_update": {
                "trip": {
                    "trip_id": entity.trip_update.trip.trip_id,
                    "start_time": entity.trip_update.trip.start_time,
                    "start_date": entity.trip_update.trip.start_date,
                    "route_id": entity.trip_update.trip.route_id,
                },
                "stop_time_update": []
            }
        }
        # direction_id is optional and protobuf returns 0 when a feed omits
        # it, which reads as a genuine direction and mislabels every untagged
        # trip; leave the key out instead so the reader falls back to "nn"
        if entity.trip_update.trip.HasField("direction_id"):
            entity_dict["trip_update"]["trip"]["direction_id"] = str(entity.trip_update.trip.direction_id)
        # what the feed says of the trip as a whole: SCHEDULED (the
        # default, so it is written even when the feed leaves it out),
        # ADDED, CANCELED, DELETED, UNSCHEDULED, DUPLICATED. A cancelled
        # trip keeps its stop updates in some feeds (the SNCF marks every
        # stop SKIPPED, sometimes with a delay), so a reader must look here
        # first. Measured 2026-09-15: NL cancels 18 % of its trips of the
        # hour, the SNCF adds trains under ids the static feed has not.
        entity_dict["trip_update"]["trip"]["schedule_relationship"] = _trip_relationship(
            entity.trip_update.trip)
        for stop_time_update in entity.trip_update.stop_time_update:
            stop_time_update_dict = {
                "stop_sequence": stop_time_update.stop_sequence,
                "stop_id": stop_time_update.stop_id,
                # SCHEDULED, SKIPPED (the vehicle does not call), NO_DATA
                # (no prediction here, the timetable stands), UNSCHEDULED
                "schedule_relationship": _stop_relationship(stop_time_update),
                "arrival": {
                    "delay": stop_time_update.arrival.delay,
                    "time": stop_time_update.arrival.time
                },
                "departure": {
                    "delay": stop_time_update.departure.delay,
                    "time": stop_time_update.departure.time
                }
            }
            entity_dict["trip_update"]["stop_time_update"].append(stop_time_update_dict)
        
        json_data["entity"].append(entity_dict)
    return json_data        

def convert_gtfs_realtime_positions_to_json(gtfs_realtime_data):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(gtfs_realtime_data)

    json_data = {
        "entity": []
    }
    for ent in feed.entity:
        entity = ent.vehicle
        entity_dict = {
        "vehicle": {
            "trip": {
                "trip_id" : entity.trip.trip_id,
                "route_id": entity.trip.route_id,
                "direction_id": entity.trip.direction_id
                },
            "vehicle": {
                "id": entity.vehicle.id,
                "label": entity.vehicle.label
                },
            "position": {
                "latitude": entity.position.latitude,
                "longitude": entity.position.longitude,
                "bearing": entity.position.bearing,
                "speed": entity.position.speed
            },
            "stop_id": entity.stop_id,
            "timestamp": entity.timestamp
        }
        }
        json_data["entity"].append(entity_dict)
    return json_data    

    
def convert_realtime_siri_trips_to_json(url,headers,stop_id):
    
    #Used for Strasbourg, but they differ on output too
    ##the Basic token is a base64 conversion of: d6452e5d-4894-4ee1-8d5b-11ce235eeef6	
    ## ZDY0NTJlNWQtNDg5NC00ZWUxLThkNWItMTFjZTIzNWVlZWY2
    ## ZDY0NTJlNWQtNDg5NC00ZWUxLThkNWItMTFjZTIzNWVlZWY2Og==    
    #_encoded = base64.b64encode(b'd6452e5d-4894-4ee1-8d5b-11ce235eeef6:').decode("utf-8") 
    #_headers = { "Authorization": f"Basic {_encoded}" }
    #url = "https://api.cts-strasbourg.eu/v1/siri/2.0/stop-monitoring?MonitoringRef=GACEN_20"

    #url = "https://bustime.mta.info/api/siri/stop-monitoring.json?key=f4f9c18e-0550-4cc7-bc36-275715015673&OperatorRef=MTA"
    
    # the url may already carry a query of its own, or none at all
    url = url + ("&" if "?" in url else "?") + f"MonitoringRef={quote(str(stop_id))}"
    response = requests.get(url, headers=_with_user_agent(headers), timeout=20)
    if response.status_code != 200:
        _LOGGER.error("Trying to read the SIRI feed, and got response(code): %s with text: %s",
                      response.status_code, response.text[:200])
        return {"entity": []}

    json_object = json.loads(response.content)
    feed = json_object

    if feed.get('Siri'):
        try:
            feed_entities = feed['Siri']['ServiceDelivery']['StopMonitoringDelivery'][0]['MonitoredStopVisit']
            feed = feed['Siri']
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Ìssues getting GTFS RT SIRI data: %s", ex)
            return 'issues with getting siri data'        
    else:  
        try:
            feed_entities = feed['ServiceDelivery']['StopMonitoringDelivery'][0]['MonitoredStopVisit']
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.error("Ìssues getting GTFS RT SIRI data: %s", ex)
            return 'issues with getting siri data'
        
    _LOGGER.debug("Feed entities: %s", feed_entities)

    tt = datetime.fromisoformat(feed['ServiceDelivery']['ResponseTimestamp'])
    json_data = {
        "header": {
            "gtfs_realtime_version": feed['ServiceDelivery']['StopMonitoringDelivery'][0].get('version','not_provided'),
            "timestamp": feed['ServiceDelivery']['ResponseTimestamp'],
            "incrementality": "n/a"
        },
        "entity": []
    }


    for entity in feed_entities:
        entity_dict = {
            "id": entity['MonitoredVehicleJourney']['FramedVehicleJourneyRef']['DatedVehicleJourneyRef'],
            "trip_update": {
                "trip": {
                    "trip_id": entity['MonitoredVehicleJourney']['FramedVehicleJourneyRef']['DatedVehicleJourneyRef'],
                    "start_time": datetime.fromisoformat(entity['MonitoredVehicleJourney']['MonitoredCall'].get('ExpectedDepartureTime',entity['MonitoredVehicleJourney']['MonitoredCall'].get('AimedDepartureTime',None))).timestamp(),
                    "start_date": datetime.fromisoformat(entity['MonitoredVehicleJourney']['MonitoredCall'].get('ExpectedDepartureTime',entity['MonitoredVehicleJourney']['MonitoredCall'].get('AimedDepartureTime',None))).timestamp(),
                    "route_id": entity['MonitoredVehicleJourney']['LineRef'],
                    "direction_id": str(entity['MonitoredVehicleJourney']['DirectionRef'])
                },
                "stop_time_update": [{
                    "stop_sequence": "n.a",
                    "stop_id": stop_id,
                    "arrival": {
                        "delay": '',
                        # ExpectedArrivalTime, the real one: spelt without
                        # its "a" this never matched, so every arrival was
                        # read as the timetable's and no delay ever showed
                        "time": datetime.fromisoformat(entity['MonitoredVehicleJourney']['MonitoredCall'].get('ExpectedArrivalTime',entity['MonitoredVehicleJourney']['MonitoredCall'].get('AimedArrivalTime',None))).timestamp()
                    },
                    "departure": {
                        "delay": '',
                        "time": datetime.fromisoformat(entity['MonitoredVehicleJourney']['MonitoredCall'].get('ExpectedDepartureTime',entity['MonitoredVehicleJourney']['MonitoredCall'].get('AimedDepartureTime',None))).timestamp()
                    }
                }]
            }
        }
        
        json_data["entity"].append(entity_dict)
        
    _LOGGER.debug("json data: %s", json.dumps(json_data))
    return json_data
