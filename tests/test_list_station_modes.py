"""Which modes call at each station of a rail line, as the train picker labels it.

The mode is read from the stop, the only place the feed says it: an id
starting with the SNCF coach prefix is a coach. Only a line mixing both
modes gets labels; a line of one mode gets none. Set here with the answer
written out; the provider sweep only checks the labels name stations the
line calls at.
"""
from __future__ import annotations

import io
import zipfile

import pygtfs

import ha_stub

ha_stub.install()

stations = ha_stub.load("stations")

TRAIN = "StopPoint:OCETrain TER-87543009"
COACH = "StopPoint:OCECar TER-87543009"
COACH_ONLY = "StopPoint:OCECar TER-87547026"
PARIS = "StopPoint:OCETrain TER-87547000"
HEAD = "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"


def _calls(trip, stops):
    return "".join(f"{trip},08:{n:02d}:00,08:{n:02d}:00,{s},{n}\n" for n, s in enumerate(stops, 1))


FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,Europe/Paris\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\n"
                  f"{TRAIN},Orléans,47.9,1.9\n{COACH},Orléans,47.9,1.9\n"
                  f"{COACH_ONLY},Paris Austerlitz Routière,48.8,2.3\n{PARIS},Paris Austerlitz,48.8,2.3\n"),
    "routes.txt": ("route_id,agency_id,route_short_name,route_long_name,route_type\n"
                   "MIXED,A,K8,Orléans - Paris,2\nTRAINS,A,P8,Orléans - Paris,2\n"),
    "trips.txt": "route_id,service_id,trip_id,direction_id\nMIXED,S,T1,0\nMIXED,S,C1,0\nTRAINS,S,T2,0\n",
    "stop_times.txt": (HEAD + _calls("T1", [TRAIN, PARIS]) + _calls("C1", [COACH, COACH_ONLY])
                       + _calls("T2", [TRAIN, PARIS])),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def test_a_mixed_line_labels_each_station_with_its_modes(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zout:
        for name, body in FEED.items():
            zout.writestr(name, body)
    (tmp_path / "feed.zip").write_bytes(buffer.getvalue())
    schedule = pygtfs.Schedule(str(tmp_path / "feed.sqlite"))
    pygtfs.append_feed(schedule, str(tmp_path / "feed.zip"))
    try:
        assert stations.get_station_modes(schedule, "MIXED") == {
            "Orléans": {"train", "coach"},
            "Paris Austerlitz": {"train"},
            "Paris Austerlitz Routière": {"coach"},
        }
        # a line of trains alone: nothing to tell apart
        assert stations.get_station_modes(schedule, "TRAINS") == {}
    finally:
        schedule.engine.dispose()
