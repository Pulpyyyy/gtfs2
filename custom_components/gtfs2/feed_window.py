"""How long the kept timetable is good for.

A feed says so twice, and neither way is reliable alone. feed_info.txt
carries feed_start_date and feed_end_date, when the feed ships one (Zou
ships none) and when the publisher keeps them honest (the TAO writes an
end three months out and the Dutch feed three months, whatever the
calendar holds). The calendar tables say it for real: the last day any
service runs, from the calendar windows that have a weekday on and the
calendar_dates additions. Past that day the sensors show nothing, and
the reason is not a broken install but a timetable that ran out, which
this is here to say. Measured on the 2026-09-15 editions: the TAO's last
service day is its feed_end_date, the SNCF's runs to 2026-12-12 against
a feed_end_date of 2027-02-28, Zou runs to 2026-12-31 with no feed_info
at all.

Read from the zip kept beside the database, never from the database:
the datasource only holds the routes the entries need, and a prune may
have emptied its calendars. The members read are small (feed_info is one
row, calendar_dates 1.5 MB on the Dutch national feed), so this costs
well under a second even there.
"""
from __future__ import annotations

import csv
import datetime
import io
import logging
import zipfile

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# how close to the end the timetable reads as ending rather than valid
ENDING_DAYS = 7


def _iso(value):
    """A GTFS date, YYYYMMDD, as YYYY-MM-DD; None for anything else."""
    value = (value or "").strip()
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return value
    return None


def read_feed_window(zip_path):
    """What the zip says of its validity, as ISO dates, {} without a zip.

    feed_publisher_name, feed_version, feed_start_date and feed_end_date
    come from feed_info.txt, None each when the feed leaves them out.
    first_service_day and last_service_day come from the calendars: the
    windows of calendar.txt whose weekdays are not all off, and the
    additions of calendar_dates.txt. A removal narrows nothing, it only
    takes one day out of a window.
    """
    window = {
        "feed_publisher_name": None, "feed_version": None,
        "feed_start_date": None, "feed_end_date": None,
        "first_service_day": None, "last_service_day": None,
    }
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as ex:
        _LOGGER.debug("No zip to read a feed window from at %s: %s", zip_path, ex)
        return {}
    with archive:
        names = {name.split("/")[-1]: name for name in archive.namelist()}

        def rows(member):
            with archive.open(names[member]) as raw:
                yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))

        try:
            if "feed_info.txt" in names:
                for row in rows("feed_info.txt"):
                    window["feed_publisher_name"] = (row.get("feed_publisher_name") or "").strip() or None
                    window["feed_version"] = (row.get("feed_version") or "").strip() or None
                    window["feed_start_date"] = _iso(row.get("feed_start_date"))
                    window["feed_end_date"] = _iso(row.get("feed_end_date"))
                    break
            first = last = None
            if "calendar.txt" in names:
                for row in rows("calendar.txt"):
                    if not any((row.get(day) or "").strip() == "1" for day in WEEKDAYS):
                        # the TAO shape: every flag off, the dates mean nothing
                        continue
                    start, end = _iso(row.get("start_date")), _iso(row.get("end_date"))
                    if start:
                        first = min(first, start) if first else start
                    if end:
                        last = max(last, end) if last else end
            if "calendar_dates.txt" in names:
                for row in rows("calendar_dates.txt"):
                    if (row.get("exception_type") or "").strip() != "1":
                        continue
                    day = _iso(row.get("date"))
                    if day:
                        first = min(first, day) if first else day
                        last = max(last, day) if last else day
            window["first_service_day"] = first
            window["last_service_day"] = last
        except (KeyError, OSError, UnicodeDecodeError, csv.Error) as ex:
            _LOGGER.warning("Could not read the feed window of %s: %s", zip_path, ex)
    return window


def timetable_state(window, today):
    """(state, days_left) of a timetable on a given day.

    valid while the last service day is more than ENDING_DAYS away,
    ending within them, expired once it is past, unknown when the zip
    says nothing. days_left counts today as a day left (0 on the last
    day, negative past it).
    """
    last = (window or {}).get("last_service_day")
    if not last:
        return "unknown", None
    try:
        last_day = datetime.date.fromisoformat(last)
    except ValueError:
        return "unknown", None
    days_left = (last_day - today).days
    if days_left < 0:
        return "expired", days_left
    if days_left <= ENDING_DAYS:
        return "ending", days_left
    return "valid", days_left
