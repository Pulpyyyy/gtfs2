import logging
import re
from datetime import datetime, timedelta
import json
import os

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import requests
import voluptuous as vol
from google.protobuf.message import DecodeError
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
from sqlalchemy.sql import text as sql_text

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
_FEED_CACHE: dict[tuple[str, str], tuple[float, object]] = {}
_FEED_CACHE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_FEED_CACHE_GUARD = threading.Lock()
# short enough that a delay stays fresh, long enough to cover a wave of
# coordinators: they were measured starting 12 ms apart
FEED_CACHE_TTL = 30


def get_gtfs_feed_entities(url: str, headers, label: str):
    """Return the feed entities, fetching at most once per TTL and per feed.

    Holds a per-feed lock across the fetch: without it the coordinators, which
    wake within milliseconds of each other, would all miss the cache and
    download in parallel before the first one filled it.
    """
    key = (url, label)
    with _FEED_CACHE_GUARD:
        lock = _FEED_CACHE_LOCKS.setdefault(key, threading.Lock())

    with lock:
        cached = _FEED_CACHE.get(key)
        if cached is not None:
            age = time.time() - cached[0]
            if age < FEED_CACHE_TTL:
                _LOGGER.debug("GTFS RT cache hit for %s (%s), age %.1fs", label, url, age)
                return cached[1]

        entities = _fetch_gtfs_feed_entities(url, headers, label)
        # a failed fetch returns None: do not cache it, the next caller should
        # get a real attempt rather than a stale failure
        if entities is not None:
            _FEED_CACHE[key] = (time.time(), entities)
        return entities


def _fetch_gtfs_feed_entities(url: str, headers, label: str):
    _LOGGER.debug(f"GTFS RT get_feed_entities for url: {url} , headers: {headers}, label: {label}")
    feed = gtfs_realtime_pb2.FeedMessage()  # type: ignore

    if url.startswith('file'):
        requests_session = requests.session()
        requests_session.mount('file://', LocalFileAdapter())
        response = requests_session.get(url)   
    else:
        response = requests.get(url, headers=headers, timeout=20)

    # Success is the status code plus a body that parses below. Grepping the
    # decoded body for error phrases rejected valid feeds whose own free text
    # carried them, e.g. an alert quoting "Not Found".
    if response.status_code == 200:
        _LOGGER.debug("Successfully updated %s", label)
    else:
        _LOGGER.error("Trying to update %s, and got RT response(code): %s with text: %s", label, response.status_code, response.text)
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
        ATTR_NEXT_RT_DELAYS: next_delays                                        
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
    
    if self._vehicle_position_url:   
        vehicle_positions = get_rt_vehicle_positions(self)

    # feed_entities may be passed in by a caller that already fetched/parsed
    # it once for the current refresh cycle (e.g. matching many stops against
    # the same feed), avoiding a re-fetch + re-parse per call.
    if feed_entities is None:
        feed_entities = get_gtfs_feed_entities(
            url=self._trip_update_url, headers=self._headers, label="trip_data"
        )
    self._feed_entities = feed_entities
    
    if not feed_entities:
        _LOGGER.debug("No proper RT feed entities: %s", feed_entities)
        return {}

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
            if not route_id:
                self._rt_group = "trip"   
                route_id = self._route_id                
                
            if self._rt_group == "trip":
                direction_id = self._direction   

            trip_id = entity["trip_update"]["trip"]["trip_id"]
            entity_id = entity["id"]
            
            #_LOGGER.debug("Search for entity with params - group: %s, route_id: %s, direction_id: %s, self_trip_id: %s, with rt trip: %s, rt id: %s", self._rt_group, route_id, direction_id, self._trip_id, entity["trip_update"]["trip"], entity_id)            
                
            # first part covers start/end and thus multiple RT are possible for the same stop, also, for SIRI route_id do not match so a 'in' is used 
            # the second part covers local stops, i.e. per trip, so only one RT possible for that stop         
            if self._rt_group == "route":
                # route-mode, between predefined start/stop
                if direction_id != "nn":
                    matched = (
                        str(direction_id) == str(self._direction)
                        and _same_route(self._route_id, route_id)
                    )  or trip_id in self._trip_list
                else:
                    matched = trip_id == self._trip_id or self._trip_id in trip_id or (trip_id in self._trip_list)
            else:
                # trip-mode, for local stops which can have multiple routes
                matched = trip_id == self._trip_id or entity_id == self._trip_short_name

            if matched:
                _LOGGER.debug("Entity found params - group: %s, route_id: %s, direction_id: %s, self_trip_id: %s, with rt trip: %s, rt id: %s", self._rt_group, route_id, direction_id, self._trip_id, entity["trip_update"]["trip"], entity_id)
                
                for stop in entity["trip_update"]["stop_time_update"]:
                    stop_id = stop["stop_id"]
                    stop_sequence = stop["stop_sequence"]
                    if stop_id == self._stop_id or (stop_id == "" and stop_sequence == self._stop_sequence):
                        _LOGGER.debug("Stop found: %s", stop)
                        # if the data does not contain a stop_id but only a stop_sequence, assume stop_id being the correct stop based on sequence
                        # this does not have to be always correct but best-guess
                        if stop_id == "":
                            stop_id = self._stop_id
                        
                        if self._route_id not in departure_times:
                            departure_times[self._route_id] = {}
                                               
                        if direction_id == "nn" or self._direction in (None, "None") or entity_id == self._trip_short_name or trip_id in self._trip_list: # in this case the trip_id serves as a basis so one can safely set direction to the requesting entity direction; a trip from the entity's own trip list carries the static (possibly repaired) direction, which overrules what the rt feed announces
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
                        
                        # the later of the two 'time' attributes is the one to announce
                        # e.g. at a terminus/layover where the vehicle stands several
                        # minutes at its bay
                        stop_time = max(stop["arrival"]["time"],
                                        stop["departure"]["time"])
                            
                        if stop["departure"].get("delay",0) >= stop["arrival"].get("delay",0):
                            delay = stop["departure"].get("delay",0)
                        else: 
                            delay = stop["arrival"].get("delay",0)
                            
                        # Ignore arrival times in the past
                        departure_dt = dt_util.utc_from_timestamp(stop_time)  # aware UTC, epoch is always UTC
                        if due_in_minutes(departure_dt) >= 0:
                            departure_times[self._route_id][direction_id][stop_id]["departures"].append(departure_dt)
                            # the delay belongs to this departure: appending it
                            # outside this branch kept the delays of departures
                            # that were dropped, so delays[n] described some
                            # other departure than departures[n]
                            departure_times[self._route_id][direction_id][stop_id]["delays"].append(delay)
                            _LOGGER.debug("RT stoptime: %s, in utcfromtimestamp: %s", stop_time, departure_dt)
                        else:
                            _LOGGER.debug("Not using realtime stop data for old due-in-minutes: %s", due_in_minutes(departure_dt))

    # Sort by time, carrying each delay with its own departure: sorting the two
    # lists independently, or only one of them, breaks the pairing again
    for route in departure_times:
        for direction in departure_times[route]:
            for stop in departure_times[route][direction]:
                slot = departure_times[route][direction][stop]
                if len(slot["delays"]) == len(slot["departures"]):
                    paired = sorted(zip(slot["departures"], slot["delays"]),
                                    key=lambda p: p[0])
                    slot["departures"] = [p[0] for p in paired]
                    slot["delays"] = [p[1] for p in paired]
                else:
                    slot["departures"].sort()

    self.info = departure_times
    _LOGGER.debug("Departure times Route Trip: %s", departure_times)
    return departure_times    

def get_rt_vehicle_positions(self):
    feed_entities = get_gtfs_feed_entities(
        url=self._vehicle_position_url,
        headers=self._headers,
        label="vehicle_positions",
    )
    geojson_body = []
    geojson_element = {"geometry": {"coordinates":[],"type": "Point"}, "properties": {"id": "", "title": "", "trip_id": "", "route_id": "", "direction_id": "", "vehicle_id": "", "vehicle_label": ""}, "type": "Feature"}
    if not feed_entities:
        # a failed fetch returns None: iterating it raises, and the caller's
        # broad except then abandons the whole realtime block, so a hiccup on
        # vehicle-positions used to take the departure times down with it
        _LOGGER.debug("No proper RT feed entities for vehicle positions")
        return geojson_body
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
    
def _alert_kind(alert):
    """The GTFS-RT cause and effect of an alert, as their spec names.

    The feed carries far more than the sentence gtfs2 keeps: a cause out of
    twelve (STRIKE, ACCIDENT, WEATHER, CONSTRUCTION...) and an effect out of
    eleven (NO_SERVICE, DETOUR, SIGNIFICANT_DELAYS...). A card cannot draw
    "roadworks" from a free sentence, but it can from these.

    UNKNOWN_CAUSE and UNKNOWN_EFFECT are dropped: they are the proto's default
    for a field the feed never set, so publishing them would put a value on an
    attribute that has nothing to say. Absent means "the feed did not say".
    """
    out = {}
    for field in ("cause", "effect"):
        value = getattr(alert, field, None)
        if value is None:
            continue
        try:
            name = alert.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number[value].name
        except Exception:  # pylint: disable=broad-except
            # an enum value this binding does not know: the spec grows, and a
            # number nobody can name is not worth failing an update over
            _LOGGER.debug("Unknown alert %s value: %s", field, value)
            continue
        if name and not name.startswith("UNKNOWN"):
            out[field] = name
    return out


# The GTFS-RT effects, most disruptive first. Several alerts reach the same
# journey at once and only one sentence fits in an attribute, so the order is
# decided here rather than left to the feed: on the SNCF feed the seasonal
# notice "Service Velos 2026" names 613 trips one by one with no cause and no
# effect, and it hid a cancellation on the same train just by coming first.
_ALERT_EFFECT_ORDER = (
    "NO_SERVICE",
    "SIGNIFICANT_DELAYS",
    "DETOUR",
    "REDUCED_SERVICE",
    "MODIFIED_SERVICE",
    "STOP_MOVED",
    "ACCESSIBILITY_ISSUE",
    "ADDITIONAL_SERVICE",
    "OTHER_EFFECT",
    "NO_EFFECT",
)

# an attribute is read at a glance on a card, not paged through
_ALERTS_KEPT = 5


def _alert_severity(item):
    """Rank of one alert. An effect the feed never stated comes last: _alert_kind
    drops UNKNOWN_EFFECT, so a missing key means the feed said nothing, not that
    nothing is happening."""
    try:
        return _ALERT_EFFECT_ORDER.index(item.get("effect"))
    except ValueError:
        return len(_ALERT_EFFECT_ORDER)


def _rank_alerts(items):
    """The alerts of one end of the journey, worst first and without repeats.

    SNCF publishes the same alert under two ids, word for word, and the reader
    would see the sentence twice; text, cause and effect together are what one
    can tell apart. The sort is stable, so at equal effect the feed's own order
    still decides, and the cap is applied last so what is kept is the worst.
    """
    seen = set()
    unique = []
    for item in items:
        key = (item.get("text", ""), item.get("cause"), item.get("effect"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=_alert_severity)
    return unique[:_ALERTS_KEPT]


# the station of a stop does not change while the datasource does not, so the
# lookup is done once per stop and kept, keyed by datasource
_STOP_ALIASES = {}


def _same_trip(named, trip_id):
    """Whether an alert trip selector names the trip being watched.

    Exact first. Then the truncated form: SNCF calls a train OCESN853603F in
    its alerts and OCESN853603F1187_F:TER:... in its timetable, the same train
    under an id the alert feed cuts before the agency. The guard is that what
    follows the prefix has to be a digit, the start of that agency id, without
    which train 105 would swallow train 1052. Measured over a whole feed: 6805
    of the 7061 trips named by an alert resolve this way, none ambiguously.
    """
    if not named or not trip_id:
        return False
    if named == trip_id:
        return True
    if not trip_id.startswith(named) or len(trip_id) <= len(named):
        return False
    return trip_id[len(named)].isdigit()


def _stop_aliases(self, stop_id):
    """The ids a stop can be named by: its own, and the station above it.

    Feeds derived from NeTEx publish a station and each of its platforms as
    separate stops. The timetable is built on the platform while the alerts
    name the station, so an alert about your own station never matched the stop
    the departure came from. Reading the parent puts the two back together, and
    it is exact: nothing is guessed from the shape of an id.
    """
    stop_id = str(stop_id or "")
    if not stop_id:
        return set()
    data = getattr(self, "_data", None) or {}
    schedule = data.get("schedule")
    if schedule is None:
        return {stop_id}
    key = (data.get("file"), stop_id)
    if key in _STOP_ALIASES:
        return _STOP_ALIASES[key]
    aliases = {stop_id}
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                sql_text("select parent_station from stops where stop_id = :stop_id"),
                {"stop_id": stop_id}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # a locked or pruned datasource is no reason to lose the alerts the
        # stop itself is named in, and a failure is not worth remembering
        _LOGGER.debug("Could not read the station of stop %s: %s", stop_id, ex)
        return aliases
    for row in rows:
        if row[0]:
            aliases.add(str(row[0]))
    _STOP_ALIASES[key] = aliases
    return aliases


def _journey_stops(self):
    """Every stop of the journey, from where you get on to where you get off.

    An alert can name a station in the middle of the run: a lift out of order
    where you change, a train held two stops before yours. That concerns the
    journey as much as an alert on either end does, and reading only the two
    ends dropped all of it.

    Not cached, unlike the station of a stop: the journey belongs to the next
    departure, so the key would change with every trip and the cache would only
    grow. One indexed lookup on trip_id is cheaper than that.
    """
    data = getattr(self, "_data", None) or {}
    schedule = data.get("schedule")
    departure = data.get("next_departure") or {}
    trip_id = departure.get("trip_id") or getattr(self, "_trip_id", None)
    first = departure.get("origin_stop_sequence")
    last = (departure.get("destination_stop_time") or {}).get("Sequence")
    if schedule is None or not trip_id or first is None or last is None:
        return set()
    stops = set()
    try:
        with schedule.engine.connect() as conn:
            rows = conn.execute(
                sql_text("select s.stop_id, s.parent_station from stop_times st "
                         "inner join stops s on s.stop_id = st.stop_id "
                         "where st.trip_id = :trip_id "
                         "and st.stop_sequence >= :first "
                         "and st.stop_sequence <= :last"),
                {"trip_id": trip_id, "first": first, "last": last}).fetchall()
    except Exception as ex:  # pylint: disable=broad-except
        # the two ends are still read without this, so a datasource that cannot
        # answer costs the middle of the journey and nothing else
        _LOGGER.debug("Could not read the stops of trip %s: %s", trip_id, ex)
        return stops
    for stop_id, parent in rows:
        stops.add(str(stop_id))
        if parent:
            stops.add(str(parent))
    return stops


def _alert_language(self):
    """The language to read an alert in: the one Home Assistant is set to."""
    config = getattr(getattr(self, "hass", None), "config", None)
    return getattr(config, "language", None) or "en"


def _alert_text(translated, language):
    """One TranslatedString, in the wanted language, as plain text.

    The order of the translations belongs to the feed, not to the reader: SNCF
    puts German first on 328 of its 440 alerts while publishing feed_lang fr,
    so taking whichever came first showed German to a French user three times
    out of four. The first translation stays the fallback, for a feed that
    labels none of them.

    This also replaces splitting the protobuf debug rendering on the literal
    text marker, which took whatever came first and dropped every colon of the
    sentence on the way out.
    """
    translations = list(translated.translation)
    if not translations:
        return ""
    wanted = (language or "").lower()
    if wanted:
        for candidate in (wanted, wanted.split("-")[0]):
            for translation in translations:
                spoken = (translation.language or "").lower()
                if spoken == candidate or spoken.split("-")[0] == candidate:
                    return translation.text.strip()
    return translations[0].text.strip()


def _alert_scope(alert, origin_ids, destination_ids, route_id, trip_id=None,
                 journey_ids=None):
    """Which end of this journey an alert names, over ALL its informed entities.

    The loop used to reassign stop_id and route_id on every turn and compare
    only once it had ended, so an alert naming ten stops was matched on the
    tenth alone: yours had to be last in the list, or the alert was dropped in
    silence. An alert for a whole network names many stops.

    origin_ids and destination_ids are sets because a stop can be named by more
    than one id, see _stop_aliases. journey_ids holds everything in between, so
    that a stop the journey merely passes through counts too. And a trip is
    read because an alert is not obliged to name a stop or a line at all: SNCF
    addresses 385 of its 440 alerts to trips alone, and looking only at stop_id
    and route_id made every one of them invisible.
    """
    journey_ids = journey_ids or set()
    hits = {"origin": False, "destination": False, "route": False,
            "trip": False, "journey": False}
    for x in alert.informed_entity:
        e_stop = x.stop_id if x.HasField("stop_id") else None
        e_route = x.route_id if x.HasField("route_id") else None
        e_trip = x.trip.trip_id if x.HasField("trip") else None
        if e_route is not None and e_route != str(route_id):
            continue                      # an alert about another line
        if e_trip and _same_trip(e_trip, str(trip_id or "")):
            hits["trip"] = True
        if e_stop is not None and e_stop in origin_ids:
            hits["origin"] = True
        elif e_stop is not None and e_stop in destination_ids:
            hits["destination"] = True
        elif e_stop is not None and e_stop in journey_ids:
            hits["journey"] = True
        elif e_stop is None and e_route == str(route_id):
            hits["route"] = True
    return hits


def get_rt_alerts(self):
    rt_alerts = {}
    # an entry created before this option existed has no alerts_url at all, and
    # subscripting None raised, which cost that entry its whole realtime block
    if str(self._alerts_url or "")[:4] == "http":
        feed_entities = get_gtfs_feed_entities(
            url=self._alerts_url,
            headers=self._headers,
            label="alerts",
        )
        if not feed_entities:
            _LOGGER.debug("No proper RT feed entities for alerts")
            return rt_alerts
        origin_ids = _stop_aliases(self, self._stop_id)
        destination_ids = _stop_aliases(self, self._destination_id)
        # the destination the flow stored can be a station name rather than an
        # id, which never matched anything; the departure knows the real one
        arrival = ((getattr(self, "_data", None) or {})
                   .get("next_departure") or {}).get("destination_stop_id")
        if arrival:
            destination_ids |= _stop_aliases(self, arrival)
        journey_ids = _journey_stops(self)
        language = _alert_language(self)
        origin_alerts = []
        destination_alerts = []
        for entity in feed_entities:
            if not entity.HasField("alert"):
                continue
            alert = entity.alert
            hits = _alert_scope(alert, origin_ids, destination_ids,
                                self._route_id, getattr(self, "_trip_id", None),
                                journey_ids)
            if not any(hits.values()):
                continue
            # an alert with no readable header still carries its cause and its
            # effect, and it does not take a sentence to say that something is
            # going on
            item = {"text": _alert_text(alert.header_text, language)}
            item.update(_alert_kind(alert))
            _LOGGER.debug("RT Alert for route: %s, scope: %s, alert: %s", self._route_id, hits, alert.header_text)
            # an alert about the line, about the train itself, or about a stop
            # somewhere along the way speaks for the whole journey
            whole_journey = hits["route"] or hits["trip"] or hits["journey"]
            if hits["origin"] or whole_journey:
                origin_alerts.append(item)
            if hits["destination"] or whole_journey:
                destination_alerts.append(item)
        origin_alerts = _rank_alerts(origin_alerts)
        destination_alerts = _rank_alerts(destination_alerts)
        # A journey can be under several alerts at once and the strings hold one
        # sentence each, so they take the worst of them instead of whichever the
        # feed published last. The lists carry the rest, in the same order.
        if origin_alerts:
            rt_alerts["origin_stop_alerts"] = origin_alerts
            rt_alerts["origin_stop_alert"] = origin_alerts[0]["text"]
        if destination_alerts:
            rt_alerts["destination_stop_alerts"] = destination_alerts
            rt_alerts["destination_stop_alert"] = destination_alerts[0]["text"]
        # cause and effect have to describe the alert the sentence comes from.
        # Taken from two different alerts, as they were, a card that styles
        # itself on them paints a service notice as an incident. Origin first,
        # because that is the sentence a start/stop card reads.
        head = (origin_alerts or destination_alerts or [{}])[0]
        for field in ("cause", "effect"):
            if field in head:
                rt_alerts["alert_" + field] = head[field]

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
            _LOGGER.error("Ìssues with downloading GTFS RT SIRI data to: %s with error: 5s", os.path.join(gtfs_dir, file), ex)
            return "no_rt_data_file" 
        return "ok"                                
    try:
        r = requests.get(url, headers = _headers , allow_redirects=True)
        open(os.path.join(gtfs_dir, file), "wb").write(r.content)
        if r.status_code != 200:
            _LOGGER.error("Ìssues with downloading GTFS RT data, error: %s, content: %s", r.status_code, r.content)
            return "no_rt_data_file"
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
        for stop_time_update in entity.trip_update.stop_time_update:
            stop_time_update_dict = {
                "stop_sequence": stop_time_update.stop_sequence,
                "stop_id": stop_time_update.stop_id,
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

def convert_gtfs_realtime_alerts_to_json(gtfs_realtime_data):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(gtfs_realtime_data)

    json_data = {
        "entity": []
    }
    for entity in feed.entity:
        _LOGGER.debug("Alert entity: %s", entity)
        if entity.HasField('alert'):
            informed_entities = []
            for informed_entity in entity.alert.informed_entity:
                informed_entity_json = {
                        "route_id": informed_entity.route_id,
                        "trip_id": informed_entity.trip.trip_id
                    }
                informed_entities.append(informed_entity_json)
            entity_dict = {
                "alert": {
                    "id": entity.id,
                    #"active_period": {
                    #    "start": entity.alert.active_period.start,
                    #    "end": entity.alert.active_period.end
                    #},
                    "informed_entity": informed_entities,
                    "header_text": entity.alert.header_text,
                    "description_text": entity.alert.description_text
                }   
            }
        json_data["entity"].append(entity_dict)
        _LOGGER.debug("Alert entity JSON: %s", json_data["entity"])
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
    
    url = url + f"&MonitoringRef={stop_id}"
    response = requests.get(url, headers=headers, timeout=20)

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
                        "time": datetime.fromisoformat(entity['MonitoredVehicleJourney']['MonitoredCall'].get('ExpectedArrivlTime',entity['MonitoredVehicleJourney']['MonitoredCall'].get('AimedArrivalTime',None))).timestamp()
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
