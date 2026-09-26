"""The lines a train entry's departures rode, read back from its database.

A train entry stores "train" for its line and rides whatever line serves
its two stations; its map files are named after those lines, and its
removal left them behind for good. The lines are read back from the
source's database when the entry goes.
"""
from __future__ import annotations

from pathlib import Path

import pygtfs

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")

FEED = Path(__file__).parents[1] / "tests_provider" / "fixtures" / "sncf" / "static.zip"


def _database(tmp_path):
    schedule = pygtfs.Schedule(str(tmp_path / "sncf.sqlite"))
    pygtfs.append_feed(schedule, str(FEED))
    schedule.engine.dispose()
    return tmp_path / "sncf.sqlite"


# Arles then Miramas is ridden by the K7, K9 and K34; the C7 calls at
# Miramas but never at Arles (read from the fixture's stop_times, written
# out rather than asked of the database again the component's way)
K7 = "FR:Line::7B48FDEF-35FE-45ED-BA4D-A638BCE7E21A:"
K9 = "FR:Line::A997C5D1-9FC9-42ED-B213-C74411746662:"
K34 = "FR:Line::b1eda504-6395-4189-9623-6460d66f2bae:"


def test_the_lines_between_the_two_stations(tmp_path):
    _database(tmp_path)
    data = {"file": "sncf", "route": "train", "origin": "Arles", "destination": "Miramas"}
    assert set(gtfs_helper.train_entry_routes(str(tmp_path), data)) == {K7, K9, K34}


def test_no_database_no_lines(tmp_path):
    assert gtfs_helper.train_entry_routes(str(tmp_path), {"file": "gone", "origin": "A",
                                                          "destination": "B"}) == []
