"""A zip put in the folder is held to the tables a download is held to.

A download had to carry routes.txt, trips.txt and stop_times.txt to be
kept; a zip the user put in the folder passed on routes.txt alone, and a
feed without trips or calls then ended on "no route has a timetable"
screens later. It also said "Could not download the file", of a file
nothing downloaded. It is now refused on its own words, not_a_feed; a zip
of zips still offers its networks, and an unreadable file stays
no_zip_file.
"""
from __future__ import annotations

import zipfile

import ha_stub

source_zip = ha_stub.load("source_zip")


def _zip(path, *members):
    with zipfile.ZipFile(path, "w") as zout:
        for name in members:
            zout.writestr(name, "x\n")
    return str(path)


def test_a_zip_with_the_three_tables_is_a_feed(tmp_path):
    zip_path = _zip(tmp_path / "a.zip", "routes.txt", "trips.txt", "stop_times.txt", "stops.txt")
    assert source_zip._holds_a_feed(zip_path) is None


def test_a_feed_nested_in_a_folder_is_a_feed(tmp_path):
    zip_path = _zip(tmp_path / "a.zip", "gtfs/routes.txt", "gtfs/trips.txt", "gtfs/stop_times.txt")
    assert source_zip._holds_a_feed(zip_path) is None


def test_routes_alone_is_not_a_feed(tmp_path):
    assert source_zip._holds_a_feed(_zip(tmp_path / "a.zip", "routes.txt", "stops.txt")) == "not_a_feed"


def test_no_table_at_all_is_not_a_feed(tmp_path):
    assert source_zip._holds_a_feed(_zip(tmp_path / "a.zip", "readme.pdf")) == "not_a_feed"


def test_a_zip_of_zips_still_offers_its_networks(tmp_path):
    zip_path = _zip(tmp_path / "a.zip", "google_bus.zip", "google_rail.zip")
    assert source_zip._holds_a_feed(zip_path) == "zip_holds_zips"


def test_an_unreadable_file_stays_no_zip_file(tmp_path):
    junk = tmp_path / "a.zip"
    junk.write_bytes(b"not a zip")
    assert source_zip._holds_a_feed(str(junk)) == "no_zip_file"
