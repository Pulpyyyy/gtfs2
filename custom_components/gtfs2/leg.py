"""The leg file under www/gtfs2: the ride of an entry's next departure,
timed stop by stop, realtime included (write_leg_file).

Its name, so that the writer, the sensor attribute and the removal on
entry deletion agree (leg_geojson_name, leg_geojson_pattern,
owns_leg_file). Written by exports.export_leg from the executor.
"""
from __future__ import annotations

import datetime
import glob
import logging
import os

from sqlalchemy.sql import text
import homeassistant.util.dt as dt_util

from .const import DEFAULT_PATH_GEOJSON
from .geojson import entry_file_part, safe_file_part, write_json_if_changed
from .gtfs_helper import agency_zone, gtfs_seconds, shown_ends
from .places import _call_type
from .rt_feed import (
    CANCELLED_TRIP, NO_DATA_STOP, SKIPPED_STOP, stop_relationship, stop_update_clock,
    trip_relationship,
)

_LOGGER = logging.getLogger(__name__)


# how many of the listed departures the leg file times stop by stop: a board
# shows a handful, and a day's worth of runs on a busy line is a file
# rewritten every minute for nothing
LEG_TRIPS_MAX = 20


def leg_geojson_name(route_id, direction, name):
    """File name of the leg export: the line and direction first, so a
    folder listing reads a line's files together, then the entry's name,
    since it describes what THIS sensor rides and two entries of one line
    must not overwrite each other's. Kept in one place, like the other
    two, so the writer and the sensor attribute agree; the removal finds
    it back by its entry part alone (see leg_geojson_pattern)."""
    return f"{safe_file_part(route_id)}_{safe_file_part(direction)}_leg_{entry_file_part(name)}.json"


# the direction part of a leg file name: what str() makes of a departure's
# trip_direction_id, or of an entry without one
LEG_DIRECTIONS = ("0", "1", "none")


def leg_geojson_pattern(name) -> tuple[str, ...]:
    """The globs that find an entry's leg file whatever line it was written
    under: a train entry's departure may name a route the entry does not.

    One glob per direction rather than one on the entry part alone: an
    entry called "x leg a" writes ..._leg_x_leg_a.json, which a bare
    *_leg_a.json matched, so removing the entry called "a" took the other
    entry's file with it. The direction sits between the two, and a name
    cannot forge it there.
    """
    part = glob.escape(entry_file_part(name))
    return tuple(f"*_{direction}_leg_{part}.json" for direction in LEG_DIRECTIONS)


def owns_leg_file(path, name) -> bool:
    """Whether a file a leg glob found is this entry's own.

    The glob's star takes anything, another entry's name included: "Tram 1
    leg Centre" writes r_0_leg_tram_1_leg_centre.json, which the "Centre"
    glob *_1_leg_centre.json finds. What stands before the entry's own
    ending is a line id, which never holds a leg ending of its own: a
    prefix that does is the start of another entry's file.
    """
    part = entry_file_part(name)
    base = os.path.basename(path)
    for direction in LEG_DIRECTIONS:
        ending = f"_{direction}_leg_{part}.json"
        if base.endswith(ending):
            prefix = f"_{base[:-len(ending)]}_"
            return not any(f"_{d}_leg_" in prefix for d in LEG_DIRECTIONS)
    return False


def _leg_timezone(schedule, route_id, departure, hass):
    """The zone the line's clocks are written in: the agency's, as the
    departure query reads it, else the origin stop's, else Home Assistant's."""
    zone = agency_zone(schedule, route_id)
    if zone is not None:
        return zone
    name = departure.get("origin_stop_timezone") or hass.config.time_zone
    return dt_util.get_time_zone(name) or datetime.timezone.utc


def _listed_trips(departure):
    """The trips a leg file times, the ridden one first, and when each
    leaves the origin, as the sensor says it."""
    trip_id = str(departure.get("trip_id") or "") or None
    trip_ids = []
    for t in [trip_id] + list(departure.get("next_departures_trip_id") or []):
        if t and str(t) not in trip_ids:
            trip_ids.append(str(t))
    leaves = {}
    for t, when in zip(departure.get("next_departures_trip_id") or [], departure.get("next_departures") or []):
        leaves.setdefault(str(t), when)
    if trip_id and departure.get("departure_time"):
        first = departure["departure_time"]
        leaves.setdefault(trip_id, first.isoformat() if hasattr(first, "isoformat") else str(first))
    return trip_id, trip_ids[:LEG_TRIPS_MAX], leaves


def _read_trip_calls(schedule, trip_ids, origin_id):
    """The calls of each trip, in their order, and the station the origin
    belongs to."""
    stops_by_trip = {}
    if not trip_ids:
        return stops_by_trip, None
    params = {f"t{i}": t for i, t in enumerate(trip_ids)}
    sql = f"""
    SELECT st.trip_id, st.stop_id, s.stop_name, s.stop_lat, s.stop_lon,
           st.stop_sequence, st.arrival_time, st.departure_time, s.parent_station,
           st.pickup_type, st.drop_off_type
    FROM stop_times st
    JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.trip_id IN ({", ".join(":" + k for k in params)})
    ORDER BY st.trip_id, st.stop_sequence
    """  # noqa: S608
    with schedule.engine.connect() as conn:
        for row in conn.execute(text(sql), params).fetchall():
            stops_by_trip.setdefault(str(row[0]), []).append(row)
        parent = conn.execute(text("SELECT parent_station FROM stops WHERE stop_id = :s"),
                              {"s": origin_id}).fetchone()
    return stops_by_trip, (parent[0] if parent and parent[0] else None)


def _origin_call(rows, origin_id, origin_parent):
    """The trip's call at the origin: the entry's record, else a platform
    of the same station (the trip may serve a sibling record), else None."""
    origin_row = next((r for r in rows if str(r[1]) == origin_id), None)
    if origin_row is None and origin_parent:
        origin_row = next((r for r in rows if r[8] == origin_parent), None)
    return origin_row


def _service_midnight(when, rows, origin_row, zone):
    """The service day's midnight of a trip, in the line's zone: the
    origin's departure, as listed, minus the origin's stored clock. Without
    a call at the origin, the trip's first stop stands in."""
    if not when:
        return None
    if origin_row is None:
        origin_row = rows[0]
    seconds = gtfs_seconds(origin_row[7])
    if seconds is None:
        return None
    try:
        local = datetime.datetime.fromisoformat(str(when)).astimezone(zone)
    except ValueError:
        return None
    return (local - datetime.timedelta(seconds=seconds)).replace(hour=0, minute=0, second=0, microsecond=0)


def _leg_time(midnight, stored):
    seconds = gtfs_seconds(stored)
    if midnight is None or seconds is None:
        return None
    return (midnight + datetime.timedelta(seconds=seconds)).astimezone(datetime.timezone.utc).isoformat()


def _boarding_first(rows, origin_row):
    """The trip's calls, the ride's own first.

    A stop is a key here, so a trip calling twice at one stop can only
    keep one of its two calls, and a loop line calls at its terminus
    twice. The one that counts is the one the rider makes, so the
    calls from the origin onwards come first and the ones before it
    follow: reading them in order then keeps the right one, where the
    plain feed order kept whichever came last, the return pass.
    """
    if origin_row is None:
        return rows
    return sorted(rows, key=lambda r: (r[5] < origin_row[5], r[5]))


def _leg_call(midnight, r):
    return {
        "sequence": r[5],
        "scheduled_arrival": _leg_time(midnight, r[6]),
        "scheduled": _leg_time(midnight, r[7]),
        # as the feed flags the call: 0 regular, 1 none, 2 phone
        # ahead, 3 tell the driver; a card chaining legs picks its
        # ends among the calls the rider can make (see _boards)
        "pickup_type": _call_type(r[9]),
        "drop_off_type": _call_type(r[10]),
    }


def _leg_features(rows, midnight, trip_id, route_id, direction):
    """The ridden trip's calls as map points."""
    features = []
    for r in rows:
        # each point carries its own call, not the one the stop
        # keeps: on a loop the two differ
        call = _leg_call(midnight, r)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r[4], r[3]]},
            "properties": {
                "id": f"{route_id}_{direction}_{r[5]}",
                # a stop the feed left unnamed falls back on its id,
                # here as in the route file
                "title": f"{r[2] or r[1]}_stop",
                "trip_id": trip_id,
                "stop_id": r[1],
                "stop_name": r[2] or r[1],
                "stop_sequence": r[5],
                "scheduled_arrival": call["scheduled_arrival"],
                "scheduled": call["scheduled"],
                "pickup_type": call["pickup_type"],
                "drop_off_type": call["drop_off_type"],
            },
        })
    return features


def _leg_runs(t, trips, trip_updates):
    """(key, run, trip update) of each run the feed reports for trip t: the
    trip itself for one, a copy keyed trip_id@start_time for each of
    several (a frequency-based trip)."""
    if len(trip_updates) == 1:
        return [(t, trips[t], trip_updates[0])]
    keyed = []
    for i, trip_update in enumerate(trip_updates):
        start = (trip_update.get("trip") or {}).get("start_time") or str(i)
        run = {"stops": {sid: dict(v) for sid, v in trips[t]["stops"].items()}}
        trips[f"{t}@{start}"] = run
        keyed.append((f"{t}@{start}", run, trip_update))
    return keyed


def _call_of(run, update, by_sequence, called_twice):
    """The call of the run a stop update times, None when it names none of
    them, or names the pass of a stop called twice that the ride skips."""
    stop_id = str(update.get("stop_id") or "") or by_sequence.get(update.get("stop_sequence"))
    stop = run["stops"].get(stop_id) if stop_id else None
    if stop is None:
        return None
    told = update.get("stop_sequence")
    if (stop_id in called_twice and told is not None
            and stop.get("sequence") not in (None, told)):
        # the trip calls there twice and the feed says which
        # call it times: this one is the pass the ride skips.
        # Only then, since a feed may number its calls its own
        # way (the SNCF does) and the id is enough elsewhere
        return None
    return stop


def _time_call(run, update, by_sequence, called_twice):
    """Lay one stop update on its call of the run; True when the call now
    carries realtime (a time, a delay, a skip)."""
    stop = _call_of(run, update, by_sequence, called_twice)
    if stop is None:
        return False
    called = stop_relationship(update)
    if called == SKIPPED_STOP:
        stop["skipped"] = True
        return True
    if called == NO_DATA_STOP:
        stop["no_data"] = True
        return False
    when, delay = stop_update_clock(update)
    if when:
        stop["expected"] = datetime.datetime.fromtimestamp(int(when), datetime.timezone.utc).isoformat()
    if when and not delay and stop.get("scheduled"):
        # a feed that gives times without delays (TAO, Palm Bus):
        # the delay is the gap to the schedule
        delay = int((datetime.datetime.fromisoformat(stop["expected"])
                     - datetime.datetime.fromisoformat(stop["scheduled"])).total_seconds())
    if delay or when:
        stop["delay"] = int(delay or 0)
        return True
    return False


def _time_leg_trips(trips, called_twice, feed_entities):
    """The realtime of every listed trip, at every stop the feed covers;
    True when the feed said anything of them."""
    updates = {}
    for entity in feed_entities or []:
        trip_update = entity.get("trip_update") if isinstance(entity, dict) else None
        if not trip_update:
            continue
        t = str((trip_update.get("trip") or {}).get("trip_id") or "")
        if t in trips:
            updates.setdefault(t, []).append(trip_update)
    realtime = False
    for t, trip_updates in updates.items():
        by_sequence = {v["sequence"]: sid for sid, v in trips[t]["stops"].items()}
        for _key, run, trip_update in _leg_runs(t, trips, trip_updates):
            start = (trip_update.get("trip") or {}).get("start_time")
            if start:
                run["start_time"] = start
            # what the feed struck out: the whole run, or single calls. The
            # keys are only written when set, so a run the feed leaves
            # alone reads as before.
            if trip_relationship({"trip_update": trip_update}) in CANCELLED_TRIP:
                run["cancelled"] = True
                realtime = True
                continue
            for update in trip_update.get("stop_time_update") or []:
                if _time_call(run, update, by_sequence, called_twice.get(t, ())):
                    realtime = True
    return realtime


def write_leg_file(hass, data, feed_entities=None):
    """Write www/gtfs2/<entry>_leg.json: the trip the next departure rides,
    stop by stop, and for every listed departure when its trip calls at
    every stop, scheduled and, where the feed says, expected.

    The route file draws the line; this one times a ride. A card chaining
    legs into a journey needs, for a stop in the middle of the ride, when
    THIS run gets there: the scheduled time of each listed trip answers it
    without guessing from the origin, and the trip updates the coordinator
    already fetched give the realtime at every stop the feed covers, where
    the sensor itself only reads the origin's.

    Every time is a datetime, the way the sensor's own departures are: the
    stored clock is laid on the service day of the departure the sensor
    lists for that trip, in the agency's zone, so a stop past midnight
    lands on the next calendar day rather than on a clock past 24:00.

    Keyed by trip_id. A frequency-based trip the feed reports several times
    keeps its scheduled entry under the bare id and gets one realtime entry
    per run, keyed trip_id@start_time. A stop update carrying no time and a
    zero delay is left out: protobuf reads an absent field as zero, so that
    one cannot be told from "on time".
    """
    schedule = data["schedule"]
    name = data.get("name") or ""
    departure = data.get("next_departure") or {}
    trip_id, trip_ids, leaves = _listed_trips(departure)
    route_id, direction, origin_id, _destination = shown_ends(data, departure)
    stops_by_trip, origin_parent = _read_trip_calls(schedule, trip_ids, origin_id)
    zone = _leg_timezone(schedule, route_id, departure, hass)

    trips = {}
    features = []
    # the stops a trip calls at twice, the only ones where the feed's own
    # stop_sequence has to be believed over the stop id
    called_twice = {}
    for t in trip_ids:
        rows = stops_by_trip.get(t)
        if not rows:
            continue
        origin_row = _origin_call(rows, origin_id, origin_parent)
        midnight = _service_midnight(leaves.get(t), rows, origin_row, zone)
        stops = {}
        seen = set()
        for r in _boarding_first(rows, origin_row):
            stop_id = str(r[1])
            if stop_id in seen:
                called_twice.setdefault(t, set()).add(stop_id)
            seen.add(stop_id)
            stops.setdefault(stop_id, _leg_call(midnight, r))
        trips[t] = {"stops": stops}
        if t == trip_id:
            features = _leg_features(rows, midnight, trip_id, route_id, direction)
    realtime = _time_leg_trips(trips, called_twice, feed_entities)
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    file = os.path.join(geojson_dir, leg_geojson_name(route_id, direction, name))
    _LOGGER.debug("Creating leg geojson file: %s", file)
    properties = {
        "name": name,
        "route_id": route_id,
        "direction_id": direction,
        "trip_id": trip_id,
        "origin_stop_id": departure.get("origin_stop_id"),
        "destination_stop_id": departure.get("destination_stop_id"),
        "timezone": str(zone),
        "realtime": realtime,
    }
    body = {"type": "FeatureCollection", "properties": properties,
            "features": features, "trips": trips}
    write_json_if_changed(
        file,
        {**body, "properties": {**properties,
                                "updated_at": dt_util.utcnow().isoformat()}},
        body)
    if _LEG_FILES.get(name) != file:
        _drop_other_legs(geojson_dir, name, file)
        _LEG_FILES[name] = file


# the leg file each entry last wrote, by entry name
_LEG_FILES: dict[str, str] = {}


def _drop_other_legs(geojson_dir, name, kept):
    """Remove the entry's leg files other than the one just written.

    The leg file is named after the line and the direction of the departure
    as well as the entry: when those change, the file of the old line stayed
    until the entry was removed, and a card still found it. Looked for once
    per new name, with the globs the entry's removal uses.
    """
    for pattern in leg_geojson_pattern(name):
        for path in glob.glob(os.path.join(geojson_dir, pattern)):
            if os.path.abspath(path) == os.path.abspath(kept) or not owns_leg_file(path, name):
                continue
            try:
                os.remove(path)
                _LOGGER.debug("Removed the leg file of an earlier line: %s", path)
            except OSError as ex:
                _LOGGER.warning("Could not remove %s: %s", path, ex)
