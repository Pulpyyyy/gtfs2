"""Build the datasource of a fixture zip, the way the integration builds one.

The code under test reads its timetable through a pygtfs schedule, so a test
hands it one: fixtures/static.zip goes through pygtfs.append_feed, as a
feed does on an install. This tree used to read the tables itself and
reshape them the way pygtfs stores them; every difference with pygtfs was a
test that failed, or passed, for a reason no install would see (weekday
flags stored as text, a padded header, calendar_dates.txt missing, times
without their microseconds). pygtfs costs about a second per fixture.

    schedule = fixture_db.shared("tests_provider/fixtures/palmbus")
    with schedule.engine.connect() as conn:
        rows = conn.execute(text("SELECT ... FROM trips")).fetchall()

shared() builds a fixture once per session and hands every test the same
schedule, to read. A test that writes into its database (test_night folds
calendar_dates into calendar) takes a build() of its own. Every database of
a session sits in one temporary folder, removed when the session ends.
"""
from __future__ import annotations

import atexit
import contextlib
import datetime
import io
import os
import shutil
import tempfile
import types

import ha_stub
import pygtfs
from sqlalchemy import event

_ROOT = None
_ENGINES = []
_SHARED = {}


def _root():
    """The session's folder, made on first use and removed at exit."""
    global _ROOT
    if _ROOT is None:
        _ROOT = tempfile.mkdtemp(prefix="gtfs2-fixture-")
        atexit.register(_remove_root)
    return _ROOT


def _remove_root():
    # SQLite keeps its file open until the engine lets go of it, and
    # Windows refuses to remove an open file
    for engine in _ENGINES:
        engine.dispose()
    shutil.rmtree(_ROOT, ignore_errors=True)


def build(fixtures):
    """A pygtfs schedule over fixtures/static.zip, indexed as an install
    indexes it, its SQLite clock frozen. A new database on each call."""
    directory = tempfile.mkdtemp(dir=_root())
    path = os.path.join(directory, "fixture.sqlite")
    schedule = pygtfs.Schedule(path)
    _ENGINES.append(schedule.engine)
    # pygtfs prints a line per table it reads
    with contextlib.redirect_stdout(io.StringIO()):
        pygtfs.append_feed(schedule, os.path.join(fixtures, "static.zip"))
    # and what an install adds before any query: the indexes the queries
    # lean on, the agency a route may lack. Without the indexes a train
    # departure took 0.74 s on Metro-North against 0.06 s, and the 48-feed
    # sweep ran for hours on queries no install makes that slowly
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: os.path.join(directory, *parts)))
    ha_stub.load("gtfs_helper").check_datasource_index(hass, schedule, "", "fixture")
    _freeze_sqlite_now(schedule.engine)
    return schedule


def shared(fixtures):
    """The session's one schedule of this fixture, for tests that only read."""
    key = os.path.abspath(fixtures)
    if key not in _SHARED:
        _SHARED[key] = build(fixtures)
    return _SHARED[key]


def _freeze_sqlite_now(engine):
    """Make the literal 'now' in a query answer with freezegun's clock.

    freezegun patches Python's clock, not SQLite's, which reads the real OS
    clock through its own C code: a query asking SQLite for
    datetime('now', 'localtime') would look at today's real date whatever
    freeze_time says, and a fixture built in 2026 would fail differently in
    2027. The component reads its clock in Python now, in the agency's zone,
    and sends SQLite no 'now'; this stays for a component that still does
    (an older checkout under test), and rewrites every 'now' about to be
    sent to the instant datetime.datetime.utcnow() reports, which is what
    freeze_time controls. Outside a freeze_time block that is the real time.
    """
    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def _substitute_now(conn, cursor, statement, parameters, context, executemany):
        if "'now'" in statement:
            frozen = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            statement = statement.replace("'now'", f"'{frozen}'")
        return statement, parameters
