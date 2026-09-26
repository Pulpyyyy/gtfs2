"""Departures on the days the clocks change.

Twice a year a network's day is 23 or 25 hours long: an hour of wall clock
is skipped in spring and one is lived twice in autumn. The sensor lays the
feed's clocks on the day in the agency's zone and compares them with now,
so those two days are where an hour of departures gets lost, doubled or
shown in the wrong order. The fixtures cover four zones and both
hemispheres: America/Los_Angeles (bart), Europe/Paris and Europe/Amsterdam
(sncf-journeys, gvb, boarding), Australia/Adelaide (adelaide).

The promise, for every line of a fixture and every day its calendar runs it
on which the agency's clocks change: from each ride's first stop a rider can
board at to its last stop a rider can leave at, asked at 00:05, at 01:30
(an hour lived twice in autumn), at 02:30 (an hour skipped in spring), at
03:30 and at 12:00 of that day, the departure get_next_departure shows, and
the first it lists, is the next ride the feed has (the same reading as
test_journeys: call by call from stop_times and the calendar, the
component's query set aside), and the departures it lists run forward in
time, each once.

The feed's clocks are read as wall clock times of the service day, the way
the sensor and nearly every agency read them. GTFS measures them from noon
minus 12 hours, which differs by an hour before 02:00 on these two days.
Kept on purpose (user decision, 2026-09-26): the one feed of the 48-feed
sweep where it shows is Auckland, whose TMK trips at 25:00, 26:00 and
27:00 on the eve of spring forward read 01:00, 02:00 (a time that does not
exist, laid on 03:00) and 03:00.

    pytest tests_provider/test_summer_time.py
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import zipfile
import zoneinfo

import pytest
from freezegun import freeze_time

import test_journeys as tj

UTC = datetime.timezone.utc
CLOCKS = (datetime.time(0, 5), datetime.time(1, 30), datetime.time(2, 30),
          datetime.time(3, 30), datetime.time(12, 0))
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _rows(archive, name):
    if name not in archive.namelist():
        return []
    with archive.open(name) as raw:
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))]


def _changes(zone, first, last):
    """The days between first and last on which the zone's clocks change."""
    day, found = first, []
    while day <= last:
        start = datetime.datetime.combine(day, datetime.time(), zone)
        end = datetime.datetime.combine(day, datetime.time(23, 59), zone)
        if start.utcoffset() != end.utcoffset():
            found.append(day)
        day += datetime.timedelta(days=1)
    return found


def _runs(calendar, dates, service, day):
    """Whether the zip's calendar runs the service on that day."""
    ymd = day.strftime("%Y%m%d")
    kind = dates.get((service, ymd))
    if kind:
        return kind == "1"
    row = calendar.get(service)
    return bool(row and row["start_date"] <= ymd <= row["end_date"]
                and row[WEEKDAYS[day.weekday()]] == "1")


def _cases():
    """(fixture, route_id, day) for every line of a fixture its calendar
    runs on a day the agency's clocks change. Read off the zip: the db is
    built by the first case that asks for it."""
    cases = []
    for path in sorted(tj.FIXTURES.iterdir()):
        if not (path / "manifest.json").is_file():
            continue
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if not manifest.get("static_only") or not manifest.get("routes_kept"):
            continue
        with zipfile.ZipFile(path / "static.zip") as archive:
            zone_name = next((row["agency_timezone"] for row in _rows(archive, "agency.txt")
                              if row.get("agency_timezone")), "UTC")
            calendar = {row["service_id"]: row for row in _rows(archive, "calendar.txt")}
            dates = {(row["service_id"], row["date"]): row["exception_type"]
                     for row in _rows(archive, "calendar_dates.txt")}
            trains = {row["route_id"] for row in _rows(archive, "routes.txt")
                      if row.get("route_type") == "2"}
            services = {}
            for trip in _rows(archive, "trips.txt"):
                services.setdefault(trip["route_id"], set()).add(trip["service_id"])
        spans = [(row["start_date"], row["end_date"]) for row in calendar.values()]
        spans += [(ymd, ymd) for _service, ymd in dates]
        if not spans:
            continue
        first = datetime.datetime.strptime(min(s for s, _e in spans), "%Y%m%d").date()
        last = datetime.datetime.strptime(max(e for _s, e in spans), "%Y%m%d").date()
        changes = _changes(zoneinfo.ZoneInfo(zone_name), first, last)
        for label, ids in sorted(manifest["routes_kept"].items()):
            for route_id in ([ids] if isinstance(ids, str) else ids):
                # a train is asked by station name on a path of its own,
                # which test_journeys covers; the lines here go by stop
                if route_id in trains:
                    continue
                for day in changes:
                    if any(_runs(calendar, dates, s, day) for s in services.get(route_id, ())):
                        cases.append(pytest.param(
                            path.name, route_id, day.isoformat(),
                            id=f"{path.name}-{label}-{day.isoformat()}"))
    return cases


@pytest.mark.parametrize(("fixture", "route_id", "day"), _cases())
def test_the_next_ride_holds_on_the_day_the_clocks_change(record_property, fixture,
                                                          route_id, day):
    fx = tj.fixture_of(fixture)
    zone = zoneinfo.ZoneInfo(fx.agency_tz)
    tj.dt_util.set_default_time_zone(tj.dt_util.get_time_zone(fx.agency_tz))
    schedule = fx.schedule
    route_type = str(fx.route_types[route_id])
    entries, ids, entry_of = tj.line_places(fx, route_id)
    running = tj.services_on(schedule, day)
    with schedule.engine.connect() as conn:
        service_of = dict(conn.execute(tj.text(
            "SELECT trip_id, service_id FROM trips WHERE route_id = :r"), {"r": route_id}).fetchall())
    check = tj.Check()
    hass = fx.hass()
    for pattern, trip_ids in sorted(tj.line_patterns(schedule, route_id).items()):
        if not any(service_of.get(t) in running for t in trip_ids):
            continue
        ends = fx.rider_ends(pattern, trip_ids)
        if ends is None or any(stop not in entry_of for stop in pattern):
            check.note(True, f"the ride {pattern[0]} .. {pattern[-1]} offers no journey "
                       f"a rider can ask", no_journey=True)
            continue
        origin, destination = ids[entry_of[pattern[ends[0]]]], ids[entry_of[pattern[ends[1]]]]
        if origin == destination:
            check.note(True, f"the ride {pattern[0]} .. {pattern[-1]} ends where it starts",
                       same_place=True)
            continue
        kept = tj.gtfs_helper.get_pair_direction(schedule, route_id, origin, destination)
        data = tj._data_for(schedule, route_id, route_type, entries, entry_of,
                            pattern[ends[0]], pattern[ends[1]], kept)
        origins, reached = fx.siblings_of(origin), fx.siblings_of(destination)
        for at in CLOCKS:
            now = datetime.datetime.combine(datetime.date.fromisoformat(day), at, zone)
            with freeze_time(now.astimezone(UTC)):
                result = tj.get_next_departure(hass, data)
            expected = fx.next_ride(route_id, kept, origins, reached, now)
            shown = (result or {}).get("departure_time")
            shown = shown.astimezone(zone) if isinstance(shown, datetime.datetime) else None
            listed = [datetime.datetime.fromisoformat(iso)
                      for iso in (result or {}).get("next_departures") or []]
            first = listed[0].astimezone(zone) if listed else None
            if expected is None:
                ok = shown is None and first is None
            else:
                ok = all(got is not None and abs(got.timestamp() - expected[0].timestamp()) < 60
                         for got in (shown, first))
            # each once: a trip listed once. Two trips leaving at one instant
            # are two departures; read the wall clock way, Auckland's 26:00
            # and 27:00 on its spring-forward eve are two such (see above)
            trips = (result or {}).get("next_departures_trip_id") or []
            forward = (all(a <= b for a, b in zip(listed, listed[1:]))
                       and len(set(zip(listed, trips))) == len(listed)
                       if len(trips) == len(listed) else
                       all(a < b for a, b in zip(listed, listed[1:])))
            asked = {"origin": origin, "destination": destination, "route": route_id,
                     "day": day, "at": f"{at:%H:%M}", "utc": f"{now.astimezone(UTC):%H:%M}"}
            check.note(ok, (
                f"at {at:%H:%M} ({now:%Z}) on {day}, {origin} -> {destination} on "
                f"{route_id}: the feed's next ride "
                + (f"leaves {expected[0]:%m-%d %H:%M %Z} (trip {expected[1]})" if expected
                   else "is none left in the calendar")
                + ", got " + (f"{shown:%m-%d %H:%M %Z}" if shown else "nothing")
                + ", listed first " + (f"{first:%m-%d %H:%M %Z}" if first else "nothing")),
                asked=asked, next_expected=(expected[0].isoformat() if expected else None),
                next_got=shown.isoformat() if shown else None)
            check.note(forward, f"at {at:%H:%M} on {day}, {origin} -> {destination}: "
                       f"the {len(listed)} departures listed run forward in time, each once",
                       asked=asked, listed=[t.isoformat() for t in listed])
    record_property("case", {"fixture": fixture, "route": route_id, "day": day})
    record_property("checks", check.records)
    assert check.records, "no ride of the line runs that day"
    assert not check.failures, "\n".join(check.failures)
