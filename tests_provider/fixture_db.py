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

A fixture of fixtures/ is imported once and kept, and each build() is a
copy of that import: the 49-feed sweep imported each feed again in every
process and every test file that read it (Leipzig four times in
test_night alone, 12 to 19 s each on the big cuts). The kept import sits
in the system temp folder under gtfs2-fixture-cache/<fixture>/, one per
fixture, named by a digest of the zip, of this file, of the component's
code and of pygtfs's version: any of them changed, the fixture is
imported again and its older import removed. A zip made on the fly (a
test's own feed in a temporary folder) is imported each time, as before.
"""
from __future__ import annotations

import atexit
import contextlib
import datetime
import hashlib
import io
import os
import shutil
import tempfile
import types
import zipfile
from importlib.metadata import version
from pathlib import Path

import ha_stub
import pygtfs
from sqlalchemy import event

_ROOT = None
_ENGINES = []
_SHARED = {}
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_CACHE = Path(tempfile.gettempdir()) / "gtfs2-fixture-cache"
_CODE = {}


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
    if Path(fixtures).resolve().parent == _FIXTURES:
        shutil.copyfile(_kept(fixtures), path)
        schedule = pygtfs.Schedule(path)
        _ENGINES.append(schedule.engine)
    else:
        schedule = _import(fixtures, directory, path)
    _freeze_sqlite_now(schedule.engine)
    return schedule


def _kept(fixtures):
    """The kept import of a fixture of fixtures/, made when missing."""
    source = os.path.join(fixtures, "static.zip")
    with open(source, "rb") as f:
        digest = hashlib.file_digest(f, "sha256")
    digest.update(_code_digest())
    folder = _CACHE / Path(fixtures).resolve().name
    kept = folder / f"{digest.hexdigest()[:16]}.sqlite"
    if kept.exists():
        return kept
    folder.mkdir(parents=True, exist_ok=True)
    directory = tempfile.mkdtemp(dir=_root())
    path = os.path.join(directory, "fixture.sqlite")
    _import(fixtures, directory, path).engine.dispose()
    # several processes may import the same fixture at once: each writes
    # its own file and renames it in place, the last rename wins whole
    partial = folder / f"{kept.name}.{os.getpid()}"
    shutil.copyfile(path, partial)
    try:
        os.replace(partial, kept)
    except PermissionError:
        # another process got there first and is copying it: Windows
        # refuses to replace an open file, and theirs is the same import
        if not kept.exists():
            raise
        with contextlib.suppress(OSError):
            partial.unlink()
    for older in folder.glob("*.sqlite"):
        if older != kept:
            # another process may still be copying it: Windows refuses,
            # and the next import of this fixture removes it
            with contextlib.suppress(OSError):
                older.unlink()
    return kept


def _code_digest():
    """What an import depends on besides the zip: this file, the
    component's code (the index step, the feed_info check) and pygtfs."""
    component = Path(ha_stub.COMPONENT)
    if component not in _CODE:
        digest = hashlib.sha256(Path(__file__).read_bytes())
        for source in sorted(component.glob("*.py")):
            digest.update(source.name.encode())
            digest.update(source.read_bytes())
        digest.update(version("pygtfs").encode())
        _CODE[component] = digest.digest()
    return _CODE[component]


def _import(fixtures, directory, path):
    """fixtures/static.zip through pygtfs into path, indexed."""
    schedule = pygtfs.Schedule(path)
    _ENGINES.append(schedule.engine)
    source = os.path.join(fixtures, "static.zip")
    if ha_stub.load("gtfs_filter").feed_info_unreadable(source):
        # feed_info dates pygtfs cannot read (Krakow's trams leave them
        # empty) stop its whole import: an install then leaves the table
        # out, and so does this
        source = _without(source, "feed_info.txt", directory)
    # pygtfs prints a line per table it reads
    with contextlib.redirect_stdout(io.StringIO()):
        pygtfs.append_feed(schedule, source)
    # and what an install adds before any query: the indexes the queries
    # lean on, the agency a route may lack. Without the indexes a train
    # departure took 0.74 s on Metro-North against 0.06 s, and the 48-feed
    # sweep ran for hours on queries no install makes that slowly
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: os.path.join(directory, *parts)))
    ha_stub.load("gtfs_helper").check_datasource_index(hass, schedule, "", "fixture")
    return schedule


def _without(source, name, directory):
    """A copy of the zip at source, in directory, without the member name."""
    copy = os.path.join(directory, "static.zip")
    with zipfile.ZipFile(source) as zin, \
            zipfile.ZipFile(copy, "w", zipfile.ZIP_DEFLATED) as zout:
        for member in zin.namelist():
            if member.rsplit("/", 1)[-1] != name:
                zout.writestr(member, zin.read(member))
    return copy


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
