"""Two variants riding two places in opposite orders, then the terminus.

Each of the two places came after the other, so neither was ever free:
the walk fell back on the busiest place left, the terminus every ride
reaches, and listed it before them (TEC B0026 listed Noduwez after
Jodoigne Gare d'Autobus; the 48-feed sweep, 2026-09-26). The two form a
group now, free once what comes before it is placed.
"""
from __future__ import annotations

import feed_db
import ha_stub

ha_stub.install()

places = ha_stub.load("data.places")
place_destinations = ha_stub.load("data.place_destinations")

HEAD = feed_db.STOP_TIMES


_calls = feed_db.calls


FEED = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,A,http://a,UTC\n",
    "stops.txt": ("stop_id,stop_name,stop_lat,stop_lon\nO,Origin,45.0,1.0\nA,Alpha,45.1,1.1\n"
                  "B,Bravo,45.2,1.2\nT,Terminus,45.3,1.3\n"),
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR,A,1,One,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR,S,T1,0\nR,S,T2,0\nR,S,T3,0\nR,S,T4,0\n",
    # and an express straight to the terminus: the busiest place of all
    "stop_times.txt": (HEAD + _calls("T1", "OABT") + _calls("T2", "OABT") + _calls("T3", "OBAT")
                       + _calls("T4", "OT")),
    "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                     "start_date,end_date\nS,1,1,1,1,1,1,1,20260901,20261231\n"),
}


def test_a_cycle_between_variants_comes_before_the_terminus(tmp_path):
    schedule = feed_db.build(tmp_path, FEED)
    try:
        listed = [str(s).split(":")[0] for s in
                  places.get_destination_stop_list(schedule, "R", None, "O")]
        # the busier variant's order first, the terminus last
        assert listed == ["A", "B", "T"]
    finally:
        schedule.engine.dispose()


def test_places_that_order_each_other_are_one_group():
    before = {"O": set(), "A": {"O", "B"}, "B": {"O", "A"}, "T": {"A", "B"}}
    group = place_destinations._groups_of(before)
    assert group["A"] == group["B"]
    assert len({group["O"], group["A"], group["T"]}) == 3
