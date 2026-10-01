"""The files this integration writes under www/gtfs2 for a map card.

Their names, so that the writer, the sensor attribute and the removal on
entry deletion agree (route_geojson_name, vehicle_positions_name); the
route file, the line drawn from its fullest trip with the shape read out
of the zip and the boarding rules per stop (write_route_file); and what
every file here shares: an id or an entry's name as a file name part
(safe_file_part, entry_file_part), a write no reader catches half done
(write_json_file) and a write skipped when nothing changed
(write_json_if_changed). The leg file is in leg.py, the timetable in
timetable.py. The coordinator calls the writers from the executor; the
positions file itself is written by gtfs_rt_helper.get_rt_vehicle_positions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import Counter

from sqlalchemy.sql import text

from .const import DEFAULT_PATH_GEOJSON
from .gtfs_db import feed_zip
from .gtfs_helper import gtfs_seconds, shown_ends
from .places import _call_type, _line_ways
from .gtfs_shape import read_shape, trip_shape_id

_LOGGER = logging.getLogger(__name__)


def _fmt_gtfs_time(value):
    """Render a stored stop time as the clock the feed wrote in stop_times.txt:
    HH:MM:SS, past 24:00 after midnight (SNCF writes 24:36:00 there). The
    line file has no service day to pin a date on, so it keeps the feed's
    own convention; the leg file, which has one, carries datetimes instead.
    """
    seconds = gtfs_seconds(value)
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


def clear_vehicle_file(hass, route_id, direction) -> bool:
    """Take the vehicles off the map, the feeds not being read any more.

    The positions file is the last thing the map was told, and nothing
    says how old it is: left as it was when the service window closed,
    the evening's last buses sat on the map all night. Written empty it
    says what is true, that nothing is running.

    Returns whether it wrote. A file already empty, or one that was never
    written, is left alone, so a paused source costs no write per minute.
    """
    file = os.path.join(hass.config.path(DEFAULT_PATH_GEOJSON),
                        vehicle_positions_name(route_id, direction))
    if not os.path.exists(file):
        return False
    try:
        with open(file, encoding="utf-8") as handle:
            if not (json.load(handle).get("features") or []):
                return False
    except (OSError, ValueError):
        # unreadable: write it afresh rather than leave whatever it holds
        pass
    write_json_file(file, {"features": [], "type": "FeatureCollection"})
    _LOGGER.debug("Vehicles taken off the map: %s", file)
    return True


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


def _route_trip_calls(schedule, route_id, direction):
    """(shaped, stops) for a route and direction: the trips that have a
    shape, and {trip_id: its stop ids in riding order}. None when the
    database cannot be read; an empty dict when the line has no trip."""
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
    return shaped, {trip_id: tuple(stop_id for _, stop_id in sorted(rows))
                    for trip_id, rows in calls.items()}


def _rank_representative(stops, shaped, origin_id, destination_id):
    """The trip get_representative_trip draws, out of {trip_id: its stops}
    and the trips with a shape, by the ranking its docstring gives."""
    trips = list(stops)
    if origin_id or destination_id:
        ridden = [trip_id for trip_id in trips if _calls_in_order(stops[trip_id], origin_id, destination_id)]
        if ridden:
            trips = ridden
        else:
            _LOGGER.debug("No trip calls at %s then %s, drawing from all of them",
                          origin_id, destination_id)
    trips = [trip_id for trip_id in trips if trip_id in shaped] or trips
    most = max(len(stops[trip_id]) for trip_id in trips)
    trips = [trip_id for trip_id in trips if len(stops[trip_id]) == most]
    followed = Counter(stops[trip_id] for trip_id in trips)
    return min(trips, key=lambda trip_id: (-followed[stops[trip_id]], trip_id))


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
    # a sentinel of get_gtfs ("not_built", "no_zip_file") holds no database
    # to read: matched by shape, as the helpers of gtfs_helper do
    if schedule is None or isinstance(schedule, str):
        _LOGGER.debug("No usable schedule to draw route %s (%s)", route_id, schedule or "empty")
        return None
    read = _route_trip_calls(schedule, route_id, direction)
    if read is None:
        return None
    shaped, stops = read
    if not stops:
        _LOGGER.debug("No trip at all for route %s direction %s", route_id, direction)
        return None
    trip_id = _rank_representative(stops, shaped, str(origin_id) if origin_id else None,
                                   str(destination_id) if destination_id else None)
    _LOGGER.debug("Drawing route %s direction %s from trip: %s", route_id, direction, trip_id)
    return trip_id


def write_route_file(hass, data, route_id, direction, trip_id=None):
    """Write the line's ordered stops to www/gtfs2/<route>_<direction>_route.json.

    Companion file to the vehicle-positions geojson. The stops as Points,
    each with an id and a title the way the geojson integration expects,
    plus the trip_id; what describes the whole line sits on the
    FeatureCollection. When the zip beside the database still holds
    shapes.txt and its trips.txt names a shape for the trip, its polyline comes first as a
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
        _route, _direction, origin_id, destination_id = shown_ends(
            data, data.get("next_departure") or {})
        trip_id = get_representative_trip(schedule, route_id, direction, origin_id, destination_id)
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
        # only to tell the log when the database is another edition
        db_shape = conn.execute(text("SELECT shape_id FROM trips WHERE trip_id = :trip_id"),
                                {"trip_id": trip_id}).fetchone()
        boards, alights = _line_ways(conn, route_id, direction)
    if not stop_rows:
        _LOGGER.debug("No stops found for trip: %s", trip_id)
        return
    zip_path = feed_zip(hass.config.path(data["gtfs_dir"]), str(data["file"]))
    # the shape is the trip's, named by the zip the points come from and
    # not by the database, which may be another edition (see trip_shape_id)
    shape_id = trip_shape_id(zip_path, trip_id)
    db_shape = db_shape[0] if db_shape and db_shape[0] else None
    if db_shape and db_shape != shape_id:
        # the window between the zip's adoption and the database's build,
        # or a build that failed: the file is written again once the
        # database follows (see exports._drawn_trip)
        _LOGGER.info("Route %s direction %s, trip %s: the database names shape %s and the zip %s, "
                     "two editions of the feed; the zip's is drawn",
                     route_id, direction, trip_id, db_shape, shape_id or "none")
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
                # give the stop entity a picture, see upstream c666cb7.
                # pygtfs stores an empty stop_name as None, and a feed that
                # leaves one blank used to take the whole file down with it
                "title": f"{row[1] or row[0]}_stop",
                "trip_id": trip_id,
                "stop_id": row[0],
                "stop_name": row[1] or row[0],
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
    # the ids come out of the datasource, so they are not file names until
    # they are made ones: see safe_file_part
    file = os.path.join(geojson_dir, route_geojson_name(route_id, direction))
    _LOGGER.debug("Creating route geojson file: %s", file)
    write_json_file(file, {
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
    })


# what each written file last held, apart from the moment it was written:
# a refresh that changes nothing then costs no write. Keyed by path, and
# emptied by a restart, which writes once. Sensors refresh every minute and
# these files are the size of a timetable, so rewriting them for a new
# timestamp alone is a few thousand writes a day on a memory card.
_WRITTEN: dict[str, str] = {}


def write_json_if_changed(file, doc, stable) -> bool:
    """Write doc to file as json, unless it already says the same thing.

    stable is what the comparison reads: doc without the moment it was
    written, which is new every time and says nothing about the contents.
    A file that is not there is always written, so emptying www/gtfs2 by
    hand gets everything back at the next refresh.
    """
    digest = hashlib.sha1(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    if _WRITTEN.get(file) == digest and os.path.exists(file):
        _LOGGER.debug("Unchanged since the last write, left alone: %s", file)
        return False
    write_json_file(file, doc)
    _WRITTEN[file] = digest
    return True


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


def name_in_use(name, taken) -> bool:
    """Whether an entry name is taken, as a name or as the file part the
    timetable and leg files are named with: "Orléans" and "Orleans", or
    "Bus 1 Gare > Centre" and "bus-1 gare - centre", are two names and one
    file part, and the second entry wrote over the first one's files."""
    part = entry_file_part(name)
    return name in taken or any(entry_file_part(t) == part for t in taken if t)


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


def write_json_file(file, doc):
    """Write a json file the way a reader can never catch it half written.

    The map cards fetch these files while the sensors rewrite them, every
    minute for the vehicles: written in place, a fetch landing mid-write
    read a truncated document and dropped the layer. The file is written
    beside its target and renamed over it, which a reader sees whole.
    """
    # a name of its own per writer: two entries on one line write the same
    # file in the same second, and a shared staging name had each rename
    # the other's half-written file, or find it gone
    staged = f"{file}.{os.getpid()}.{threading.get_ident()}.tmp"
    # the folder too: www/gtfs2 emptied or never made by hand
    os.makedirs(os.path.dirname(file) or ".", exist_ok=True)
    try:
        with open(staged, "w") as outfile:
            json.dump(doc, outfile)
        for attempt in range(5):
            try:
                os.replace(staged, file)
                break
            except PermissionError:
                # Windows refuses a rename onto a file another writer is
                # renaming onto at that instant; it is free a moment later
                if attempt == 4:
                    raise
                time.sleep(0.02)
    finally:
        if os.path.exists(staged):
            os.remove(staged)
