"""The clocks of a feed: a stop time as the seconds past its service day's
midnight (gtfs_seconds), and the time zone the feed, or one of its
agencies, writes its times in (zone_of, agency_zone).
"""
from __future__ import annotations

import datetime
import logging
import re
from typing import TYPE_CHECKING

import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import text

if TYPE_CHECKING:
    # for the annotations only
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
