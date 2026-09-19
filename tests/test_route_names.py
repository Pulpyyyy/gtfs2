"""What the route list reads for a line: _route_label and _set_apart.

The cases are the feeds' own: IDFM repeats the number as the long name on
1837 of its 2024 lines and lists the metro 1 of the RATP beside the bus 1 of
Terres d'Envol; the Netherlands feed has a tram 4 at GVB and at HTM; SNCF
leaves the long name at " -" and files fifty lines as "INCONNU" under one
agency.
"""
from __future__ import annotations

import ha_stub

gtfs_helper = ha_stub.load("gtfs_helper")


def test_a_long_name_that_repeats_the_number_is_dropped():
    assert gtfs_helper._route_label("1", "1") == "1"
    assert gtfs_helper._route_label("Licorne", " licorne ") == "Licorne"


def test_the_route_ends_stand_in_for_a_repeated_number():
    assert gtfs_helper._route_label("1", "1", "La Défense > Château de Vincennes") == \
        "1 : La Défense > Château de Vincennes"


def test_a_long_name_that_says_more_is_kept():
    assert gtfs_helper._route_label("T4", "Remplacement Tram T4") == "T4 : Remplacement Tram T4"
    assert gtfs_helper._route_label("4", "Lijn 4") == "4 : Lijn 4"
    assert gtfs_helper._route_label("INCONNU", " -", None, "R1") == "INCONNU"
    assert gtfs_helper._route_label(None, None, None, "R1") == "R1"


def test_look_alike_lines_of_two_operators_get_their_agency():
    options = ["1##M1##1", "3##B1##1", "3##B2##2##pruned"]
    got = gtfs_helper._set_apart(options, ["RATP", "Terres d'Envol", "RATP"])
    assert got == ["1##M1##1 · RATP", "3##B1##1 · Terres d'Envol", "3##B2##2##pruned"]


def test_look_alikes_of_one_agency_are_left_alone():
    options = ["3##S1##INCONNU", "2##S2##INCONNU"]
    assert gtfs_helper._set_apart(options, ["OCEdefault", "OCEdefault"]) == options
    # nor does a missing agency get a bare separator
    assert gtfs_helper._set_apart(["3##X##7", "3##Y##7"], ["GVB", None]) == \
        ["3##X##7 · GVB", "3##Y##7"]
