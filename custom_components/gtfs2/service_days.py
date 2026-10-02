"""The days a service runs: the next one a journey runs on
(get_next_service_date), and the SQL pieces that lay a stop time on its
service day and tell whether the calendar runs on a date (_on_service_day,
_runs_on, _removed_on).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

from .stop_rules import RAIL_ROUTE_TYPES_SQL, _alights, _boards, _place_group, station_names_in

if TYPE_CHECKING:
    # for the annotations only
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


# How far ahead get_next_service_date is allowed to look. A route that has not
# run for three months is not "resuming later", it is out of the feed, and an
# unbounded scan would walk the whole calendar to say so.
NEXT_SERVICE_HORIZON_DAYS = 90


def get_next_service_date(schedule: Schedule | str | None, origin_id: str, dest_id: str,
                          from_date: str, route_type: str = "3",
                          horizon: int = NEXT_SERVICE_HORIZON_DAYS, line: str | None = None,
                          origin_names: list[str] | None = None,
                          destination_names: list[str] | None = None, route: str | None = None,
                          direction: str | int | None = None) -> str | None:
    """Return the first date on or after from_date that this trip runs, or None.

    include_tomorrow only ever reaches J+1, so a line that rests over the
    weekend or a holiday leaves the sensor blank with nothing to show. This
    answers the question the user actually asks in that gap: not "is there a
    bus today", but "when is the next one".

    Both calendar shapes are read, because feeds use either: calendar holds
    weekday flags over a validity window, calendar_dates holds explicit
    additions and removals. TAO publishes everything through calendar_dates
    with every weekday flag at 0, so reading calendar alone would find nothing.

    Returns a plain 'YYYY-MM-DD' string, and None when no service is found
    within horizon: a route can legitimately have no trips left at all.

    For a train, origin_id and dest_id are station names; origin_names and
    destination_names, when given, are every station the entry ticked at each end,
    and line holds the answer to the line the flow picked, as the departures
    are held to it.

    route and direction hold the answer to the entry's line and, at a
    loop's terminus, its way round, as the departures are held to them:
    without them a day this line rests but another line serves the same
    two places read as a day it runs, and the sensor announced a service
    that does not exist.
    """
    # the coordinator calls this with whatever get_gtfs returned, which is a
    # sentinel string or None when the datasource is unusable. Matched by
    # shape, not by class: anything schedule-shaped may query
    if schedule is None or isinstance(schedule, str):
        _LOGGER.warning("No usable schedule to look up the next service date (%s)", schedule or "empty")
        return None
    line_join = line_where = ""
    params: dict[str, Any]
    if route_type == "2":
        # trains match on the exact stop_name, like get_next_departure does
        origin_in, params = station_names_in("origin", origin_names or [origin_id])
        dest_in, dest_params = station_names_in("dest", destination_names or [dest_id])
        params.update(dest_params)
        origin_where = ("o.stop_id in (select stop_id from stops "
                        f"where stop_name in {origin_in})")
        dest_where = ("x.stop_id in (select stop_id from stops "
                      f"where stop_name in {dest_in})")
        # held to rail, as the departures are: two stations of one name can
        # also be served by a bus the train sensor never lists
        line_join = "inner join routes r on r.route_id = t.route_id"
        line_where = f"and r.route_type in ({RAIL_ROUTE_TYPES_SQL})"
        if line:
            # without it, a day the line rests but another one serves the
            # same stations (P8 beside K8+) read as a day it runs
            line_where += " and r.route_short_name = :line"
            params["line"] = line
    else:
        # the whole place at each end, as the departures are matched
        origin_where = "o.stop_id in " + _place_group("origin")
        dest_where = "x.stop_id in " + _place_group("dest")
        params = {"origin": origin_id, "dest": dest_id}
        if route:
            line_where = "and t.route_id = :route"
            params["route"] = route
        if str(direction) in ("0", "1"):
            line_where += " and (t.direction_id = :direction or t.direction_id is null)"
            params["direction"] = int(str(direction))

    sql = f"""
        with recursive dates(d) as (
            select date(:from_date)
            union all
            select date(d, '+1 day') from dates
            where d < date(:from_date, :horizon)
        ),
        serving as (
            select distinct t.service_id
            from trips t
            inner join stop_times o on o.trip_id = t.trip_id
            inner join stop_times x on x.trip_id = t.trip_id
            {line_join}
            where {origin_where} and {dest_where}
              and o.stop_sequence < x.stop_sequence
              and {_boards("o")} and {_alights("x")}
              {line_where}
        )
        select min(dates.d) from dates
        where exists (
            select 1 from serving s
            inner join calendar cal on cal.service_id = s.service_id
            where cal.start_date <= dates.d and cal.end_date >= dates.d
              and {_runs_on("dates.d", "cal")}
              and not {_removed_on("s.service_id", "dates.d")})
        or exists (
            select 1 from serving s
            inner join calendar_dates cd on cd.service_id = s.service_id
            where cd.date = dates.d and cd.exception_type = 1)
    """  # noqa: S608

    try:
        with schedule.engine.connect() as conn:
            row = conn.execute(text(sql), {
                **params,
                "from_date": from_date,
                "horizon": f"+{int(horizon)} days",
            }).fetchone()
    except SQLAlchemyError as ex:
        # never let a lookup that only enriches an attribute break the update
        _LOGGER.warning("Could not determine next service date: %s", ex)
        return None

    result = row[0] if row else None
    _LOGGER.debug("Next service date for %s -> %s from %s: %s",
                  origin_id, dest_id, from_date, result)
    return str(result)[:10] if result else None


def _day_offset(time_column: str) -> str:
    """SQL: the whole days a stored stop time lies past its service day.

    Stop times are stored on 1970-01-01, and a time past midnight on the
    days after: a call at 25:10 is one day on, one past 48:00 two days on.
    """
    return f"CAST(julianday(date({time_column})) - julianday('1970-01-01') AS INTEGER)"


def _on_service_day(day: str, time_column: str) -> str:
    """SQL: a stored stop time laid on its service day, the days it lies
    past midnight included (see _day_offset)."""
    return (f"datetime({day} || ' ' || time({time_column}), "
            f"'+' || {_day_offset(time_column)} || ' days')")


def _runs_on(day: str, calendar: str = "") -> str:
    """SQL: the calendar row runs on the weekday of this date."""
    cal = f"{calendar}." if calendar else ""
    return (f"(case cast(strftime('%w', {day}) as int)"
            f" when 0 then {cal}sunday when 1 then {cal}monday"
            f" when 2 then {cal}tuesday when 3 then {cal}wednesday"
            f" when 4 then {cal}thursday when 5 then {cal}friday"
            f" else {cal}saturday end) = 1")


def _removed_on(service: str, day: str) -> str:
    """SQL: calendar_dates takes this service out on this date."""
    return (f"exists (select 1 from calendar_dates removed"
            f" where removed.service_id = {service}"
            f" and removed.date = {day} and removed.exception_type = 2)")
