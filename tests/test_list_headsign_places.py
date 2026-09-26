"""A town written in capitals is a destination, a mission code is not.

A short word in capitals with no space was taken for a code, IDFM's RER
missions (UZAR) and SNCF's train numbers alike; NICE or PAU, a town
written in capitals, was thrown out with them. A word that names a
place of the feed stays a destination.
"""
from __future__ import annotations

import zipfile

import ha_stub

route_names = ha_stub.load("route_names")

STOPS = ("stop_id,stop_name,stop_lat,stop_lon\n"
         "S1,Nice Ville,0,0\nS2,Marseille Saint-Charles,0,0\nS3,Pau,0,0\nS4,Bordeaux,0,0\n")
TRIPS = ("route_id,service_id,trip_id,direction_id,trip_headsign\n"
         "R1,W,T1,0,NICE\nR1,W,T2,1,Marseille Saint-Charles\n"
         "R2,W,T3,0,PAU\nR2,W,T4,1,Bordeaux\n"
         "R3,W,T5,0,UZAR\nR3,W,T6,1,UZAR\n")


def test_towns_in_capitals_stay_destinations(tmp_path):
    zip_path = tmp_path / "src.zip"
    with zipfile.ZipFile(zip_path, "w") as zout:
        zout.writestr("stops.txt", STOPS)
        zout.writestr("trips.txt", TRIPS)
    ends = route_names.headsign_ends(str(tmp_path), "src", ["R1", "R2", "R3"])
    assert ends["R1"] == "NICE ↔ Marseille Saint-Charles"
    assert ends["R2"] == "PAU ↔ Bordeaux"
    assert "R3" not in ends


def test_a_code_without_a_place_is_a_code():
    assert not route_names._names_a_place("UZAR", frozenset({"nice", "nice ville"}))
    assert route_names._names_a_place("NICE", frozenset({"nice", "nice ville"}))
    assert not route_names._names_a_place("44930")
