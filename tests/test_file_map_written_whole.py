"""The map files are written whole, or not at all.

The cards fetch the vehicle, route and leg files while the sensors
rewrite them; written in place, a fetch landing mid-write read a
truncated document. They are written beside their target and renamed.
"""
from __future__ import annotations

import json

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")
geojson = ha_stub.load("geojson")


def test_a_file_is_replaced_whole(tmp_path):
    target = tmp_path / "r_0.json"
    target.write_text('{"features": [1, 2, 3], "type": "FeatureCollection"}')
    gtfs_rt_helper.write_json_file(str(target), {"features": [], "type": "FeatureCollection"})
    assert json.loads(target.read_text()) == {"features": [], "type": "FeatureCollection"}
    assert [p.name for p in tmp_path.iterdir()] == ["r_0.json"]


def test_a_failed_write_leaves_the_file_as_it_was(tmp_path):
    target = tmp_path / "r_0.json"
    target.write_text('{"features": [1]}')
    try:
        gtfs_rt_helper.write_json_file(str(target), {"features": [object()]})
    except TypeError:
        pass
    assert json.loads(target.read_text()) == {"features": [1]}
    assert [p.name for p in tmp_path.iterdir()] == ["r_0.json"]


def test_two_writers_at_once_each_write_whole(tmp_path):
    # two entries on one line write the same vehicle file in the same second
    import threading
    target = tmp_path / "r_0.json"
    errors = []

    def write(n):
        for i in range(300):
            try:
                gtfs_rt_helper.write_json_file(str(target), {"features": [n] * 200, "i": i})
            except Exception as ex:  # pylint: disable=broad-except
                errors.append(repr(ex))

    threads = [threading.Thread(target=write, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert set(json.loads(target.read_text())["features"]) in ({1}, {2})
    assert [p.name for p in tmp_path.iterdir()] == ["r_0.json"]


def test_the_timetable_and_leg_writer_goes_through_it(tmp_path):
    target = tmp_path / "leg.json"
    geojson._WRITTEN.clear()
    assert geojson.write_json_if_changed(str(target), {"a": 1, "at": "now"}, {"a": 1})
    assert not geojson.write_json_if_changed(str(target), {"a": 1, "at": "later"}, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1, "at": "now"}
    assert [p.name for p in tmp_path.iterdir()] == ["leg.json"]
