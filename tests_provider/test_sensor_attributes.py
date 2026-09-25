"""What the sensors show, read off the real sensor.py, against a reviewed file.

Every entity of sensor.py is built here on the data its coordinator hands
it, and what Home Assistant would read off it (state, device class, icon,
attribution, name, device, attributes) is written down in
expected/sensor_attributes.json, one entry per variant, keys sorted. That
file is the review surface: a change that moves an attribute fails here
with the variant, the key and both values, and is either a bug or a change
someone reads and approves by regenerating the file on purpose:

    GTFS2_UPDATE_EXPECTED=1 pytest tests_provider/test_sensor_attributes.py
    git diff tests_provider/expected/

The variants, by the sensor they build:

    departure   the journey sensor on made-up coordinator data (a line
                resting for days or done for today, no service at all,
                departures with realtime, alerts and map files, alerts at
                both ends, an offset, a first
                bus tomorrow or in four days, a source still unpacking, a
                train, a realtime reading with no scheduled departure, a
                realtime reading that lists nothing, a schedule that is a
                sentinel, stops the timetable no longer has, an agency the
                route names and the feed lacks), and on the data the
                coordinator's own steps build from the fixtures/boarding
                line (get_next_departure, next_service_date_for,
                departure_records) under a frozen clock in Europe/Paris
    restore     the journey sensor added before its first refresh: empty
                and named from its entry, then showing what Home Assistant
                kept of it only while the departure it names is still ahead
    local stop  the stops-around-a-person sensor, on the departures
                get_local_stops_next_departures lists at the boarding
                line's Gare, while the source unpacks, and once the stop has
                left the list
    source      the datasource's diagnostic sensors: realtime off, switched
                off, not gated yet, paused and active; the timetable read
                from a kept zip, and from no zip

The promises beside the file:

    each variant renders exactly as the file says, and the file holds no
    variant the test does not render
    what Home Assistant reads as the attributes is the dict the sensor
    wrote, never a stale copy of it
    an update renders the same as a fresh sensor on the same data, from
    whatever the sensor showed before: nothing of the last departure
    lingers, and the update writes the state once. One thing does linger:
    a sensor that clears itself keeps the agency of its last departure as
    its attribution. That is kept on record as an expected failure, strict,
    so the day it is fixed the test says so
    get_next_departure answers {} when there is no departure, and the
    sensor on {} shows no departure field at all, only when the line runs
    next
    every list or mapping the sensors write is kept out of the recorder,
    and the recorded and unrecorded attribute names are written down in
    the file, so a new attribute is seen landing on one side or the other
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import os
import types
import zoneinfo
from pathlib import Path

import pytest
from freezegun import freeze_time

import ha_stub

ha_stub.install()

import homeassistant.util.dt as dt_util  # noqa: E402

import fixture_db  # noqa: E402

sensor = ha_stub.load("sensor")
const = ha_stub.load("const")
gtfs_helper = ha_stub.load("gtfs_helper")
departure_attributes = ha_stub.load("departure_attributes")
refresh_steps = ha_stub.load("refresh_steps")
rt_window = ha_stub.load("rt_window")

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
EXPECTED = HERE / "expected" / "sensor_attributes.json"
UPDATE = os.environ.get("GTFS2_UPDATE_EXPECTED") == "1"
REGENERATE = ("GTFS2_UPDATE_EXPECTED=1 pytest tests_provider/test_sensor_attributes.py, "
              "then read git diff tests_provider/expected/")

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
UTC = datetime.timezone.utc
# a Wednesday, the made-up departures an hour and more ahead of it
NOW = datetime.datetime(2026, 9, 16, 10, 0, tzinfo=PARIS)
# the boarding line runs T1 08:00, T2 09:00, T3 10:00 from Gare every day
# of 2026 and 2027 but Christmas Day
EARLY = datetime.datetime(2026, 9, 16, 7, 30, tzinfo=PARIS)
LATE = datetime.datetime(2026, 9, 16, 10, 30, tzinfo=PARIS)
CHRISTMAS_EVE = datetime.datetime(2026, 12, 24, 20, 0, tzinfo=PARIS)


# --- made-up coordinator data ------------------------------------------------

def table(**fields):
    """A pygtfs-like row: fields as attributes, __table__.columns naming them."""
    row = types.SimpleNamespace(**fields)
    row.__table__ = types.SimpleNamespace(columns=[types.SimpleNamespace(name=k) for k in fields])
    return row


class Schedule:
    """The four lookups departure_records makes, on one line of two stops."""

    def stops_by_id(self, stop_id):
        if stop_id in ("S1", "S1b"):
            return [table(stop_id=stop_id, stop_name="Stop One", location_type=0, wheelchair_boarding=1)]
        if stop_id == "S2":
            return [table(stop_id="S2", stop_name="Stop Two", location_type=1, wheelchair_boarding=2)]
        return []

    def routes_by_id(self, route_id):
        if route_id == "R1":
            return [table(route_id="R1", route_short_name="1", route_long_name="A > B", route_type=3,
                          route_color="FF0000", agency_id="AG")]
        if route_id == "R2":
            # a route whose agency the feed does not carry
            return [table(route_id="R2", route_short_name="2", route_long_name="C > D", route_type=0,
                          route_color="", agency_id="GONE")]
        return []

    def trips_by_id(self, trip_id):
        if trip_id in ("T1", "T2"):
            return [table(trip_id=trip_id, route_id="R1", trip_headsign="B", bikes_allowed=1,
                          wheelchair_accessible=2, direction_id=0)]
        return []

    def agencies_by_id(self, agency_id):
        if agency_id == "AG":
            return [table(agency_id="AG", agency_name="Agency", agency_timezone="Europe/Paris")]
        return []


def stop_time(seq):
    return {"Arrival Time": "10:30:00", "Departure Time": "10:30:00", "Drop Off Type": 0,
            "Pickup Type": 0, "Timepoint": 1, "Stop Sequence": seq}


def departure(at, listed=3):
    """What get_next_departure answers for a bus leaving at `at`, every
    tenth minute after it listed too."""
    dep = at.astimezone(UTC)
    return {
        "trip_id": "T1", "route_id": "R1", "origin_stop_id": "S1b", "destination_stop_id": "S2",
        "departure_time": dep, "arrival_time": dep + datetime.timedelta(minutes=25), "duration": 25,
        "day": "today", "first": None, "last": False, "trip_direction_id": "0",
        "origin_stop_time": stop_time(1), "destination_stop_time": stop_time(7),
        "origin_stop_timezone": "Europe/Paris", "destination_stop_timezone": "Europe/Paris",
        "next_departures": [(dep + datetime.timedelta(minutes=10 * i)).isoformat() for i in range(listed)],
        "next_departures_lines": ["1"] * listed, "next_departures_headsign": ["B"] * listed,
        "next_departures_trip_id": ["T1", "T2", "T1"][:listed],
        "next_departures_destination_arrival_times": [
            (dep + datetime.timedelta(minutes=25 + 10 * i)).isoformat() for i in range(listed)],
        "next_departures_durations": [25] * listed,
        "next_departures_origin_stop_id": ["S1b", "S1", "S1b"][:listed],
        "next_departures_route_types": [3, 3, 714][:listed],
    }


def realtime():
    return {
        const.ATTR_RT_UPDATED_AT: "2026-09-16T07:59:00+00:00",
        const.ATTR_NEXT_RT: ["2026-09-16T08:02:00+00:00", "2026-09-16T08:11:00+00:00"],
        const.ATTR_NEXT_RT_DELAYS: [120, 60],
        const.ATTR_NEXT_RT_TRIPS: ["T1", "T2"],
        const.ATTR_RT_CANCELLED: {"T9": "20260916"},
        const.ATTR_RT_SKIPPED: {},
    }


def _with_records(data):
    """The coordinator's last step: the rows the sensor describes with."""
    return {**data, "records": departure_attributes.departure_records(data["schedule"], data)}


def made_up():
    """label -> coordinator data, as the coordinator would hand it at NOW."""
    base = {
        "name": "one to two", "schedule": Schedule(), "extracting": False, "file": "feed",
        "origin": "S1: Stop One", "destination": "S2: Stop Two", "offset": 0,
        "route_type": "3", "route": "R1: Line 1", "direction": "0",
        "next_departure": {}, "next_departure_realtime_attr": None,
        "alert": {}, "gtfs_updated_at": "2026-09-16T07:50:00+00:00",
    }
    today = NOW.date()
    out = {
        "resting, next in 3 days": {**base, "next_service_date": (today + datetime.timedelta(days=3)).isoformat()},
        "resting, next today but done": {**base, "next_service_date": today.isoformat()},
        "no service at all": {**base, "next_service_date": None},
        "today, realtime, alerts, files": {
            **base, "next_departure": departure(NOW + datetime.timedelta(hours=1)),
            "next_departure_realtime_attr": realtime(),
            "alert": {"origin_stop_alert": "Works", "destination_stop_alert": "no info",
                      "origin_stop_alerts": [{"text": "Works", "cause": "CONSTRUCTION", "effect": "DETOUR"}],
                      "destination_stop_alerts": None, "alert_cause": "CONSTRUCTION", "alert_effect": "DETOUR"},
            "route_geojson_file": "R1_0_route.json", "leg_geojson_file": "R1_0_leg_one_to_two.json",
            "vehicle_positions_file": "R1_0.json", "timetable_file": "R1_0_timetable_one_to_two.json",
        },
        "tomorrow morning": {**base, "next_departure": departure(
            datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time(8, 0), PARIS), listed=1)},
        "in four days": {**base, "next_departure": departure(
            datetime.datetime.combine(today + datetime.timedelta(days=4), datetime.time(8, 0), PARIS), listed=1)},
        "extracting": {**base, "extracting": True},
        # the coordinator reads realtime on the entry's fallbacks when the
        # timetable has nothing left: the late last bus is still coming
        "realtime, no scheduled departure left": {
            **base, "next_service_date": (today + datetime.timedelta(days=1)).isoformat(),
            "next_departure_realtime_attr": realtime()},
        # a feed read that lists nothing for this stop
        "realtime read, nothing listed": {
            **base, "next_departure": departure(NOW + datetime.timedelta(hours=1)),
            "next_departure_realtime_attr": {const.ATTR_RT_UPDATED_AT: "2026-09-16T07:59:00+00:00"}},
        "alerts at both ends": {
            **base, "next_departure": departure(NOW + datetime.timedelta(hours=1), listed=1),
            "alert": {"origin_stop_alert": "Strike", "destination_stop_alert": "Stop closed",
                      "origin_stop_alerts": [{"text": "Strike", "cause": "STRIKE", "effect": "REDUCED_SERVICE"}],
                      "destination_stop_alerts": [{"text": "Stop closed", "effect": "STOP_MOVED",
                                                   "stops": ["Stop Two"]}],
                      "alert_cause": "STRIKE", "alert_effect": "REDUCED_SERVICE"}},
        "schedule is a sentinel": {**base, "schedule": "no_zip_file"},
        "origin stop gone from the timetable": {**base, "origin": "S9: Nowhere"},
        "destination stop gone from the timetable": {**base, "destination": "S9: Nowhere"},
        "agency missing from the feed": {**base, "route": "R2: Line 2", "next_service_date": None},
        # the arrival is the departure's own clock: an offset only moves
        # what counts as today
        "offset of 90 minutes, first bus tomorrow": {
            **base, "offset": 90, "next_departure": departure(
                datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time(8, 0), PARIS),
                listed=1)},
    }
    train = departure(NOW + datetime.timedelta(hours=2))
    train.update({"origin_stop_name": "Gare A", "origin_stop_sequence": 3, "destination_stop_name": "Gare B"})
    out["train, no realtime"] = {**base, "route_type": "2", "origin": "Gare A", "destination": "Gare B",
                                 "next_departure": train}
    out["train, no departure left"] = {**base, "route_type": "2", "origin": "Gare A", "destination": "Gare B",
                                       "next_service_date": None}
    return {label: _with_records(data) for label, data in out.items()}


# --- coordinator data built from the boarding line ----------------------------

def _hass(root=".", where=None):
    async def run_in_executor(fn, *args):
        return fn(*args)
    return types.SimpleNamespace(
        config=types.SimpleNamespace(path=lambda *parts: str(Path(root, *parts)),
                                     time_zone="Europe/Paris"),
        states=types.SimpleNamespace(get=lambda _entity: types.SimpleNamespace(
            attributes={"latitude": where[0], "longitude": where[1]} if where else {})),
        async_add_executor_job=run_in_executor)


def _boarding_entry(schedule, origin, destination):
    """What the coordinator puts in its data before the refresh reads it."""
    names = {"A": "Gare", "B": "Mairie", "C": "Ecole"}
    return {"schedule": schedule, "origin": f"{origin}: {names[origin]} (1)",
            "destination": f"{destination}: {names[destination]} (3)",
            "offset": 0, "gtfs_dir": ".", "name": f"B1 {names[origin]} {names[destination]}",
            "file": "fixture", "route_type": "3", "route": "B1: B1", "loop_direction": None,
            "line": None, "extracting": False, "next_departure": {},
            "next_departure_realtime_attr": {}, "alert": {}}


def _refreshed(schedule, origin, destination):
    """The coordinator's static refresh on the boarding line, step by step,
    at the frozen clock: the departure, its stamp, the next service day
    when there is none, the records."""
    data = _boarding_entry(schedule, origin, destination)
    hass = _hass()
    data["next_departure"] = gtfs_helper.get_next_departure(hass, data)
    data["gtfs_updated_at"] = dt_util.utcnow().isoformat()
    if not data["next_departure"]:
        data["next_service_date"] = asyncio.run(refresh_steps.next_service_date_for(
            hass, schedule, data, data.get("offset", 0)))
    return _with_records(data)


BOARDING = {  # label: (clock, origin, destination)
    "boarding Gare to Ecole, first bus ahead": (EARLY, "A", "C"),
    "boarding Gare to Ecole, last bus gone": (LATE, "A", "C"),
    "boarding Mairie to Ecole, nobody gets on": (EARLY, "B", "C"),
    "boarding Gare to Ecole, Christmas Eve evening": (CHRISTMAS_EVE, "A", "C"),
}


# --- the variants ----------------------------------------------------------------

class Variant(types.SimpleNamespace):
    """kind, now, and what the sensor is built on: data for a departure or
    local stop sensor, last (a restored state) for restore, entry for a
    source sensor."""


def _variants(bus):
    variants = {}
    for label, data in made_up().items():
        variants[f"departure / {label}"] = Variant(kind="departure", now=NOW, data=data)
    for label, (now, origin, destination) in BOARDING.items():
        with freeze_time(now.astimezone(UTC)):
            dt_util.set_default_time_zone(PARIS)
            variants[f"departure / {label}"] = Variant(
                kind="departure", now=now, data=_refreshed(bus, origin, destination))

    variants["restore / before its first refresh"] = Variant(kind="restore", now=NOW, data=None, last=None)
    kept = {"friendly_name": "x", "icon": "mdi:bus", "device_class": "timestamp",
            "attribution": "Agency", "next_departures": ["a"]}
    for label, state, data in (
            ("restored, departure ahead", "2026-09-16T08:30:00+00:00", None),
            ("not restored, departure gone", "2026-09-16T07:30:00+00:00", None),
            ("not restored, state was unknown", "unknown", None),
            ("not restored, first refresh already done", "2026-09-16T08:30:00+00:00",
             made_up()["no service at all"])):
        variants[f"restore / {label}"] = Variant(
            kind="restore", now=NOW, data=data,
            last=types.SimpleNamespace(state=state, attributes=dict(kept)))
    variants["restore / nothing kept"] = Variant(kind="restore", now=NOW, data=None, last=None, added=True)

    with freeze_time(EARLY.astimezone(UTC)):
        dt_util.set_default_time_zone(PARIS)
        local = {"schedule": bus, "gtfs_dir": ".", "name": "around me", "file": "fixture",
                 "offset": 0, "timerange": 60, "timerange_history": 15, "radius": 100,
                 "device_tracker_id": "person.rider", "extracting": False,
                 "gtfs_updated_at": dt_util.utcnow().isoformat()}
        me = types.SimpleNamespace(hass=_hass(where=(45.000, 5.000)), _realtime=False, _data=local)
        local["local_stops_next_departures"] = gtfs_helper.get_local_stops_next_departures(me)
    gare = next(s for s in local["local_stops_next_departures"] if s["stop_id"] == "A")
    variants["local stop / Gare, the next hour"] = Variant(kind="local", now=EARLY, stop=gare, data=local)
    variants["local stop / Gare, source extracting"] = Variant(
        kind="local", now=EARLY, stop=gare, data={**local, "extracting": True})
    variants["local stop / Gare, gone from the list"] = Variant(
        kind="local", now=EARLY, stop=gare, data={**local, "local_stops_next_departures": []})

    url = {const.CONF_TRIP_UPDATE_URL: "https://example.invalid/tu"}
    for label, options, state in (
            ("realtime, no feed", {}, None),
            ("realtime, switched off", {**url, const.CONF_RT_ENABLED: False}, None),
            ("realtime, not gated yet", url, None),
            ("realtime, paused", url, {"paused": "out_of_window", "window_start": None, "window_end": None,
                                       "extended_until": "2026-09-16T01:10:00+02:00",
                                       "checked_at": "2026-09-16T10:00:00+02:00"}),
            ("realtime, active", url, {"paused": None, "window_start": "2026-09-16T05:30:00+02:00",
                                       "window_end": "2026-09-17T00:40:00+02:00",
                                       "checked_at": "2026-09-16T10:00:00+02:00"})):
        variants[f"source / {label}"] = Variant(kind="source_rt", now=NOW, options=options, state=state)
    variants["source / timetable, Palm Bus zip"] = Variant(
        kind="source_timetable", now=NOW, root=FIXTURES / "palmbus", file="static")
    variants["source / timetable, no zip"] = Variant(
        kind="source_timetable", now=NOW, root=FIXTURES / "palmbus", file="gone")
    return variants


@contextlib.contextmanager
def _clock(now):
    """The sensor's clock pinned at `now`, read in Europe/Paris. The sensor
    reads only dt_util, so the stub's own clock does, and at a fraction of
    freezegun's cost: the updates below build some seven hundred sensors."""
    ha_stub.freeze(now.timestamp())
    dt_util.set_default_time_zone(PARIS)
    try:
        yield
    finally:
        ha_stub.freeze(None)


def _coordinator(data, name):
    return types.SimpleNamespace(data=data, config_entry=types.SimpleNamespace(data={"name": name}))


def _build(variant):
    """The entity of a variant, built under its clock in Europe/Paris."""
    with _clock(variant.now):
        if variant.kind == "departure":
            return sensor.GTFSDepartureSensor(_coordinator(variant.data, variant.data["name"]))
        if variant.kind == "restore":
            entity = sensor.GTFSDepartureSensor(_coordinator(variant.data, "restored"))
            if variant.last is not None or getattr(variant, "added", False):
                async def get_last_state(last=variant.last):
                    return last
                entity.async_get_last_state = get_last_state
                asyncio.run(entity.async_added_to_hass())
            return entity
        if variant.kind == "local":
            coordinator = _coordinator(variant.data, variant.data["name"])
            return sensor.GTFSLocalStopSensor(variant.stop, coordinator, variant.data["name"])
        entry = types.SimpleNamespace(
            data={const.CONF_KIND: const.ENTRY_KIND_DATASOURCE, const.CONF_FILE: getattr(variant, "file", "fixture")},
            options=getattr(variant, "options", {}))
        if variant.kind == "source_rt":
            return sensor.GTFSDatasourceRTSensor(entry)
        hass = _hass()
        # the sources' folder is the fixture's own: its static.zip is the kept zip
        hass.config.path = lambda *parts: str(variant.root)
        entity = sensor.GTFSDatasourceTimetableSensor(hass, entry)
        asyncio.run(entity.async_load_window())
        return entity


def _plain(value):
    """A value as the file writes it: JSON, a datetime or date tagged with
    its type, since a timestamp sensor's state has to be one."""
    if isinstance(value, datetime.datetime):
        return f"datetime {value.isoformat()}"
    if isinstance(value, datetime.date):
        return f"date {value.isoformat()}"
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=json.dumps)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return f"{type(value).__name__} {value!r}"


def _render(entity, variant):
    """What Home Assistant reads off the entity, as plain JSON."""
    with _clock(variant.now):
        # what the realtime gate last decided, as rt_window keeps it; no
        # other test's gate is left in there to answer instead
        gated = variant.kind == "source_rt"
        if gated:
            rt_window._STATE.pop(variant_file(variant), None)
            if variant.state is not None:
                rt_window._STATE[variant_file(variant)] = variant.state
        try:
            return {
                "state": _plain(entity.native_value),
                "device_class": entity.device_class,
                "icon": entity.icon,
                "attribution": entity.attribution,
                "name": entity._name if hasattr(entity, "_name") else getattr(entity, "_attr_name", None),
                "unique_id": entity.unique_id,
                "entity_category": entity.entity_category,
                "device": _plain(entity.device_info),
                "attributes": _plain(entity.extra_state_attributes),
            }
        finally:
            if gated:
                rt_window._STATE.pop(variant_file(variant), None)


def variant_file(variant):
    return getattr(variant, "file", "fixture")


# --- fixtures ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def variants():
    return _variants(fixture_db.shared(str(FIXTURES / "boarding")))


@pytest.fixture(scope="module")
def rendered(variants):
    return {label: _render(_build(variant), variant) for label, variant in variants.items()}


@pytest.fixture(scope="module")
def expected(rendered, variants):
    """The reviewed file; written from this run instead when asked to."""
    content = {"variants": rendered, "recorder": _recorder_split(rendered, variants)}
    if UPDATE:
        EXPECTED.parent.mkdir(exist_ok=True)
        EXPECTED.write_text(json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        return content
    if not EXPECTED.exists():
        pytest.fail(f"{EXPECTED} is missing: {REGENERATE}")
    return json.loads(EXPECTED.read_text(encoding="utf-8"))


# --- the file ----------------------------------------------------------------------

def _diff(label, want, got):
    """One line per field that moved, attributes key by key."""
    lines = []
    for field in sorted(set(want) | set(got)):
        if field == "attributes":
            continue
        if want.get(field, "<absent>") != got.get(field, "<absent>"):
            lines.append(f"  {field}: expected {want.get(field, '<absent>')!r}, "
                         f"got {got.get(field, '<absent>')!r}")
    was, now = want.get("attributes") or {}, got.get("attributes") or {}
    if want.get("attributes") is None or got.get("attributes") is None:
        if want.get("attributes") != got.get("attributes"):
            lines.append(f"  attributes: expected {want.get('attributes')!r}, got {got.get('attributes')!r}")
        return [f"variant {label!r}:"] + lines if lines else []
    for key in sorted(set(was) | set(now)):
        if was.get(key, "<absent>") != now.get(key, "<absent>"):
            lines.append(f"  attributes[{key!r}]: expected {was.get(key, '<absent>')!r}, "
                         f"got {now.get(key, '<absent>')!r}")
    return [f"variant {label!r}:"] + lines if lines else []


def _labels():
    """The variant names, for the test ids: read from the file, so a
    variant the code grows or loses is reported by the test below."""
    try:
        return sorted(json.loads(EXPECTED.read_text(encoding="utf-8"))["variants"])
    except (OSError, ValueError, KeyError):
        return []


@pytest.mark.parametrize("label", _labels())
def test_each_variant_renders_as_the_file_says(label, rendered, expected):
    want = expected["variants"][label]
    got = rendered.get(label)
    if got is None:
        pytest.fail(f"variant {label!r} is in the file and no longer rendered: {REGENERATE}")
    lines = _diff(label, want, got)
    assert not lines, "\n".join(lines + ["", f"If the change is meant: {REGENERATE}"])


def test_the_file_holds_every_variant_rendered_and_no_other(rendered, expected):
    missing = sorted(set(rendered) - set(expected["variants"]))
    stale = sorted(set(expected["variants"]) - set(rendered))
    assert not missing and not stale, (
        f"rendered but not in the file: {missing}; in the file but not rendered: {stale}. {REGENERATE}")


def test_home_assistant_reads_the_attributes_the_sensor_wrote(variants):
    # the sensor keeps its own dict and hands Home Assistant a reference to
    # it: rebinding one and not the other would serve a stale copy
    for label, variant in variants.items():
        if variant.kind not in ("departure", "restore", "local"):
            continue
        entity = _build(variant)
        assert entity.extra_state_attributes is entity._attributes, label


# --- updates -----------------------------------------------------------------------

@pytest.fixture(scope="module")
def updated(variants, rendered):
    """Every departure variant rendered after an update from every other
    one, and from the empty sensor of before the first refresh:
    [(label, first_label, fresh rendering, updated rendering)]."""
    departures = {label: v for label, v in variants.items() if v.kind == "departure"}
    starts = {**departures, "restore / before its first refresh": variants["restore / before its first refresh"]}
    out = []
    for first_label, first in starts.items():
        for label, then in departures.items():
            entity = _build(first)
            writes = []
            entity.async_write_ha_state = lambda writes=writes: writes.append(True)
            entity.coordinator.data = then.data
            with _clock(then.now):
                entity._handle_coordinator_update()
            got = _render(entity, then)
            got["writes"] = len(writes)
            # the entity keeps the name its entry gave it: not the update's to change
            for field in ("name", "unique_id", "device"):
                got[field] = rendered[label][field]
            out.append((label, first_label, {**rendered[label], "writes": 1}, got))
    return out


def test_an_update_renders_as_a_fresh_sensor_whatever_it_showed_before(updated):
    moved = []
    for label, first_label, want, got in updated:
        got = dict(got)
        # checked on its own below: a suspected bug, kept on record
        got["attribution"] = want["attribution"]
        if want["state"] is None:
            # no state: Home Assistant shows unknown whatever the class says
            got["device_class"] = want["device_class"]
        moved += _diff(f"{label}, after {first_label}", want, got)
    assert not moved, "\n".join(moved)


def test_a_sensor_showing_nothing_names_no_agency(updated):
    kept = [f"{label}, after {first_label}: {got['attribution']!r}"
            for label, first_label, want, got in updated
            if got["attribution"] != want["attribution"]]
    assert not kept, "\n".join(kept)


def test_a_local_stop_update_renders_as_a_fresh_sensor(variants, rendered):
    local = {label: v for label, v in variants.items() if v.kind == "local"}
    moved = []
    for first_label, first in local.items():
        for label, then in local.items():
            entity = _build(first)
            entity.async_write_ha_state = lambda: None
            entity.coordinator.data = then.data
            with _clock(then.now):
                entity._handle_coordinator_update()
            moved += _diff(f"{label}, after {first_label}", rendered[label], _render(entity, then))
    assert not moved, "\n".join(moved)


# --- the empty departure --------------------------------------------------------------

def _departure_field(key):
    """Whether only a real departure gives the sensor this attribute: its
    times, its trip, its calls at both ends (not the alert sentences on
    those stops, which the realtime feed writes with or without one)."""
    return (key in ("arrival", "day", "first", "last", "duration",
                    const.ATTR_TIMEZONE_ORIGIN, const.ATTR_TIMEZONE_DESTINATION)
            or key.startswith("trip_")
            or (key.startswith(("origin_stop_", "destination_stop_")) and "alert" not in key))


def test_no_departure_is_an_empty_dict_and_shows_no_departure_field(variants, rendered):
    data = variants["departure / boarding Mairie to Ecole, nobody gets on"].data
    # the helper's side of the contract: {} and nothing else, the sensor
    # reads any truthy answer as a departure and its fields
    assert data["next_departure"] == {} and type(data["next_departure"]) is dict
    assert gtfs_helper.get_next_departure(_hass(), {**data, "schedule": "no_zip_file"}) == {}
    for label, variant in variants.items():
        if variant.kind != "departure" or variant.data["next_departure"]:
            continue
        shown = rendered[label]
        assert shown["state"] is None, label
        written = [key for key in shown["attributes"] or {} if _departure_field(key)]
        assert not written, f"{label}: departure fields without a departure: {written}"


# --- the recorder -----------------------------------------------------------------------

CLASSES = {"departure": sensor.GTFSDepartureSensor, "restore": sensor.GTFSDepartureSensor,
           "local": sensor.GTFSLocalStopSensor}


def _recorder_split(rendered, variants):
    """Per sensor class, the attribute names written in any variant, split
    by whether the recorder keeps them, and the unrecorded names no
    variant writes."""
    split = {}
    for label, variant in variants.items():
        cls = CLASSES.get(variant.kind)
        if cls is None:
            continue
        seen = split.setdefault(cls.__name__, set())
        seen.update((rendered[label]["attributes"] or {}).keys())
    out = {}
    for name, seen in split.items():
        unrecorded = getattr(sensor, name)._unrecorded_attributes
        out[name] = {"recorded": sorted(seen - unrecorded),
                     "unrecorded": sorted(seen & unrecorded),
                     "unrecorded, never written here": sorted(unrecorded - seen)}
    return out


def test_every_list_the_sensors_write_stays_out_of_the_recorder(rendered, variants, expected):
    # a list or a mapping of departures is kilobytes rewritten every minute
    kept = []
    for label, variant in variants.items():
        cls = CLASSES.get(variant.kind)
        if cls is None:
            continue
        for key, value in (rendered[label]["attributes"] or {}).items():
            if isinstance(value, (list, dict)) and value and key not in cls._unrecorded_attributes:
                kept.append(f"{label}: {key}")
    assert not kept, "recorded lists: " + ", ".join(kept)
    assert _recorder_split(rendered, variants) == expected["recorder"], (
        f"the recorded / unrecorded split moved: {REGENERATE}")
