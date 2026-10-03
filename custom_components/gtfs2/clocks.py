"""The clocks of a feed: a stop time as the seconds past its service day's
midnight (gtfs_seconds), and the time zone the feed, or one of its
agencies, writes its times in (zone_of, agency_zone, and _leg_timezone
for a line's clocks with its fallbacks), and the SQL pieces that lay a
stop time on its service day and tell whether the calendar runs on a
date (_day_offset, _on_service_day, _runs_on, _removed_on).
"""
from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

if TYPE_CHECKING:
    # for the annotations only
    from homeassistant.core import HomeAssistant
    from pygtfs import Schedule

_LOGGER = logging.getLogger(__name__)


def zone_of(*names: str | None) -> datetime.tzinfo | None:
    """The zone of the first name given that is not empty: the agency's
    before a stop's, each caller keeping its own fallback; None when none
    is given or Home Assistant does not know it."""
    name = next((name for name in names if name), None)
    return dt_util.get_time_zone(name) if name else None


def agency_zone(schedule: Schedule, route: str | None = None) -> datetime.tzinfo | None:
    """The time zone the feed writes its clocks in: the route's agency's,
    else the first agency that names one; None when the feed names none or
    cannot be read."""
    name = None
    try:
        with schedule.engine.connect() as conn:
            row = None
            if route:
                row = conn.execute(text(
                    "SELECT agency.agency_timezone FROM routes "
                    "JOIN agency ON agency.agency_id = routes.agency_id "
                    "WHERE routes.route_id = :route"), {"route": route}).fetchone()
            if not row or not row[0]:
                row = conn.execute(text(
                    "SELECT agency_timezone FROM agency "
                    "WHERE agency_timezone IS NOT NULL AND agency_timezone <> '' "
                    "LIMIT 1")).fetchone()
            name = row[0] if row else None
    except SQLAlchemyError as ex:
        _LOGGER.debug("Could not read the agency's zone, using Home Assistant's: %s", ex)
    return zone_of(name)


def _leg_timezone(schedule: Schedule, route_id: str | None, departure: Mapping[str, Any],
                  hass: HomeAssistant) -> datetime.tzinfo:
    """The zone the line's clocks are written in: the agency's, as the
    departure query reads it, else the origin stop's, else Home Assistant's."""
    zone = agency_zone(schedule, route_id)
    if zone is not None:
        return zone
    return zone_of(departure.get("origin_stop_timezone"), hass.config.time_zone) or datetime.timezone.utc


def gtfs_seconds(value: object) -> int | None:
    """Seconds since the service day's midnight of a stop time, or None.

    The one reader of stop times for every module: the queries hand them
    back as pygtfs stores them, a datetime counted from 1970-01-01 (a 01:15
    departure after midnight reads '1970-01-02 01:15:00'), a database from
    another pygtfs build as bare text ('25:15:00'), a caller may already
    hold seconds or a timedelta. Three readers each took some of these
    forms and refused, or raised on, the others.
    """
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return int(value.total_seconds())
    if isinstance(value, (int, float)):
        return int(value)
    text_value = str(value).strip()
    if text_value.isdigit():
        return int(text_value)
    days = 0
    stored = re.match(r"^1970-01-(\d{2})[ T](.*)$", text_value)
    if stored:
        days = int(stored.group(1)) - 1
        text_value = stored.group(2)
    parts = text_value.split(".")[0].split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


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
