"""What the route list reads for a line: _route_label and _set_apart.

The cases are the feeds' own: IDFM repeats the number as the long name on
1837 of its 2024 lines and lists the metro 1 of the RATP beside the bus 1 of
Terres d'Envol; the Netherlands feed has a tram 4 at GVB and at HTM; SNCF
leaves the long name at " -" and files fifty lines as "INCONNU" under one
agency. Brisbane publishes its airport line once per period of validity,
eighteen route_ids reading the same.
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


def test_a_line_whose_days_are_over_gives_way_to_its_live_twin():
    # the Dutch feed: one line for the day the old timetable ended, one after
    options = ["3##N1##22", "3##N2##22", "3##X##40", "0##T1##22"]
    spans = {"N1": ("20260101", "20260301"), "N2": ("20260302", "20261231"),
             "X": ("20250101", "20250601"), "T1": ("20250101", "20250601")}
    got = route_names._leave_out_expired(options, spans, today="20260915")
    # a dead line with no twin stays, and the tram 22 is not the bus 22
    assert got == ["3##N2##22", "3##X##40", "0##T1##22"]


def test_a_feed_entirely_out_of_date_keeps_its_lines():
    options = ["3##A##7", "3##B##7"]
    spans = {"A": ("20240101", "20240601"), "B": ("20240602", "20241231")}
    assert route_names._leave_out_expired(options, spans, today="20260915") == options
    # nor does a line without dates count as over
    assert route_names._leave_out_expired(["3##A##7", "3##C##7"], spans, today="20260915") == \
        ["3##C##7"]


def test_look_alikes_of_different_periods_say_their_days():
    options = ["3##B1##29", "3##B2##29##pruned", "3##U##30"]
    spans = {"B1": ("20260901", "20261231"), "B2": ("20270104", "20270104"),
             "U": ("20260101", "20261231")}
    assert route_names._set_apart_by_span(options, spans) == [
        "3##B1##29 · 2026-09-01 → 2026-12-31",
        # a single day is said once
        "3##B2##29 · 2027-01-04##pruned",
        "3##U##30"]


def test_look_alikes_of_the_same_days_are_not_dated():
    # Leipzig's rail replacement runs, one name and one twelvemonth
    options = ["3##S1##SEV", "3##S2##SEV"]
    spans = {"S1": ("20260101", "20261231"), "S2": ("20260101", "20261231")}
    assert route_names._set_apart_by_span(options, spans) == options
    # a date that is not one is not shown
    assert route_names._set_apart_by_span(
        options, {"S1": ("20260101", "20261231"), "S2": ("2026", "x")}) == \
        ["3##S1##SEV · 2026-01-01 → 2026-12-31", "3##S2##SEV"]


def _dated_feed(tmp_path, files):
    import zipfile
    with zipfile.ZipFile(tmp_path / "feed.zip", "w") as zout:
        for name, body in files.items():
            zout.writestr(name, body)
    return str(tmp_path)


def test_the_days_a_line_runs_come_from_both_calendars(tmp_path):
    gtfs_dir = _dated_feed(tmp_path, {
        # no trip_headsign: four of the surveyed feeds leave it out
        "trips.txt": "route_id,service_id,trip_id\n"
                     "W,WEEK,T1\nW,SAT,T2\nD,DAYS,T3\nN,NONE,T4\n",
        "calendar.txt": "service_id,start_date,end_date\n"
                        "WEEK,20260105,20260630\nSAT,20260110,20260627\n",
        "calendar_dates.txt": "service_id,date,exception_type\n"
                              # an added day widens the window, a removed one never
                              "WEEK,20260702,1\nWEEK,20261225,2\n"
                              "DAYS,20260301,1\nDAYS,20260214,1\n"})
    assert route_names.route_spans(gtfs_dir, "feed", ["W", "D", "N", "absent"]) == {
        "W": ("20260105", "20260702"), "D": ("20260214", "20260301")}


def test_the_route_list_of_a_feed_cut_by_period(tmp_path):
    # Brisbane: the code before the dash is the line, the rest its period
    gtfs_dir = _dated_feed(tmp_path, {
        "agency.txt": "agency_id,agency_name\nTL,TransLink\n",
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                      "AIR-1,TL,AIR,,2\nAIR-2,TL,AIR,,2\nAIR-3,TL,AIR,,2\nGC-1,TL,GC,,2\n",
        "trips.txt": "route_id,service_id,trip_id\n"
                     "AIR-1,OLD,T1\nAIR-2,NEXT,T2\nAIR-3,ONE,T3\nGC-1,OLD,T4\n",
        "calendar.txt": "service_id,start_date,end_date\n"
                        "OLD,20000101,20000131\nNEXT,20990101,20990630\n",
        "calendar_dates.txt": "service_id,date,exception_type\nONE,20990701,1\n"})
    assert route_names.get_route_options_from_zip(gtfs_dir, "feed") == [
        "2##AIR-2##AIR · 2099-01-01 → 2099-06-30##pruned",
        "2##AIR-3##AIR · 2099-07-01##pruned",
        "2##GC-1##GC##pruned"]
