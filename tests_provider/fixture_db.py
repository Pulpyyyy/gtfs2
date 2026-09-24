"""Build the datasource of a fixture zip, the way the integration builds one.

The code under test reads its timetable through a pygtfs schedule, so a test
hands it one: fixtures/static.zip goes through pygtfs.append_feed, as a
feed does on an install. This tree used to read the tables itself and
reshape them the way pygtfs stores them; every difference with pygtfs was a
test that failed, or passed, for a reason no install would see (weekday
flags stored as text, a padded header, calendar_dates.txt missing, times
without their microseconds). pygtfs costs about a second per fixture, and a
fixture is built once per session.

    schedule = fixture_db.build("tests_provider/fixtures/palmbus")
    with schedule.engine.connect() as conn:
        rows = conn.execute(text("SELECT ... FROM trips")).fetchall()
"""
from __future__ import annotations

import contextlib
import datetime
import io
import os
import tempfile

import pygtfs
from sqlalchemy import event


def build(fixtures):
    """A pygtfs schedule over fixtures/static.zip, its SQLite clock frozen."""
    path = os.path.join(tempfile.mkdtemp(prefix="gtfs2-fixture-"), "fixture.sqlite")
    schedule = pygtfs.Schedule(path)
    # pygtfs prints a line per table it reads
    with contextlib.redirect_stdout(io.StringIO()):
        pygtfs.append_feed(schedule, os.path.join(fixtures, "static.zip"))
    _freeze_sqlite_now(schedule.engine)
    return schedule


def _freeze_sqlite_now(engine):
    """Make the literal 'now' in a query answer with freezegun's clock.

    The component's SQL asks SQLite for datetime('now', 'localtime') /
    date('now', 'localtime') directly; freezegun patches Python's clock, not
    SQLite's, which reads the real OS clock through its own C code no matter
    what freeze_time is doing. So a test that freezes time to a day the
    fixture's trips run still has the query looking at today's real date,
    which silently changes what a LIMIT-bound, now-ordered query returns as
    real time passes -- a fixture built in 2026 fails differently in 2027.

    Rather than touch the component's query, every 'now' the driver is about
    to send to SQLite is rewritten here, just before execution, to the
    instant datetime.datetime.utcnow() reports right then -- which is exactly
    what freeze_time controls. Outside a freeze_time block this is still the
    real time, so nothing changes when a test does not freeze the clock.
    """
    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _substitute_now(conn, cursor, statement, parameters, context, executemany):
        if "'now'" in statement:
            frozen = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            statement = statement.replace("'now'", f"'{frozen}'")
        return statement, parameters
