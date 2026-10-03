"""The timetable file under www/gtfs2: every departure of an entry over the
service day under way and the two after it (write_timetable_file), and its
name (timetable_name). Written by exports.export_timetable in the
background.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
import homeassistant.util.dt as dt_util

from .const import id_of
from .feed_window import last_service_day
from .geojson import entry_file_part, map_file, write_json_if_changed
from .gtfs_helper import _fetch_departure_rows, departure_query_args, get_next_service_date
from .clocks import _leg_timezone

_LOGGER = logging.getLogger(__name__)


# The service days the timetable holds: the one under way and the two after
# it, so that it always reaches at least 48 hours ahead - at 23:00 the rest
# of the evening and two whole days, just past a day change nearly three.
TIMETABLE_DAYS = 3
# a safeguard on the rows of one read, not a length: a metro over three
# days is some 900 departures, a train a few dozen
TIMETABLE_ROWS_MAX = 5000


def timetable_name(name: str) -> str:
    """File name of an entry's timetable. The entry's name alone: unlike the
    route and positions files it is this sensor's, and a train entry's
    departures may ride several routes. Kept in one place, like the others,
    so the writer, the attribute and the removal agree."""
    return f"timetable_{entry_file_part(name)}.json"


def _local(stamp: object, zone: datetime.tzinfo) -> str | None:
    """A naive 'YYYY-MM-DD HH:MM:SS' of the query, in the line's zone, as an
    ISO datetime with its offset; None when unreadable."""
    try:
        moment = datetime.datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone).isoformat()


def timetable_doc(name: str, rows: Iterable[Mapping[str, Any]], service_dates: Iterable[str],
                  zone: datetime.tzinfo, next_departure: str | None = None,
                  until: str | None = None,
                  generated: datetime.datetime | None = None) -> dict[str, Any]:
    """The timetable file's content, from the departure rows of a window.

    service_dates are the days the file stands for, each listed even when
    no departure runs on it: an empty day says the timetable is known and
    has nothing, which a missing file cannot say. A row of the day before
    the first (a run of last night's service leaving after midnight) gets
    a day of its own ahead of them. Each departure is its trip, when it
    leaves the entry's origin and when it reaches its destination, both
    real datetimes: a run past midnight keeps its service day and carries
    the next calendar date.

    next_departure is the first departure after the window, found in the
    calendar, and until the last day the feed has any service on: past
    it nothing can be known, so an empty window with no next departure
    says "nothing published until then" rather than "never".
    """
    days: dict[str, list[dict[str, str | None]]] = {d: [] for d in service_dates}
    for row in rows:
        day = str(row.get("origin_depart_date") or "")[:10]
        dep = _local(row.get("origin_depart_dt"), zone)
        if not day or not dep:
            continue
        days.setdefault(day, []).append({
            "trip_id": str(row.get("trip_id")),
            "dep": dep,
            "arr": _local(row.get("dest_arrival_dt"), zone),
        })
    return {
        "entry": name,
        "timezone": str(zone),
        "generated": (generated or dt_util.now()).isoformat(),
        "days": [{"service_date": d, "departures": sorted(days[d], key=lambda x: x["dep"])}
                 for d in sorted(days)],
        "next": next_departure,
        "until": until,
    }


def write_timetable_file(hass: HomeAssistant, data: Mapping[str, Any], today: str, zip_path: str) -> str:
    """Write www/gtfs2/timetable_<entry>.json: every departure of the entry
    from now to the end of the third service day, today's included.

    The sensor lists ten departures, enough for a board and too few for a
    journey: a card chaining a bus, a train and a metro needs the metro an
    hour and a half ahead, where a line every four minutes has long run
    out of listed runs. The card reads the sensor first, realtime and all,
    and this file past it. Written from the same query as the sensor, so
    both agree on the calendar, the places and the runs after midnight;
    rewritten when the service day, the zip or the database changes (see
    the coordinator), not on every refresh.

    today is the local service date as YYYY-MM-DD. Returns the file name.
    """
    schedule = data["schedule"]
    name = data.get("name") or ""
    first = datetime.date.fromisoformat(today)
    service_dates = [(first + datetime.timedelta(days=i)).isoformat() for i in range(TIMETABLE_DAYS)]
    yesterday = (first - datetime.timedelta(days=1)).isoformat()
    args = departure_query_args(data)
    rows, _origin = _fetch_departure_rows(
        data["route_type"], data["origin"], data["destination"], schedule,
        window=(yesterday, service_dates[-1]), limit=TIMETABLE_ROWS_MAX, **args)
    departure = data.get("next_departure") or {}
    zone = _leg_timezone(schedule, str(departure.get("route_id") or args["route"] or ""), departure, hass)
    # the first run past the window: the next day the entry runs at all,
    # then its first departure that day
    next_departure = None
    after = (first + datetime.timedelta(days=TIMETABLE_DAYS)).isoformat()
    day = get_next_service_date(
        schedule, id_of(data["origin"]), id_of(data["destination"]), after,
        data["route_type"], **args)
    if day:
        later, _origin = _fetch_departure_rows(
            data["route_type"], data["origin"], data["destination"], schedule,
            window=(day, day), limit=1, **args)
        if later:
            next_departure = _local(later[0].get("origin_depart_dt"), zone)
    doc = timetable_doc(name, rows, service_dates, zone, next_departure, last_service_day(zip_path))
    file = timetable_name(name)
    _LOGGER.debug("Creating timetable file: %s, %s departures", file, sum(len(d["departures"]) for d in doc["days"]))
    write_json_if_changed(map_file(hass, file), doc,
                          {k: v for k, v in doc.items() if k != "generated"})
    return file
