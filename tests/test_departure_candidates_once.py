"""The trips riding a pair are read once for a database, whatever the time.

Each reading of the timetable looked again for the trips that ride from
the origin to the destination, which does not depend on the time: 2 of
the 2.8 s a reading of TAO tram A took, every quarter of an hour and each
time the departure shown left. They are now read once for a schedule and
a pair, and the departures still follow the clock; a database opened
again, a new edition, has them read again.
"""
from __future__ import annotations

import pygtfs
from freezegun import freeze_time
from sqlalchemy import event

import feed_db
import ha_stub

ha_stub.install()

departures = ha_stub.load("data.departures")

HEAD = feed_db.STOP_TIMES
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,47.0,1.0\nS2,Two,47.1,1.1\nS3,Three,47.2,1.2\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,Red,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,D,T10,0\nR,D,T11,0\n",
    "stop_times.txt": (HEAD + "T10,10:00:00,10:00:00,S1,1\nT10,10:10:00,10:10:00,S2,2\n"
                       "T10,10:20:00,10:20:00,S3,3\n"
                       "T11,11:00:00,11:00:00,S1,1\nT11,11:10:00,11:10:00,S2,2\n"
                       "T11,11:20:00,11:20:00,S3,3\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nD,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def _database(tmp_path):
    schedule = feed_db.build(tmp_path, FEED)
    schedule.engine.dispose()
    return str(tmp_path / "feed.sqlite")


def _counted(schedule):
    """The number of times the trips of a pair are looked for."""
    looked = []

    def note(_conn, _cursor, statement, *_rest):
        if "FROM trips trip" in statement and "json_each" not in statement:
            looked.append(statement)
    event.listen(schedule.engine, "before_cursor_execute", note)
    return looked


def _departures(schedule, at, origin="S1", destination="S3"):
    with freeze_time(at):
        rows, _start = departures._fetch_departure_rows("3", origin, destination, schedule, None, "R")
    return [row["trip_id"] for row in rows]


def test_the_trips_of_a_pair_are_read_once_and_the_clock_still_counts(tmp_path):
    schedule = pygtfs.Schedule(_database(tmp_path))
    looked = _counted(schedule)
    try:
        first = _departures(schedule, "2026-09-24 09:00:00")
        later = _departures(schedule, "2026-09-24 10:30:00")
        assert first[:2] == ["T10", "T11"]
        assert later[0] == "T11"
        assert len(looked) == 1
        # another pair of the same database is looked for on its own
        _departures(schedule, "2026-09-24 09:00:00", "S2", "S3")
        assert len(looked) == 2
    finally:
        schedule.engine.dispose()


def test_a_database_opened_again_has_them_read_again(tmp_path):
    path = _database(tmp_path)
    first = pygtfs.Schedule(path)
    _departures(first, "2026-09-24 09:00:00")
    first.engine.dispose()
    again = pygtfs.Schedule(path)
    looked = _counted(again)
    try:
        assert _departures(again, "2026-09-24 09:00:00")[:2] == ["T10", "T11"]
        assert len(looked) == 1
    finally:
        again.engine.dispose()
