"""What the realtime says of a journey sensor's line, one rule at a time.

get_next_services reads the trip updates for the line, the direction and
the stop a journey sensor follows (route mode): the departures the feed
announces there, soonest first, each with its delay and its trip. Until
now only the captured cases of test_route_combined ran it. Here each rule
is driven on a feed written by hand:

- a trip of the line and way the sensor follows is listed, another
  line's or the other way's is not;
- a line id the feed qualifies ("1:R1") still names the line, a longer
  number never passes for a shorter one ("R11" is not "R1");
- an update naming no direction counts only for the trip the sensor
  watches, found in a qualified id too, never in a longer number;
- a trip of the sensor's own board counts whatever direction the feed
  gives it: the timetable's, possibly repaired, overrules the feed's;
- a feed naming no line at all is read by trip.
"""
from __future__ import annotations

import datetime
import sys
import types
from unittest.mock import patch

from freezegun import freeze_time

import ha_stub

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")
rt_feed = sys.modules["gtfs2_under_test.feed.rt_feed"]
const = sys.modules["gtfs2_under_test.const"]

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 27, 11, 0, tzinfo=UTC)


def at(minutes):
    return int((NOW + datetime.timedelta(minutes=minutes)).timestamp())


def update(trip, minutes, route="R1", direction=0, stop="S1", delay=None):
    """One trip update calling at stop, leaving in some minutes."""
    departure = {"time": at(minutes)}
    if delay is not None:
        departure["delay"] = delay
    tagged = {"trip_id": trip}
    if route is not None:
        tagged["route_id"] = route
    if direction is not None:
        tagged["direction_id"] = direction
    return {"id": f"e-{trip}", "trip_update": {
        "trip": tagged, "stop_time_update": [{"stop_id": stop, "departure": departure}]}}


def sensor(trip="T1", board=(), direction="0"):
    return types.SimpleNamespace(
        _data={"file": "src", "next_departure": {}}, _headers={}, _vehicle_position_url=None,
        _trip_update_url="http://rt.test/trips",
        _route_id="R1", _trip_id=trip, _trip_short_name="", _direction=direction,
        _stop_id="S1", _destination_id="S9", _stop_sequence=None,
        _trip_list=list(board))


def services(feed, me=None):
    me = me or sensor()
    with freeze_time(NOW), patch.object(rt_feed, "get_gtfs_feed_entities",
                                        lambda **_kw: feed):
        return gtfs_rt_helper.get_next_services(me)


def trips(attrs):
    return attrs[const.ATTR_NEXT_RT_TRIPS]


def test_the_line_and_way_followed_soonest_first_each_with_its_delay():
    got = services([update("T2", 12, delay=60), update("T1", 5, delay=120),
                    update("T3", 8, direction=1), update("T4", 6, route="R2"),
                    update("T5", 7, stop="S2")])
    assert trips(got) == ["T1", "T2"]
    assert got[const.ATTR_NEXT_RT_DELAYS] == [120, 60]
    assert [d.timestamp() for d in got[const.ATTR_NEXT_RT]] == [at(5), at(12)]
    # the first departure, as an instant (the "Due in" key that said it
    # too was read by nothing and went)
    assert got[const.ATTR_NEXT_RT][0].utcoffset() is not None


def test_a_line_the_feed_qualifies_is_still_the_line():
    got = services([update("T1", 5, route="1:R1"), update("T2", 6, route="agency_R1"),
                    update("T3", 7, route="R11"), update("T4", 8, route="1:R11")])
    assert trips(got) == ["T1", "T2"]


def test_without_a_direction_only_the_watched_trip_counts():
    got = services([update("T1", 5, direction=None), update("T7", 6, direction=None)])
    assert trips(got) == ["T1"]


def test_the_watched_trip_is_found_in_a_qualified_id_never_in_a_longer_number():
    me = sensor(trip="100")
    got = services([update("OP:100:20260927", 5, direction=None),
                    update("2100", 6, direction=None), update("1005", 7, direction=None)], me)
    assert trips(got) == ["OP:100:20260927"]


def test_a_trip_of_the_board_counts_whatever_way_the_feed_gives_it():
    # the feed says direction 1, the timetable (repaired) says 0
    got = services([update("T2", 9, direction=1), update("T8", 10, direction=1)],
                   sensor(board=["T1", "T2"]))
    assert trips(got) == ["T2"]


def test_a_feed_naming_no_line_is_read_by_trip():
    got = services([update("T1", 5, route=None, direction=None),
                    update("T2", 6, route=None, direction=None)],
                   sensor(board=["T1", "T2"]))
    assert trips(got) == ["T1", "T2"]
    got = services([update("T9", 5, route=None, direction=None)])
    assert trips(got) == []


def test_names_trip_reads_whole_ids_between_separators():
    assert gtfs_rt_helper._names_trip("T1", "T1")
    assert gtfs_rt_helper._names_trip("100", "OP:100")
    assert gtfs_rt_helper._names_trip("100", "100-20260927")
    assert not gtfs_rt_helper._names_trip("100", "2100")
    assert not gtfs_rt_helper._names_trip("100", "1005")
    assert not gtfs_rt_helper._names_trip("", "100")
    assert not gtfs_rt_helper._names_trip(None, "100")
