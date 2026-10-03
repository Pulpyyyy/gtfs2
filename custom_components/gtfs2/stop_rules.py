"""What a stop is to the rider, as the queries write it: whether a call
lets them on or off (_boards, _alights, and _call_type for a call's
pickup_type or drop_off_type read in Python), the records of one place
(_place_group), the station names a train entry matches at each end
(station_names_in), and which route types are trains.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


# The SNCF files the coaches that stand in for its trains under the train line
# itself, so route_type calls them rail. Only the stop tells them apart: a
# coach calls at "StopPoint:OCECar TER-87543009" where the train calls at
# "StopPoint:OCETrain TER-87543009", same station, same name. On the national
# feed of September 2026, 283 of the 582 rail lines carry such coaches, and no
# coach shares a single stop_id with a train. "Navette" is not one of them: it
# is the rail shuttle between Tours and Saint-Pierre-des-Corps.
COACH_STOP_PREFIX = "StopPoint:OCECar "


RAIL_ROUTE_TYPES = (2, *range(100, 118))


# the same, as the queries write it
RAIL_ROUTE_TYPES_SQL = ",".join(str(t) for t in RAIL_ROUTE_TYPES)


def entry_stations(data: Mapping[str, Any], end: str) -> list[str]:
    """Every station a train entry matches at one end, "origin" or
    "destination": the ones ticked on the station screen, or the single name
    an entry created before that screen took several holds."""
    names = data.get(f"{end}_stations") or [data.get(end)]
    return [str(name) for name in names if name]


def station_names_in(prefix: str, names: Iterable[str] | None) -> tuple[str, dict[str, str]]:
    """An SQL "(:prefix_name_0, ...)" for a list of station names, and its
    parameters.

    A train entry may name several stations at one end: the station, and the
    coach station its replacement coaches leave from, which the feed files as
    a station of its own under another name (SNCF K8+: "Paris Austerlitz" for
    the trains, "Paris-Austerlitz Routiere" 240 m away for the coaches).
    Nothing in the feed links the two, so the rider ticks both.
    """
    names = [str(name) for name in names or [] if name] or [""]
    keys = [f"{prefix}_name_{n}" for n in range(len(names))]
    return "(" + ", ".join(f":{key}" for key in keys) + ")", dict(zip(keys, names))


# A place is what the rider waits at, whatever the feed writes it as. Most
# feeds give each side of the road a record of its own, one per direction,
# and some give one per platform: Zou files the two poles of Pont de la
# Brague, 8 m apart, under one parent station, and half of the line's trips
# are entered on the pole across the road from the way they drive. Picking a
# record hid the trips entered on the other one. So a place is the parent
# station when the feed has one; when it has none (TAO: 9 parents for 1359
# poles), the records of the same name within PLACE_LAT / PLACE_LON of each
# other, about 150 m, which gathered the three poles of Zenith, 107 m apart
# at most, and keeps apart two villages' "Centre". The box is measured from
# the record the entry holds, so the list and the queries agree on it.
PLACE_LAT = 0.00135


PLACE_LON = 0.002


def _place_group(param: str) -> str:
    """SQL "(...)" of every stop_id of the place of the stop bound to :param."""
    return f"""(
    select sibling.stop_id
    from stops chosen, stops sibling
    where chosen.stop_id = :{param}
      and (sibling.stop_id = chosen.stop_id
           or (chosen.parent_station is not null
               and chosen.parent_station <> ''
               and sibling.parent_station = chosen.parent_station)
           or ((chosen.parent_station is null or chosen.parent_station = '')
               and (sibling.parent_station is null or sibling.parent_station = '')
               and sibling.stop_name = chosen.stop_name
               and abs(sibling.stop_lat - chosen.stop_lat) <= {PLACE_LAT}
               and abs(sibling.stop_lon - chosen.stop_lon) <= {PLACE_LON})))"""


def _no_call_between(trip: str, board: str, alight: str, origin_group: str, end_group: str) -> str:
    """SQL: the ride from the board call to the alight call of the trip is
    its shortest, no call the rider can use at either end in between."""
    return f"""NOT EXISTS (
                SELECT 1 FROM stop_times between_stop
                WHERE between_stop.trip_id = {trip}.trip_id
                  AND between_stop.stop_sequence > {board}.stop_sequence
                  AND between_stop.stop_sequence < {alight}.stop_sequence
                  AND ((between_stop.stop_id IN {origin_group} AND {_boards("between_stop")})
                       OR (between_stop.stop_id IN {end_group} AND {_alights("between_stop")})))"""


# Whether the rider can get on, or off, at a call. stop_times says it per
# call: pickup_type and drop_off_type read 0 (or nothing) for a regular
# stop, 1 for none at all, 2 and 3 for a phone call or a word to the driver,
# which is still a way on. A 1 is not rare, and not only at the ends of a
# trip: a night train takes nobody on at its morning stops (SNCF: 2,080
# calls mid-route, 44 route-stop pairs where no trip ever boards), a coach
# sets down only on its way into town (Zou: 9,285 calls, 279 pairs), the
# Dutch feed flags 51,145 calls and 912 pairs. Offering such a call as a
# departure, or as a place to get off, sends the rider to a bus that will
# not open its door. The value is cast, pygtfs stores it as a number but a
# feed's blank is a NULL, and the db of a test may hold text.
def _boards(alias: str) -> str:
    """SQL: the rider can get on at this stop_times row."""
    return f"coalesce(cast({alias}.pickup_type as integer), 0) <> 1"


def _alights(alias: str) -> str:
    """SQL: the rider can get off at this stop_times row."""
    return f"coalesce(cast({alias}.drop_off_type as integer), 0) <> 1"


def _call_type(value: str | int | None) -> int:
    """A pickup_type / drop_off_type as the feed meant it: 0 when blank."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
