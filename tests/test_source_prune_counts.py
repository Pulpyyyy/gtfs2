"""A prune's dry run counts what the prune keeps, table by table.

prune_gtfs_datasource keeps the trips of the lines asked for, what hangs
off them and the calendar of the services they run on; its dry run only
counts. Both are read here on a real pygtfs database, plain and interned:
the dry run announces, for every table, the rows the prune then leaves.
"""
from __future__ import annotations

import feed_db
import ha_stub

shrink = ha_stub.load("shrink")

HEAD = feed_db.STOP_TIMES
FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nS1,One,0,0\nS2,Two,0,0.01\nS3,Three,0,0.02\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                  "R1,A,1,One,3\nR2,A,2,Two,3\n",
    "trips.txt": "route_id,service_id,trip_id\nR1,WK,T1\nR1,WK,T2\nR2,SAT,T3\n",
    "stop_times.txt": HEAD + (
        "T1,08:00:00,08:00:00,S1,1\nT1,08:10:00,08:10:00,S2,2\n"
        "T2,09:00:00,09:00:00,S1,1\nT2,09:10:00,09:10:00,S2,2\n"
        "T3,10:00:00,10:00:00,S2,1\nT3,10:10:00,10:10:00,S3,2\nT3,10:20:00,10:20:00,S1,3\n"),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nWK,1,1,1,1,1,0,0,20260901,20261231\n"
                     "SAT,0,0,0,0,0,1,0,20260901,20261231\n"),
    "calendar_dates.txt": "service_id,date,exception_type\nWK,20261225,2\nSAT,20261226,2\n",
}

COUNTED = ("trips", "stop_times", "calendar", "calendar_dates", "gtfs2_stop_times")


def _source(tmp_path, interned=False):
    feed_db.build(tmp_path, FEED).engine.dispose()
    if interned:
        assert shrink.intern_gtfs_datasource(str(tmp_path), "feed")
    return str(tmp_path)


def _announced(stats):
    return {k: v for k, v in stats.items() if k.rsplit("_", 1)[0] in COUNTED}


def _rows(tmp_path, table):
    return feed_db.rows(tmp_path / "feed.sqlite", f"select count(*) from {table}")[0][0]  # noqa: S608


def test_the_dry_run_counts_what_the_prune_keeps(tmp_path):
    gtfs_dir = _source(tmp_path)
    dry = shrink.prune_gtfs_datasource(gtfs_dir, "feed", ["R1"], dry_run=True)
    assert _rows(tmp_path, "stop_times") == 7
    done = shrink.prune_gtfs_datasource(gtfs_dir, "feed", ["R1"])
    assert _announced(dry) == _announced(done)
    assert (done["trips_after"], done["stop_times_after"]) == (2, 4)
    assert (done["calendar_after"], done["calendar_dates_after"]) == (1, 1)
    for table in ("stop_times", "calendar", "calendar_dates"):
        assert _rows(tmp_path, table) == done[f"{table}_after"]


def test_an_interned_datasource_keeps_its_rows_and_keys(tmp_path):
    gtfs_dir = _source(tmp_path, interned=True)
    dry = shrink.prune_gtfs_datasource(gtfs_dir, "feed", ["R1"], dry_run=True)
    done = shrink.prune_gtfs_datasource(gtfs_dir, "feed", ["R1"])
    assert _announced(dry) == _announced(done)
    assert (done["gtfs2_stop_times_before"], done["gtfs2_stop_times_after"]) == (7, 4)
    assert _rows(tmp_path, "gtfs2_stop_times") == 4
    # the keys of the trips and stops gone go with them
    assert _rows(tmp_path, "gtfs2_trip_key") == 2
    assert _rows(tmp_path, "gtfs2_stop_key") == 2
