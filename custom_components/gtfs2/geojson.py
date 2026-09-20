"""The files this integration writes under www/gtfs2 for a map card.

Their names, so that the writer, the sensor attribute and the removal on
entry deletion agree (route_geojson_name, vehicle_positions_name,
leg_geojson_name); the route file, the line drawn from its fullest trip
with the shape read out of the zip and the boarding rules per stop
(write_route_file); the leg file, the ride of the next departure timed
stop by stop, realtime included (write_leg_file); and the timetable, every
departure of the entry over the next service days (write_timetable_file).
The coordinator calls the writers from the executor; the positions file
itself is written by gtfs_rt_helper.get_rt_vehicle_positions.
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import logging
import os
import re
import unicodedata
from collections import Counter

from sqlalchemy.sql import text
import homeassistant.util.dt as dt_util

from .const import DEFAULT_PATH_GEOJSON
from .feed_window import read_feed_window
from .gtfs_helper import (
    _call_type, _fetch_departure_rows, _line_ways, departure_query_args, get_next_service_date,
)
from .gtfs_rt_helper import (
    CANCELLED_TRIP, NO_DATA_STOP, SKIPPED_STOP, safe_file_part, stop_relationship, trip_relationship,
)
from .gtfs_shape import read_shape

_LOGGER = logging.getLogger(__name__)


def _gtfs_seconds(value):
    """Seconds since the service day's midnight of a stored stop time, or None.

    pygtfs stores stop_times through SQLAlchemy's Interval, which SQLite
    keeps as a datetime counted from 1970-01-01: a 01:15 departure after
    midnight reads '1970-01-02 01:15:00', and a raw query hands that string
    back as is. Seconds and timedeltas pass through.
    """
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return int(value.total_seconds())
    try:
        return int(value)
    except (TypeError, ValueError):
        stored = re.match(r"^1970-01-(\d{2}) (\d{2}):(\d{2}):(\d{2})", str(value))
        if not stored:
            return None
        day, hours, minutes, secs = (int(g) for g in stored.groups())
        return ((day - 1) * 24 + hours) * 3600 + minutes * 60 + secs


def _fmt_gtfs_time(value):
    """Render a stored stop time as the clock the feed wrote in stop_times.txt:
    HH:MM:SS, past 24:00 after midnight (SNCF writes 24:36:00 there). The
    line file has no service day to pin a date on, so it keeps the feed's
    own convention; the leg file, which has one, carries datetimes instead.
    """
    seconds = _gtfs_seconds(value)
    if seconds is None:
        return str(value) if value is not None else None
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"




def route_geojson_name(route_id, direction):
    """File name of the route export, in one place because three callers need
    the same answer: the writer, the sensor attribute and the removal on entry
    deletion. A file nobody can name again is a file nobody can delete."""
    return f"{safe_file_part(route_id)}_{safe_file_part(direction)}_route.json"


def vehicle_positions_name(route_id, direction):
    """Same, for the realtime positions file written by get_rt_vehicle_positions."""
    return f"{safe_file_part(route_id)}_{safe_file_part(direction)}.json"


def _calls_in_order(stops, origin_id, destination_id):
    """Whether a trip's ordered stop_ids call at origin_id, then at
    destination_id further on; either one may be None. The first call at the
    origin is the earliest boarding, so a line that loops back through it is
    still read right."""
    start = 0
    if origin_id:
        if origin_id not in stops:
            return False
        start = stops.index(origin_id) + 1
    return not destination_id or destination_id in stops[start:]


def get_representative_trip(schedule, route_id, direction, origin_id=None, destination_id=None):
    """The trip that stands for a route and direction on the map.

    The route file draws its stops, and a card places the sensor's boarding
    and alighting stops on them, so it has to be a trip the sensor rides.
    Ranked by:

    1. calling at origin_id, then at destination_id further on. The ids are
       compared whole, never by name, station or part of the id, because a
       change of mode is always another stop: the SNCF files its substitution
       coaches under the train line, on stops of their own under the same
       station, and at Orleans the two share the name and the UIC code, only
       the prefix differs (StopPoint:OCETrain TER-87543009 and
       StopPoint:OCECar TER-87543009). When no trip matches (a station
       configured, ids the provider renamed), every trip stays in;
    2. having a shape, as before;
    3. the most stops, so not a short turn;
    4. the stop sequence most trips follow: coaches and trains of the K8+
       both call at 3 stops, and the SNCF files both ways of a line under
       one direction_id with the same stop count (K5+ direction 1: 17 trips
       Nevers -> Paris, 22 Paris -> Nevers);
    5. the smallest trip_id, so a restart does not swap the drawn path. On
       its own it drew the K8+ from a coach both ways: the coach trip_ids
       sort before the train ones.

    Without stop ids, 1 is skipped.
    """
    if not route_id:
        return None
    origin_id = str(origin_id) if origin_id else None
    destination_id = str(destination_id) if destination_id else None
    where = "t.route_id = :route_id"
    params = {"route_id": str(route_id)}
    # direction_id is optional in GTFS and gtfs2 stringifies a missing one
    if direction not in (None, "", "None"):
        where += " AND CAST(t.direction_id AS TEXT) = :direction"
        params["direction"] = str(direction)
    # ONE pass over stop_times, filtered by a subquery on trips, and never a
    # join. pygtfs creates no index on stop_times at all, so joining it to a
    # filtered trips set makes SQLite scan the whole table once per candidate
    # trip: measured on a mid-sized city feed (680k stop_times, 1818 trips on
    # the line) that was 17.3 SECONDS against 45 ms this way, for the same
    # answer. The ranking needs every trip's stops in order, so the rows come
    # back as they are and are ranked here. Which trips have a shape is a
    # question for trips alone, indexed and small.
    sql_shaped = f"SELECT t.trip_id FROM trips t WHERE {where} AND t.shape_id IS NOT NULL"
    sql_calls = f"""
    SELECT st.trip_id, st.stop_id, st.stop_sequence
    FROM stop_times st
    WHERE st.trip_id IN (SELECT t.trip_id FROM trips t WHERE {where})
    """
    calls = {}
    try:
        with schedule.engine.connect() as conn:
            shaped = {row[0] for row in conn.execute(text(sql_shaped), params)}
            for trip_id, stop_id, sequence in conn.execute(text(sql_calls), params):
                calls.setdefault(trip_id, []).append((sequence, stop_id))
    except Exception as ex:  # pylint: disable=broad-except
        _LOGGER.warning("Could not find a trip to draw route %s direction %s: %s", route_id, direction, ex)
        return None
    if not calls:
        _LOGGER.debug("No trip at all for route %s direction %s", route_id, direction)
        return None
    stops = {trip_id: tuple(stop_id for _, stop_id in sorted(rows)) for trip_id, rows in calls.items()}
    trips = list(stops)
    if origin_id or destination_id:
        ridden = [trip_id for trip_id in trips if _calls_in_order(stops[trip_id], origin_id, destination_id)]
        if ridden:
            trips = ridden
        else:
            _LOGGER.debug("No trip of route %s direction %s calls at %s then %s, drawing from all of them",
                          route_id, direction, origin_id, destination_id)
    trips = [trip_id for trip_id in trips if trip_id in shaped] or trips
    most = max(len(stops[trip_id]) for trip_id in trips)
    trips = [trip_id for trip_id in trips if len(stops[trip_id]) == most]
    followed = Counter(stops[trip_id] for trip_id in trips)
    trip_id = min(trips, key=lambda trip_id: (-followed[stops[trip_id]], trip_id))
    _LOGGER.debug("Drawing route %s direction %s from trip: %s", route_id, direction, trip_id)
    return trip_id


def write_route_file(hass, data, route_id, direction, trip_id=None):
    """Write the line's ordered stops to www/gtfs2/<route>_<direction>_route.json.

    Companion file to the vehicle-positions geojson. The stops as Points,
    each with an id and a title the way the geojson integration expects,
    plus the trip_id; what describes the whole line sits on the
    FeatureCollection. When the zip beside the database still holds
    shapes.txt and the trip names a shape, its polyline comes first as a
    LineString, in the trip's travel direction, so a map card draws the
    street or the track rather than a straight line between stops: on the
    tram A of Orleans the stops sit within 26 m of it. The polyline is read
    from the zip, never from the database (see gtfs_shape). Without it, a
    card joins the points in stop_sequence order, as before.

    The line drawn is the whole line: its fullest trip in this direction, not
    the trip of the next departure. That one is a short turn often enough
    (TAO tram B runs 110 of them a day) to leave a map showing half a line
    at the wrong hour, and it moves at every departure while the line does
    not. What the next departure rides, and when, is the leg file's business
    (write_leg_file). Rewritten only when the fullest trip or the zip
    changes, that is when the feed does (see coordinator).
    """
    schedule = data["schedule"]
    if not trip_id:
        # a trip the sensor rides: its stops come from the next departure,
        # from the entry once the last one of the day is gone
        departure = data.get("next_departure") or {}
        trip_id = get_representative_trip(
            schedule, route_id, direction,
            departure.get("origin_stop_id") or (data.get("origin") or "").split(": ")[0],
            departure.get("destination_stop_id") or (data.get("destination") or "").split(": ")[0])
    if not trip_id:
        return
    sql_stops = """
    SELECT st.stop_id, s.stop_name, s.stop_lat, s.stop_lon, st.stop_sequence, st.departure_time,
           st.pickup_type, st.drop_off_type
    FROM stop_times st
    JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.trip_id = :trip_id
    ORDER BY st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        stop_rows = conn.execute(text(sql_stops), {"trip_id": trip_id}).fetchall()
        # the shape is the trip's, and trips keep their shape_id even though
        # the shapes themselves are never imported
        shape_row = conn.execute(text("SELECT shape_id FROM trips WHERE trip_id = :trip_id"),
                                 {"trip_id": trip_id}).fetchone()
        boards, alights = _line_ways(conn, route_id, direction)
    if not stop_rows:
        _LOGGER.debug("No stops found for trip: %s", trip_id)
        return
    shape_id = shape_row[0] if shape_row and shape_row[0] else None
    zip_path = os.path.join(hass.config.path(data["gtfs_dir"]), str(data["file"]) + ".zip")
    shape = read_shape(zip_path, shape_id) if shape_id else None
    features = []
    if shape and len(shape) >= 2:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": shape},
            "properties": {
                "id": str(route_id) + "_" + str(direction) + "_shape",
                "title": str(route_id) + "_shape",
                "trip_id": trip_id,
                "shape_id": str(shape_id),
            },
        })
    else:
        shape_id = None
    for row in stop_rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [row[3], row[2]]},
            "properties": {
                "id": str(route_id) + "_" + str(direction) + "_" + str(row[4]),
                # the _stop suffix is what a customize_glob rule matches on to
                # give the stop entity a picture, see upstream c666cb7
                "title": row[1] + "_stop",
                "trip_id": trip_id,
                "stop_id": row[0],
                "stop_name": row[1],
                "stop_sequence": row[4],
                "departure_time": _fmt_gtfs_time(row[5]),
                # how this trip calls there: 0 regular, 1 no way on / off,
                # 2 phone ahead, 3 tell the driver (see _boards)
                "pickup_type": _call_type(row[6]),
                "drop_off_type": _call_type(row[7]),
                # and whether any trip of the line does, this way: false
                # only when no trip ever takes riders on, or sets them
                # down, at this place, the one word that may shut it out
                # of a card's lists (see _line_ways)
                "boards": boards(row[0]),
                "alights": alights(row[0]),
            },
        })
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    # the ids come out of the datasource, so they are not file names until
    # they are made ones: see safe_file_part
    file = os.path.join(geojson_dir, route_geojson_name(route_id, direction))
    _LOGGER.debug("Creating route geojson file: %s", file)
    with open(file, "w") as outfile:
        json.dump({
            "type": "FeatureCollection",
            "properties": {
                "trip_id": trip_id,
                "route_id": str(route_id),
                "direction_id": str(direction),
                # the trip stands for the line, it is not the one about to leave
                "representative": True,
                # the shape drawn, None when the stops alone draw the line
                "shape_id": shape_id,
            },
            "features": features,
        }, outfile)


# how many of the listed departures the leg file times stop by stop: a board
# shows a handful, and a day's worth of runs on a busy line is a file
# rewritten every minute for nothing
LEG_TRIPS_MAX = 20


def entry_file_part(name) -> str:
    """An entry's name, made a readable file name part: accents dropped to
    their base letter (Orléans reads orleans, not orl_ans), then the same
    rule as the ids, then the stray dashes an arrow or a long dash leaves
    behind ("_-_") folded away.

    A name written in an alphabet that leaves nothing behind, Greek,
    Russian or Japanese, used to fold to the empty string: every such
    entry then wrote the same file and the last one won. It keeps a short
    print of the name instead, unreadable but its own.
    """
    plain = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    part = re.sub(r"_+", "_", safe_file_part(plain).replace("-", "_")).strip("_")
    if not part:
        part = hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:8]
    return part


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


def _leg_timezone(schedule, route_id, departure, hass):
    """The zone the line's clocks are written in: the agency's, as the
    departure query reads it, else the origin stop's, else Home Assistant's."""
    name = None
    try:
        with schedule.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT agency.agency_timezone FROM routes "
                "JOIN agency ON agency.agency_id = routes.agency_id "
                "WHERE routes.route_id = :route"), {"route": route_id}).fetchone()
            if not row or not row[0]:
                row = conn.execute(text("SELECT agency_timezone FROM agency LIMIT 1")).fetchone()
            name = row[0] if row else None
    except Exception:  # pylint: disable=broad-except
        name = None
    name = name or departure.get("origin_stop_timezone") or hass.config.time_zone
    return dt_util.get_time_zone(name) or datetime.timezone.utc


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
    trip_id = str(departure.get("trip_id") or "") or None
    trip_ids = []
    for t in [trip_id] + list(departure.get("next_departures_trip_id") or []):
        if t and str(t) not in trip_ids:
            trip_ids.append(str(t))
    trip_ids = trip_ids[:LEG_TRIPS_MAX]
    # when each listed trip leaves the origin, as the sensor says it
    leaves = {}
    for t, when in zip(departure.get("next_departures_trip_id") or [], departure.get("next_departures") or []):
        leaves.setdefault(str(t), when)
    if trip_id and departure.get("departure_time"):
        first = departure["departure_time"]
        leaves.setdefault(trip_id, first.isoformat() if hasattr(first, "isoformat") else str(first))
    route_id = str(departure.get("route_id") or (data.get("route") or "").split(": ")[0])
    direction = str(departure.get("trip_direction_id", data.get("direction")))
    origin_id = str(departure.get("origin_stop_id") or (data.get("origin") or "").split(": ")[0])
    stops_by_trip = {}
    origin_parent = None
    if trip_ids:
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
            origin_parent = parent[0] if parent and parent[0] else None
    zone = _leg_timezone(schedule, route_id, departure, hass)

    def midnight_of(t, rows):
        """The service day's midnight of that trip, in the line's zone: the
        origin's departure, as listed, minus the origin's stored clock. The
        origin is the entry's record, else a platform of the same station
        (the trip may serve a sibling record), else the trip's first stop."""
        when = leaves.get(t)
        if not when:
            return None
        origin_row = next((r for r in rows if str(r[1]) == origin_id), None)
        if origin_row is None and origin_parent:
            origin_row = next((r for r in rows if r[8] == origin_parent), None)
        if origin_row is None:
            origin_row = rows[0]
        seconds = _gtfs_seconds(origin_row[7])
        if seconds is None:
            return None
        try:
            local = datetime.datetime.fromisoformat(str(when)).astimezone(zone)
        except ValueError:
            return None
        return (local - datetime.timedelta(seconds=seconds)).replace(hour=0, minute=0, second=0, microsecond=0)

    def at(midnight, stored):
        seconds = _gtfs_seconds(stored)
        if midnight is None or seconds is None:
            return None
        return (midnight + datetime.timedelta(seconds=seconds)).astimezone(datetime.timezone.utc).isoformat()

    def boarding_first(rows):
        """The trip's calls, the ride's own first.

        A stop is a key here, so a trip calling twice at one stop can only
        keep one of its two calls, and a loop line calls at its terminus
        twice. The one that counts is the one the rider makes, so the
        calls from the origin onwards come first and the ones before it
        follow: reading them in order then keeps the right one, where the
        plain feed order kept whichever came last, the return pass.
        """
        origin_row = next((r for r in rows if str(r[1]) == origin_id), None)
        if origin_row is None and origin_parent:
            origin_row = next((r for r in rows if r[8] == origin_parent), None)
        if origin_row is None:
            return rows
        return sorted(rows, key=lambda r: (r[5] < origin_row[5], r[5]))

    trips = {}
    features = []
    # the stops a trip calls at twice, the only ones where the feed's own
    # stop_sequence has to be believed over the stop id
    called_twice = {}
    for t in trip_ids:
        rows = stops_by_trip.get(t)
        if not rows:
            continue
        midnight = midnight_of(t, rows)

        def call_at(r):
            return {
                "sequence": r[5],
                "scheduled_arrival": at(midnight, r[6]),
                "scheduled": at(midnight, r[7]),
                # as the feed flags the call: 0 regular, 1 none, 2 phone
                # ahead, 3 tell the driver; a card chaining legs picks its
                # ends among the calls the rider can make (see _boards)
                "pickup_type": _call_type(r[9]),
                "drop_off_type": _call_type(r[10]),
            }

        stops = {}
        seen = set()
        for r in boarding_first(rows):
            stop_id = str(r[1])
            if stop_id in seen:
                called_twice.setdefault(t, set()).add(stop_id)
            seen.add(stop_id)
            stops.setdefault(stop_id, call_at(r))
        trips[t] = {"stops": stops}
        if t == trip_id:
            for r in rows:
                # each point carries its own call, not the one the stop
                # keeps: on a loop the two differ
                call = call_at(r)
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r[4], r[3]]},
                    "properties": {
                        "id": f"{route_id}_{direction}_{r[5]}",
                        "title": str(r[2]) + "_stop",
                        "trip_id": trip_id,
                        "stop_id": r[1],
                        "stop_name": r[2],
                        "stop_sequence": r[5],
                        "scheduled_arrival": call["scheduled_arrival"],
                        "scheduled": call["scheduled"],
                        "pickup_type": call["pickup_type"],
                        "drop_off_type": call["drop_off_type"],
                    },
                })
    # the realtime of every listed trip, at every stop the feed covers
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
        if len(trip_updates) == 1:
            keyed = [(t, trips[t], trip_updates[0])]
        else:
            keyed = []
            for i, trip_update in enumerate(trip_updates):
                start = (trip_update.get("trip") or {}).get("start_time") or str(i)
                run = {"stops": {sid: dict(v) for sid, v in trips[t]["stops"].items()}}
                trips[f"{t}@{start}"] = run
                keyed.append((f"{t}@{start}", run, trip_update))
        for key, run, trip_update in keyed:
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
                stop_id = str(update.get("stop_id") or "") or by_sequence.get(update.get("stop_sequence"))
                stop = run["stops"].get(stop_id) if stop_id else None
                if stop is None:
                    continue
                told = update.get("stop_sequence")
                if (stop_id in called_twice.get(t, ()) and told is not None
                        and stop.get("sequence") not in (None, told)):
                    # the trip calls there twice and the feed says which
                    # call it times: this one is the pass the ride skips.
                    # Only then, since a feed may number its calls its own
                    # way (the SNCF does) and the id is enough elsewhere
                    continue
                called = stop_relationship(update)
                if called == SKIPPED_STOP:
                    stop["skipped"] = True
                    realtime = True
                    continue
                if called == NO_DATA_STOP:
                    stop["no_data"] = True
                    continue
                arrival = update.get("arrival") or {}
                departure_update = update.get("departure") or {}
                when = departure_update.get("time") or arrival.get("time") or 0
                delay = departure_update.get("delay") if (departure_update.get("time") or departure_update.get("delay")) else arrival.get("delay")
                if when:
                    stop["expected"] = datetime.datetime.fromtimestamp(int(when), datetime.timezone.utc).isoformat()
                if when and not delay and stop.get("scheduled"):
                    # a feed that gives times without delays (TAO, Palm Bus):
                    # the delay is the gap to the schedule
                    delay = int((datetime.datetime.fromisoformat(stop["expected"])
                                 - datetime.datetime.fromisoformat(stop["scheduled"])).total_seconds())
                if delay or when:
                    stop["delay"] = int(delay or 0)
                    realtime = True
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    file = os.path.join(geojson_dir, leg_geojson_name(route_id, direction, name))
    _LOGGER.debug("Creating leg geojson file: %s", file)
    with open(file, "w") as outfile:
        json.dump({
            "type": "FeatureCollection",
            "properties": {
                "name": name,
                "route_id": route_id,
                "direction_id": direction,
                "trip_id": trip_id,
                "origin_stop_id": departure.get("origin_stop_id"),
                "destination_stop_id": departure.get("destination_stop_id"),
                "timezone": str(zone),
                "realtime": realtime,
                "updated_at": dt_util.utcnow().isoformat(),
            },
            "features": features,
            "trips": trips,
        }, outfile)


# The service days the timetable holds: the one under way and the two after
# it, so that it always reaches at least 48 hours ahead - at 23:00 the rest
# of the evening and two whole days, just past a day change nearly three.
TIMETABLE_DAYS = 3
# a safeguard on the rows of one read, not a length: a metro over three
# days is some 900 departures, a train a few dozen
TIMETABLE_ROWS_MAX = 5000


def timetable_name(name):
    """File name of an entry's timetable. The entry's name alone: unlike the
    route and positions files it is this sensor's, and a train entry's
    departures may ride several routes. Kept in one place, like the others,
    so the writer, the attribute and the removal agree."""
    return f"timetable_{entry_file_part(name)}.json"


def _local(stamp, zone):
    """A naive 'YYYY-MM-DD HH:MM:SS' of the query, in the line's zone, as an
    ISO datetime with its offset; None when unreadable."""
    try:
        moment = datetime.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone).isoformat()


def timetable_doc(name, rows, service_dates, zone, next_departure=None, until=None, generated=None):
    """The timetable file's content, from the departure rows of a window.

    service_dates are the days the file stands for, each listed even when
    no departure runs on it: an empty day says the timetable is known and
    has nothing, which a missing file cannot say. A row of the day before
    the first (a run of last night's service leaving after midnight) gets
    a day of its own ahead of them. Each departure is its trip, when it
    leaves the entry's origin and when it reaches its destination, both
    real datetimes: a run past midnight keeps its service day and carries
    the next calendar date.

    next_departure is the first departure after the window, found in the
    calendar, and until the last day the feed has any service on: past
    it nothing can be known, so an empty window with no next departure
    says "nothing published until then" rather than "never".
    """
    days = {d: [] for d in service_dates}
    for row in rows:
        day = str(row.get("origin_depart_date") or "")[:10]
        dep = _local(row.get("origin_depart_dt"), zone)
        if not day or not dep:
            continue
        days.setdefault(day, []).append({
            "trip_id": str(row.get("trip_id")),
            "dep": dep,
            "arr": _local(row.get("dest_arrival_dt"), zone),
        })
    return {
        "entry": name,
        "timezone": str(zone),
        "generated": (generated or dt_util.now()).isoformat(),
        "days": [{"service_date": d, "departures": sorted(days[d], key=lambda x: x["dep"])}
                 for d in sorted(days)],
        "next": next_departure,
        "until": until,
    }


# the last service day of each zip, read once per edition: the file is
# small but every entry of a source asks, every day
_UNTIL = {}


def _feed_until(zip_path):
    try:
        stat = os.stat(zip_path)
    except OSError:
        return None
    key = (zip_path, stat.st_size, stat.st_mtime_ns)
    if key not in _UNTIL:
        _UNTIL.clear()
        _UNTIL[key] = (read_feed_window(zip_path) or {}).get("last_service_day")
    return _UNTIL[key]


def write_timetable_file(hass, data, today, zip_path):
    """Write www/gtfs2/timetable_<entry>.json: every departure of the entry
    from now to the end of the third service day, today's included.

    The sensor lists ten departures, enough for a board and too few for a
    journey: a card chaining a bus, a train and a metro needs the metro an
    hour and a half ahead, where a line every four minutes has long run
    out of listed runs. The card reads the sensor first, realtime and all,
    and this file past it. Written from the same query as the sensor, so
    both agree on the calendar, the places and the runs after midnight;
    rewritten when the service day or the zip changes (see the
    coordinator), not on every refresh.

    today is the local service date as YYYY-MM-DD. Returns the file name.
    """
    schedule = data["schedule"]
    name = data.get("name") or ""
    first = datetime.date.fromisoformat(today)
    service_dates = [(first + datetime.timedelta(days=i)).isoformat() for i in range(TIMETABLE_DAYS)]
    yesterday = (first - datetime.timedelta(days=1)).isoformat()
    args = departure_query_args(data)
    rows, _origin = _fetch_departure_rows(
        data["route_type"], data["origin"], data["destination"], schedule,
        window=(yesterday, service_dates[-1]), limit=TIMETABLE_ROWS_MAX, **args)
    departure = data.get("next_departure") or {}
    zone = _leg_timezone(schedule, str(departure.get("route_id") or args["route"] or ""), departure, hass)
    # the first run past the window: the next day the entry runs at all,
    # then its first departure that day
    next_departure = None
    after = (first + datetime.timedelta(days=TIMETABLE_DAYS)).isoformat()
    day = get_next_service_date(
        schedule, data["origin"].split(": ")[0], data["destination"].split(": ")[0], after,
        data["route_type"], line=args["line"],
        origin_names=data.get("origin_stations"), dest_names=data.get("destination_stations"))
    if day:
        later, _origin = _fetch_departure_rows(
            data["route_type"], data["origin"], data["destination"], schedule,
            window=(day, day), limit=1, **args)
        if later:
            next_departure = _local(later[0].get("origin_depart_dt"), zone)
    doc = timetable_doc(name, rows, service_dates, zone, next_departure, _feed_until(zip_path))
    geojson_dir = hass.config.path(DEFAULT_PATH_GEOJSON)
    os.makedirs(geojson_dir, exist_ok=True)
    file = timetable_name(name)
    _LOGGER.debug("Creating timetable file: %s, %s departures", file, sum(len(d["departures"]) for d in doc["days"]))
    with open(os.path.join(geojson_dir, file), "w") as outfile:
        json.dump(doc, outfile)
    return file
