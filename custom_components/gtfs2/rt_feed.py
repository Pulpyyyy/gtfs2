"""Reading a realtime feed, for every reader in gtfs_rt_helper.

One download per feed and per publication, shared by every sensor that
reads it (get_gtfs_feed_entities); a failure said once, not once per
sensor and per minute; and the body, json or protobuf, decoded into what
the readers walk (convert_gtfs_realtime_to_json,
convert_gtfs_realtime_positions_to_json), with the GTFS-RT enums spelled
out (trip_relationship, stop_relationship). Also how a route id the feed
gives names a configured route (_same_route), for the trip updates and
the alerts alike, the time and delay a stop update gives
(stop_update_clock) and whether the day a feed names for a trip is the
service day (on_service_day).
"""
from collections.abc import Collection, Mapping, Sequence
import json
import logging
import threading
import time
from typing import Any

import requests
from google.transit import gtfs_realtime_pb2

from .key_mask import fetch

_LOGGER = logging.getLogger(__name__)

# what a feed is read into: for the trip updates and the vehicles, the
# dicts convert_gtfs_realtime_to_json and its sibling write; for the
# alerts, the protobuf messages themselves
type FeedEntities = Sequence[Any]


# One GTFS-RT feed covers a whole network, so every sensor reading the same
# provider asks for the same bytes. Each coordinator used to download and parse
# it for itself, once a minute: on a 1.6 MiB feed with six entries that is
# about 13.8 GiB a day, and the protobuf to json conversion dominates the CPU.
#
# The feed publishes neither ETag nor Last-Modified, so conditional requests
# are impossible and a local cache is the only way to avoid the repeat.
_FEED_CACHE: dict[tuple[str, str, str], tuple[float, FeedEntities]] = {}
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
# A feed says when it was published (header.timestamp), and most publish
# on a steady beat: IDFM's 7.3 MB gateway once a minute, 2 to 10 s past
# it (measured 2026-09-27). The coordinators tick once a minute each but
# at the second they were set up, so a 30 s cache let two downloads a
# minute through, one of them the same bytes again. A feed whose beat is
# known is kept until its next publication is due, plus the lag it
# shows, and never longer than FEED_CACHE_MAX_AGE; one without a
# timestamp keeps the 30 s. {key: (published, beat)}, epoch seconds.
_FEED_PUBLISHED: dict[tuple[str, str, str], tuple[int, int | None]] = {}
FEED_PUBLISH_LAG = 10
FEED_CACHE_MAX_AGE = 75
FEED_BEAT_MAX = 300

RT_USER_AGENT = "GTFS2-HomeAssistant/1.0 (+https://github.com/vingerha/gtfs2)"


def _with_user_agent(headers: Mapping[str, str] | None) -> dict[str, str]:
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


def get_gtfs_feed_entities(url: str, headers: Mapping[str, str] | None, label: str,
                           owner: str = "") -> FeedEntities | None:
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
        if cached is not None and _still_current(key, cached[0]):
            _LOGGER.debug("GTFS RT cache hit for %s (%s), age %.1fs", label, url, time.time() - cached[0])
            return cached[1]

        failed = _FEED_FAILED.get(key)
        if failed is not None and time.time() - failed < FEED_FAIL_TTL:
            # the last attempt just failed: a host that is down answers every
            # sensor of the source the same way, and each of them waiting out
            # the timeout in turn holds an executor thread for nothing
            _LOGGER.debug("GTFS RT %s (%s) failed %.1fs ago, not asked again yet",
                          label, url, time.time() - failed)
            return None

        entities, published = _fetch_feed(url, headers, label)
        # a failed fetch returns None: it is not kept as data, only as the
        # memory of a failure, so the next caller gets a real attempt once
        # the short wait is over rather than a stale answer
        if entities is not None:
            _note_publication(key, published)
            _FEED_CACHE[key] = (time.time(), entities)
            _FEED_FAILED.pop(key, None)
        else:
            _FEED_FAILED[key] = time.time()
        return entities


def _still_current(key: tuple[str, str, str], fetched: float) -> bool:
    """Whether a feed read at `fetched` is still the latest one: younger
    than FEED_CACHE_TTL, or, its beat known, its next publication not due
    yet (_FEED_PUBLISHED)."""
    age = time.time() - fetched
    if age < FEED_CACHE_TTL:
        return True
    published, beat = _FEED_PUBLISHED.get(key, (None, None))
    if not published or not beat or age >= FEED_CACHE_MAX_AGE:
        return False
    return time.time() < published + beat + FEED_PUBLISH_LAG


def _note_publication(key: tuple[str, str, str], published: int | None) -> None:
    """Keep a feed's publication time and learn its beat: the shortest gap
    seen between two publications, so a reading that missed one does not
    stretch it."""
    if not published:
        _FEED_PUBLISHED.pop(key, None)
        return
    last, beat = _FEED_PUBLISHED.get(key, (None, None))
    if last and published > last:
        gap = published - last
        beat = min(beat, gap) if beat else gap
        beat = beat if beat <= FEED_BEAT_MAX else None
    elif last and published < last:
        # a feed going back in time: start again
        beat = None
    _FEED_PUBLISHED[key] = (published, beat)


def _forget_old_feeds(current: tuple[str, str, str]) -> None:
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
            _FEED_PUBLISHED.pop(key, None)
            _LOGGER.debug("Forgot the feed nothing reads any more: %s", key[2])
    for key, when in list(_FEED_FAILED.items()):
        if key != current and when < old:
            del _FEED_FAILED[key]
            _FEED_CACHE_LOCKS.pop(key, None)


# what each realtime url last failed with, while it fails: every entry
# reading a source fetches its feeds each cycle, and an outage of the host
# wrote the same error once per entry and per minute
_FAILING: dict[str, str] = {}


def _say_failure(url: str, message: str, *args: object) -> None:
    """Log a realtime failure when it is new for the url, debug after."""
    text = message % args
    if _FAILING.get(url) != text:
        _LOGGER.error(text)
        _FAILING[url] = text
    else:
        _LOGGER.debug(text)


def _say_recovered(url: str, label: str) -> None:
    if _FAILING.pop(url, None) is not None:
        _LOGGER.info("The %s feed at %s answers again", label, url)


def _feed_body(url: str, headers: Mapping[str, str] | None, label: str) -> bytes | None:
    """The bytes of a realtime feed, from its host or from the file a
    file:// url names; None, the failure said, when there are none."""
    try:
        if url.startswith("file://"):
            # a feed on disk, named by a file:// url: read as a file, it
            # needs no http round of its own
            with open(url[len("file://"):], "rb") as local:
                content = local.read()
            _LOGGER.debug("Successfully updated %s", label)
            return content
        response = fetch("get", url, headers=_with_user_agent(headers), timeout=20)
    except (requests.RequestException, OSError) as ex:
        # a host that is down, a name that no longer resolves, a certificate
        # that expired, a local copy gone: the caller reads None as "no
        # realtime this cycle", which is what it already did for a bad response
        _say_failure(url, "Could not reach %s for %s: %s", url, label, type(ex).__name__)
        return None

    # Success is the status code plus a body that parses below. Grepping the
    # decoded body for error phrases rejected valid feeds whose own free text
    # carried them, e.g. an alert quoting "Not Found".
    if response.status_code == 200:
        _LOGGER.debug("Successfully updated %s", label)
        return response.content
    # the first line of the body says what went wrong; a maintenance page
    # in full says it again in a hundred lines of html
    _say_failure(url, "Trying to update %s, and got RT response(code): %s with text: %s",
                 label, response.status_code, response.text[:200])
    return None


def _json_feed_entities(url: str, label: str, content: bytes) -> FeedEntities | None:
    """The entities of a json feed; None, the failure said, when it is not one."""
    try:
        feed = json.loads(content)
    except ValueError:
        _say_failure(url, "Trying to update %s, and got a 200 whose body is broken json", label)
        return None
    if label == "alerts" and isinstance(feed, dict):
        # the alert reader walks protobuf messages, HasField and all:
        # handed dicts it raised, and the realtime of the cycle went
        # with it. A json feed is read into the message it stands for
        try:
            from google.protobuf import json_format
            message = gtfs_realtime_pb2.FeedMessage()
            json_format.ParseDict(feed, message, ignore_unknown_fields=True)
            # an answer again, as on every other path: the outage kept
            # otherwise, and its next one was only said at debug level
            _say_recovered(url, label)
            return message.entity
        except Exception as ex:  # pylint: disable=broad-except
            _say_failure(url, "Trying to update %s, and got json that is not a GTFS-RT feed: %s",
                         label, type(ex).__name__)
            return None
    _say_recovered(url, label)
    return feed.get('entity') if isinstance(feed, dict) else None


def _protobuf_feed_entities(url: str, label: str,
                            content: bytes) -> tuple[FeedEntities | None, int | None]:
    """(the entities of a protobuf feed, the trip updates and the vehicles
    as dicts, the alerts as messages; when its header says it was
    published, None when it does not). (None, None), the failure said,
    when it is not one."""
    # Imported here and not at module level: the class lives in protobuf,
    # which arrives with gtfs-realtime-bindings, and the synthetic suite
    # stubs those bindings out.
    from google.protobuf.message import DecodeError
    _LOGGER.debug("GTFS RT data is not providing format json")
    # a maintenance or error page served with a 200 lands here and is not
    # protobuf either: degrade to no data instead of an uncaught traceback
    try:
        if label == "vehicle_positions":
            feed = convert_gtfs_realtime_positions_to_json(content)
        elif label == "trip_data":
            feed = convert_gtfs_realtime_to_json(content)
        else: # not yet converted to json
            message = gtfs_realtime_pb2.FeedMessage()  # type: ignore
            message.ParseFromString(content)
            _say_recovered(url, label)
            return message.entity, int(message.header.timestamp) or None
    except DecodeError:
        _say_failure(url, "Trying to update %s, and got a 200 whose body is neither json nor GTFS-RT protobuf", label)
        return None, None
    _say_recovered(url, label)
    return feed.get('entity'), int((feed.get("header") or {}).get("timestamp") or 0) or None


def _fetch_gtfs_feed_entities(url: str, headers: Mapping[str, str] | None, label: str) -> FeedEntities | None:
    return _fetch_feed(url, headers, label)[0]


def _fetch_feed(url: str, headers: Mapping[str, str] | None, label: str) -> tuple[FeedEntities | None, int | None]:
    """(the entities of a feed, when it says it was published), (None,
    None) when it could not be read."""
    _LOGGER.debug(f"GTFS RT get_feed_entities for url: {url} , headers: {headers}, label: {label}")
    content = _feed_body(url, headers, label)
    if content is None:
        return None, None
    # json or protobuf: a json body opens with a brace or a bracket. Asking
    # response.text of a protobuf first ran the charset detection over
    # megabytes of binary, then parsed the result twice, for nothing
    if content.lstrip()[:1] in (b"{", b"["):
        return _json_feed_entities(url, label, content), None
    return _protobuf_feed_entities(url, label, content)


# the names of the GTFS-RT enums, spelled out so a reader (and a card
# reading the leg file) never sees a bare number; the SIRI path writes
# none of them, so every reader takes SCHEDULED for a missing key
CANCELLED_TRIP = ("CANCELED", "DELETED")
SKIPPED_STOP = "SKIPPED"
NO_DATA_STOP = "NO_DATA"


def _relationship(message: gtfs_realtime_pb2.TripDescriptor
                  | gtfs_realtime_pb2.TripUpdate.StopTimeUpdate) -> str:
    """The schedule_relationship of a trip or a stop time update, by name."""
    try:
        return message.ScheduleRelationship.Name(message.schedule_relationship)
    except (AttributeError, ValueError):
        return "SCHEDULED"


def trip_relationship(entity: Mapping[str, Any]) -> str:
    """The trip's schedule_relationship out of a converted entity, SCHEDULED
    when the feed (or the SIRI path) says nothing."""
    return ((entity.get("trip_update") or {}).get("trip") or {}).get(
        "schedule_relationship") or "SCHEDULED"


def stop_relationship(stop_time_update: Mapping[str, Any] | None) -> str:
    """The stop update's schedule_relationship, SCHEDULED when unsaid."""
    return (stop_time_update or {}).get("schedule_relationship") or "SCHEDULED"


def convert_gtfs_realtime_to_json(gtfs_realtime_data: bytes) -> dict[str, Any]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(gtfs_realtime_data)

    json_data: dict[str, Any] = {
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
        entity_dict["trip_update"]["trip"]["schedule_relationship"] = _relationship(
            entity.trip_update.trip)
        for stop_time_update in entity.trip_update.stop_time_update:
            stop_time_update_dict = {
                "stop_sequence": stop_time_update.stop_sequence,
                "stop_id": stop_time_update.stop_id,
                # SCHEDULED, SKIPPED (the vehicle does not call), NO_DATA
                # (no prediction here, the timetable stands), UNSCHEDULED
                "schedule_relationship": _relationship(stop_time_update),
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

def convert_gtfs_realtime_positions_to_json(gtfs_realtime_data: bytes) -> dict[str, Any]:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(gtfs_realtime_data)

    json_data: dict[str, Any] = {
        # when the feed was published, for the feed cache
        "header": {"timestamp": feed.header.timestamp},
        "entity": []
    }
    for ent in feed.entity:
        entity = ent.vehicle
        entity_dict = {
        "vehicle": {
            "trip": {
                "trip_id" : entity.trip.trip_id,
                "route_id": entity.trip.route_id,
                # None when the feed does not say: protobuf reads an absent
                # field as 0, which put every vehicle of a feed that gives no
                # direction on the outbound map, the same trap the trip
                # updates already step around
                "direction_id": (entity.trip.direction_id
                                 if entity.trip.HasField("direction_id") else None)
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


def _same_route(configured: str | None, seen: str | None) -> bool:
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


def stop_update_clock(stop: Mapping[str, Any]) -> tuple[int, int]:
    ''' (time, delay) of a stop update: the departure's when it says
    anything, the arrival's otherwise; 0 for what it leaves out '''
    # a train that arrives late and makes up time while it stands at
    # the stop leaves with the departure's delay, not the arrival's.
    # A json feed writes its int64 times as strings
    arrival = stop.get("arrival") or {}
    departure = stop.get("departure") or {}
    told = departure if (departure.get("time") or departure.get("delay")) else arrival
    return (int(departure.get("time") or arrival.get("time") or 0),
            int(told.get("delay") or 0))


def delay_of(delay: int | None, realtime: int | None, scheduled: int | None) -> int | None:
    """A call's delay in seconds: the feed's, else, when it gives none or a
    zero one, the gap between the time it gives and the timetable's, both
    epoch seconds. IDFM's gateway writes 0 for a metro two minutes late,
    TAO and Palm Bus leave the delay out."""
    if not delay and realtime and scheduled:
        return realtime - scheduled
    return delay


def on_service_day(start_date: str | Collection[str] | None, service_day: str | None) -> bool:
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
