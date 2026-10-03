"""Build the database of a feed a test writes itself, the way a source is built.

A test that needs a timetable of its own gives its tables as text,
{"stops.txt": "stop_id,...\\n...", ...}; they are zipped and imported by
the real pygtfs, so the code under test reads what an install would read.
tests_provider/fixture_db does the same for the fixtures of that suite.

    schedule = feed_db.build(tmp_path, FEED)
    try:
        ...
    finally:
        schedule.engine.dispose()

STOP_TIMES and calls() write the stop_times.txt of trips calling a
minute apart. rows() reads a database file back, the way a test checks what a step
left on disk, and lets the file go after. marked_zip() is the bytes of the
smallest feed a download can bring, told apart by a marker.
"""
from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path


def build(folder, tables):
    """A pygtfs schedule of the tables, imported from folder/feed.zip into
    folder/feed.sqlite; the zip is left beside it, as a source keeps it."""
    import pygtfs

    archive = Path(folder) / "feed.zip"
    with zipfile.ZipFile(archive, "w") as zout:
        for name, body in tables.items():
            zout.writestr(name, body)
    schedule = pygtfs.Schedule(str(Path(folder) / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(archive))
    return schedule


def rows(path, sql):
    """The rows the query reads from the database at path, the file closed
    after."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def marked_zip(marker):
    """The bytes of a zip holding the smallest feed, the agency's name
    saying marker: what a host sends, one edition told from another."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        zout.writestr("agency.txt", "agency_id,agency_name\nX," + marker)
        # the three tables a staged download must carry to be a feed at
        # all: stage_zip refuses a zip without them
        zout.writestr("routes.txt", "route_id\nR\n")
        zout.writestr("trips.txt", "route_id,service_id,trip_id\nR,S,T\n")
        zout.writestr("stop_times.txt", "trip_id,stop_id,stop_sequence\nT,A,1\n")
    return buffer.getvalue()


# the header of the stop_times.txt calls() writes the rows of; a test whose
# calls carry boarding rules writes its own, with pickup_type and drop_off_type
STOP_TIMES = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"


def calls(trip, stops):
    """The stop_times.txt rows of a trip calling at stops in this order, a
    minute apart from 08:01."""
    return "".join(f"{trip},08:{n:02d}:00,08:{n:02d}:00,{s},{n}\n" for n, s in enumerate(stops, 1))


def line_feed(stops, trips):
    """The tables of a feed whose trips ride lines of one agency, every day:
    stops is {stop_id: name}, trips [(trip_id, route_id, direction_id or
    None, the stop ids it calls at in order)]."""
    routes = sorted({route for _trip, route, _direction, _calls in trips})
    return {
        "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(
            f"{stop},{name},45.{n},1.{n}\n" for n, (stop, name) in enumerate(stops.items(), 1)),
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\n" + "".join(
            f"{route},A,{route},{route},3\n" for route in routes),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                         "start_date,end_date\nD,1,1,1,1,1,1,1,20260101,20271231\n"),
        "trips.txt": "route_id,service_id,trip_id,direction_id\n" + "".join(
            f"{route},D,{trip},{'' if direction is None else direction}\n"
            for trip, route, direction, _calls in trips),
        "stop_times.txt": STOP_TIMES + "".join(calls(trip, list(stop_ids))
                                               for trip, _route, _direction, stop_ids in trips),
    }
