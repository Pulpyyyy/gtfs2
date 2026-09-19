"""What the route list reads for a line: _route_label and _set_apart.

The cases are the feeds' own: IDFM repeats the number as the long name on
1837 of its 2024 lines and lists the metro 1 of the RATP beside the bus 1 of
Terres d'Envol; the Netherlands feed has a tram 4 at GVB and at HTM; SNCF
leaves the long name at " -" and files fifty lines as "INCONNU" under one
agency.
"""
from __future__ import annotations

import ha_stub

route_names = ha_stub.load("route_names")


def test_a_long_name_that_repeats_the_number_is_dropped():
    assert route_names._route_label("1", "1") == "1"
    assert route_names._route_label("Licorne", " licorne ") == "Licorne"


def test_the_route_ends_stand_in_for_a_repeated_number():
    assert route_names._route_label("1", "1", "La Défense > Château de Vincennes") == \
        "1 : La Défense > Château de Vincennes"


def test_a_long_name_that_says_more_is_kept():
    assert route_names._route_label("T4", "Remplacement Tram T4") == "T4 : Remplacement Tram T4"
    assert route_names._route_label("4", "Lijn 4") == "4 : Lijn 4"
    assert route_names._route_label("INCONNU", " -", None, "R1") == "INCONNU"
    assert route_names._route_label(None, None, None, "R1") == "R1"


def test_look_alike_lines_of_two_operators_get_their_agency():
    options = ["1##M1##1", "3##B1##1", "3##B2##2##pruned"]
    got = route_names._set_apart(options, ["RATP", "Terres d'Envol", "RATP"])
    assert got == ["1##M1##1 · RATP", "3##B1##1 · Terres d'Envol", "3##B2##2##pruned"]


def test_look_alikes_of_one_agency_are_left_alone():
    options = ["3##S1##INCONNU", "2##S2##INCONNU"]
    assert route_names._set_apart(options, ["OCEdefault", "OCEdefault"]) == options
    # nor does a missing agency get a bare separator
    assert route_names._set_apart(["3##X##7", "3##Y##7"], ["GVB", None]) == \
        ["3##X##7 · GVB", "3##Y##7"]


def _feed(tmp_path, trips):
    """A source zip whose trips.txt holds (route_id, direction_id, headsign)."""
    import zipfile
    with zipfile.ZipFile(tmp_path / "feed.zip", "w") as zout:
        zout.writestr("trips.txt", "route_id,service_id,trip_id,trip_headsign,direction_id\n" + "".join(
            f"{r},S,T{i},{h},{d}\n" for i, (r, d, h) in enumerate(trips)))
    return str(tmp_path)


def test_the_trips_name_where_a_line_goes(tmp_path):
    gtfs_dir = _feed(tmp_path, [
        ("M4", "0", "Porte de Clignancourt"), ("M4", "0", "Porte de Clignancourt"),
        ("M4", "1", "Bagneux - Lucie Aubrac"), ("M4", "1", "Montparnasse Bienvenue"),
        ("M4", "1", "Bagneux - Lucie Aubrac")])
    assert route_names.headsign_ends(gtfs_dir, "feed", ["M4"]) == \
        {"M4": "Porte de Clignancourt ↔ Bagneux - Lucie Aubrac"}


def test_codes_in_the_headsign_are_not_places(tmp_path):
    # a train number (SNCF), a mission code (IDFM RER)
    gtfs_dir = _feed(tmp_path, [
        ("K8", "0", "44930"), ("K8", "1", "44931"),
        ("RERA", "0", "UZAR"), ("RERA", "1", "NATO")])
    assert route_names.headsign_ends(gtfs_dir, "feed", ["K8", "RERA"]) == {}


def test_a_feed_without_directions_gives_the_two_most_shown(tmp_path):
    gtfs_dir = _feed(tmp_path, [
        ("F", "", "Den Helder"), ("F", "", "Texel"), ("F", "", "Den Helder"), ("F", "", "Texel"),
        ("F", "", "Oudeschild")])
    assert route_names.headsign_ends(gtfs_dir, "feed", ["F"]) == {"F": "Den Helder ↔ Texel"}


def test_no_zip_no_trips_no_headsign_give_nothing(tmp_path):
    assert route_names.headsign_ends(str(tmp_path), "absent", ["X"]) == {}
    assert route_names.headsign_ends(None, "feed", ["X"]) == {}
    gtfs_dir = _feed(tmp_path, [("X", "0", "")])
    assert route_names.headsign_ends(gtfs_dir, "feed", ["X"]) == {}
