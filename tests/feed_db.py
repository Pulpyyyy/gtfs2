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

rows() reads a database file back, the way a test checks what a step
left on disk, and lets the file go after.
"""
from __future__ import annotations

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
