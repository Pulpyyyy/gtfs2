"""The kept zip stays as the host sent it, whichever way it is imported.

The whole feed import and the legacy extract rewrote the kept zip without
the tables an import leaves out (shapes, transfers, translations...): a
source imported that way lost its shapes, and the zip no longer matched
the hash and size its sidecar recorded. pygtfs now skips those tables on
the way in. The legacy extract is gone since: a datasource opened with no
database answers so, and its zip stays as it is.
"""
from __future__ import annotations

import sqlite3
import types
import zipfile

import ha_stub

ha_stub.install()

gtfs_helper = ha_stub.load("gtfs_helper")
source_zip = ha_stub.load("source_zip")

FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,Europe/Paris\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_type\nR1,A,1,3\n",
    "trips.txt": "route_id,service_id,trip_id,shape_id\nR1,S,T1,SH1\n",
    "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                       "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"),
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,Gare,47.9,1.9\nS2,Centre,47.91,1.91\n",
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260101,20261231\n"),
    "shapes.txt": ("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
                   "SH1,47.9,1.9,1\nSH1,47.91,1.91,2\n"),
    # today's form, which pygtfs does not model (it expects trans_id, lang)
    "translations.txt": ("table_name,field_name,language,translation,record_id\n"
                         "stops,stop_name,en,Station,S1\n"),
}


def _zip(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    return path.read_bytes()


def _count(db, table):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"select count(*) from {table}").fetchone()[0]  # noqa: S608
    finally:
        conn.close()


def test_the_whole_feed_import_reads_the_zip_without_writing_it(tmp_path):
    sent = _zip(tmp_path / "src.zip")
    scratch = tmp_path / "src.import.sqlite"
    assert source_zip.build_scratch_database(str(tmp_path), "src.zip", str(scratch))
    assert (tmp_path / "src.zip").read_bytes() == sent
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src.import.sqlite", "src.zip"]
    assert (_count(scratch, "stop_times"), _count(scratch, "shapes"),
            _count(scratch, "translations")) == (2, 0, 0)


def test_a_datasource_opened_with_no_database_leaves_the_zip_alone(tmp_path):
    sent = _zip(tmp_path / "src.zip")
    hass = types.SimpleNamespace(config=types.SimpleNamespace(path=lambda p: p))
    assert gtfs_helper.get_gtfs(hass, str(tmp_path), {"file": "src", "url": "na",
                                                      "extract_from": "zip"}) == "not_built"
    assert (tmp_path / "src.zip").read_bytes() == sent
    assert sorted(p.name for p in tmp_path.iterdir()) == ["src.zip"]
