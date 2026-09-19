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


def test_look_alikes_of_one_agency_get_their_ends():
    # IDFM: one operator, one name, routes to Chartres and to Montargis
    options = ["2##C1##TER : TER Centre", "2##C2##TER : TER Centre##pruned",
               "2##C3##TER : TER Centre-Val", "3##I1##INCONNU : A ↔ B", "3##I2##INCONNU : A ↔ B"]
    twins = route_names._look_alikes(options)
    assert twins == ["C1", "C2", "I1", "I2"]
    got = route_names._set_apart_by_ends(options, {
        "C1": "Montparnasse ↔ Chartres", "C2": "Bercy ↔ Montargis", "I1": "A ↔ B"})
    assert got == ["2##C1##TER : TER Centre · Montparnasse ↔ Chartres",
                   "2##C2##TER : TER Centre · Bercy ↔ Montargis##pruned",
                   # a label nobody else wears, and one already showing its ends
                   "2##C3##TER : TER Centre-Val", "3##I1##INCONNU : A ↔ B",
                   "3##I2##INCONNU : A ↔ B"]


def test_look_alikes_of_two_modes_are_left_to_the_mode():
    # Zou's P18 train and P18 coach: the flow says "(train)" and "(coach)"
    options = ["2##P18T##P18 : Nîmes-Avignon", "3##P18C##P18 : Nîmes-Avignon"]
    assert route_names._look_alikes(options) == []


def test_look_alikes_without_destinations_read_the_stops(tmp_path):
    # SNCF: the trips show a train number, only stop_times says where they go
    import zipfile
    with zipfile.ZipFile(tmp_path / "feed.zip", "w") as zout:
        zout.writestr("trips.txt", "route_id,service_id,trip_id,trip_headsign\n"
                      "R1,S,T1,3731\nR1,S,T2,3733\nR2,S,T3,3740\nR3,S,T4,1\n")
        zout.writestr("stop_times.txt", "trip_id,stop_sequence,stop_id\n"
                      "T1,1,PA\nT1,2,TA\nT2,1,PA\nT2,2,LO\nT2,3,TA\nT3,5,TA\nT3,9,PA\n")
        zout.writestr("stops.txt", "stop_id,stop_name\nPA,Paris\nLO,Lourdes\nTA,Tarbes\n")
    got = route_names.look_alike_ends(None, str(tmp_path), "feed", ["R1", "R2", "R3"])
    # the trip with the most stops draws the line; R3 has no stop to read
    assert got == {"R1": "Paris > Tarbes", "R2": "Tarbes > Paris"}


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


def test_lines_of_one_number_and_several_modes_say_their_mode():
    options = ["1##M6##6 : Nation ↔ Charles de Gaulle - Étoile",
               "3##R6##6 : Remplacement Métro 6",
               "3##B6##6 : Gare de Bourg-la-Reine",
               "1##M3B##3B : Porte des Lilas ↔ Gambetta"]
    words = {"metro": "métro", "bus": "bus"}
    assert route_names.with_modes(options, words) == [
        "6 : Nation ↔ Charles de Gaulle - Étoile (métro)",
        "6 : Remplacement Métro 6 (bus)",
        "6 : Gare de Bourg-la-Reine (bus)",
        "3B : Porte des Lilas ↔ Gambetta"]


def test_lines_of_one_number_and_one_mode_keep_their_label():
    options = ["0##T4a##4 : Lijn 4 · GVB", "0##T4b##4 : Lijn 4 · HTM", "99##X##9"]
    assert route_names.with_modes(options, {"tram": "tram"}) == [
        "4 : Lijn 4 · GVB", "4 : Lijn 4 · HTM", "9"]


def test_route_types_basic_and_extended():
    assert [route_names.line_mode(t) for t in ("0", "1", "2", "3", "4", "5", "6", "7", "11", "12")] == [
        "tram", "metro", "train", "bus", "ferry", "cable_tram", "aerial_lift", "funicular",
        "trolleybus", "monorail"]
    assert [route_names.line_mode(t) for t in ("100", "200", "401", "700", "900", "1300", "99", "x")] == [
        "train", "coach", "metro", "bus", "tram", "aerial_lift", None, None]
