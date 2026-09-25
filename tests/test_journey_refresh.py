"""What a journey sensor's refresh decides, one branch at a time.

GTFSUpdateCoordinator._async_update_data runs every minute. It reads the
timetable again only when its refresh interval is up or the departure
shown has left, carries the last answer over while the database is being
written, reads the realtime feeds only when the source has them and its
service window is open, and keeps the timetable standing when a feed
fails. test_route_combined checks what one refresh returns on captured
cases; here each of those decisions is driven on its own.

Every reader and writer the refresh calls is replaced by a stand-in that
notes its call and answers what the test says. The coordinator itself,
and drop_struck_trips which it hands the struck trips to, run as they
are. Where a test needs get_gtfs, check_extracting and the readers behind
them to see the database files, they run for real on a scratch folder.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import contextlib
import datetime
import sys
import types
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import ha_stub

ha_stub.install()

coordinator_mod = ha_stub.load("coordinator")
refresh_steps_mod = sys.modules["gtfs2_under_test.refresh_steps"]
const = sys.modules["gtfs2_under_test.const"]

UTC = datetime.timezone.utc
# not on a whole second: gtfs_updated_at is written with isoformat, which
# leaves the microseconds out when there are none
NOW = datetime.datetime(2026, 9, 25, 10, 0, 0, 250000)

ENTRY = {
    "origin": "S1: Gare (1)",
    "destination": "S9: Port (9)",
    "name": "Gare to Port",
    "file": "town",
    "route_type": "3",
    "route": "R1: Line 1",
    "direction": "0",
}

# the options of an entry that reads the realtime feeds itself, with no
# datasource entry to take them from
REALTIME = {
    "real_time": True,
    "trip_update_url": "http://rt.test/trips",
    "alerts_url": "http://rt.test/alerts",
    "vehicle_position_url": None,
}

SCHEDULE = object()
ALERTS = {"R1": ["Works at Gare"]}


def departure(trip="T1", minutes=20):
    """A departure as get_next_departure hands it back, leaving in some minutes."""
    return {
        "route_id": "R1",
        "trip_id": trip,
        "origin_stop_id": "S1",
        "origin_stop_sequence": 1,
        "destination_stop_id": "S9",
        "trip_direction_id": "0",
        "trip_short_name": None,
        "next_departures_trip_id": [trip, "T2", "T3"],
        "departure_time": NOW.replace(tzinfo=UTC) + datetime.timedelta(minutes=minutes),
    }


class _Hass:
    """What the refresh asks of Home Assistant: the config folder (a
    scratch one), the executor (run in place), the background tasks (on
    the test's loop), the entity states, and the config entries, none of
    them a datasource: the sensor's own options hold its realtime."""

    def __init__(self, root) -> None:
        self.config = types.SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts)))
        self.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [])
        self.states = types.SimpleNamespace(get=lambda entity_id: None)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_background_task(self, coro, name):
        return asyncio.get_running_loop().create_task(coro)


class _Entry:
    def __init__(self, options=None) -> None:
        self.entry_id = "journey"
        self.data = dict(ENTRY)
        self.options = {"offset": 0, **(options or {})}


class _Registry:
    """The entity registry, holding the markers geo_json_events left."""

    def __init__(self, entries) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}
        self.removed = []

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)


def marker(entity_id, unique_id, platform="geo_json_events"):
    return types.SimpleNamespace(entity_id=entity_id, unique_id=unique_id,
                                 domain="geo_location", platform=platform)


class Refresh:
    """One journey coordinator, and the stand-ins its refresh calls.

    Each stand-in notes its call in `calls`, by name, and in `order`.
    `answers` holds what they answer; `failing` names the ones that raise;
    `real` names the ones left as they are.
    """

    def __init__(self, tmp_path, options=None, real=()) -> None:
        self.root = tmp_path
        (tmp_path / "gtfs2").mkdir(exist_ok=True)
        self.hass = _Hass(tmp_path)
        self.entry = _Entry(options)
        self.coordinator = coordinator_mod.GTFSUpdateCoordinator(self.hass, self.entry)
        self.real = set(real)
        self.failing = set()
        self.calls = defaultdict(list)
        self.order = []
        self.registry = _Registry([])
        self.answers = {
            "get_gtfs": SCHEDULE,
            "get_next_departure": departure(),
            "next_service_date_for": "2026-10-01",
            "rt_window_gate": None,
            "get_rt_alerts": ALERTS,
            # what the feed struck out, as get_next_services leaves it
            "cancelled": {},
            # the departure read again without the struck trips
            "drop_departure_trips": departure("T2"),
        }

    def _noted(self, name, *args):
        self.calls[name].append(args)
        self.order.append(name)
        if name in self.failing:
            raise RuntimeError(f"{name} failed")

    def _stand_in(self, name, answer=None):
        def stand_in(*args):
            self._noted(name, *args)
            return self.answers.get(name) if answer is None else answer(*args)
        return stand_in

    def _async_stand_in(self, name):
        async def stand_in(*args):
            self._noted(name, *args)
            return self.answers.get(name)
        return stand_in

    def _next_departure(self, hass, data):
        # what the real one leaves beside its answer: the rows it read,
        # which drop_departure_trips reads again without the struck trips
        data["departure_rows"] = ["row"]
        found = self.answers["get_next_departure"]
        return dict(found) if found else {}

    def _next_services(self, coordinator):
        # the trip updates read, kept on the coordinator for the leg file,
        # and what they struck out
        coordinator._feed_entities = ["trip update"]
        coordinator._rt_cancelled = dict(self.answers["cancelled"])
        departure_time = NOW.replace(tzinfo=UTC) + datetime.timedelta(minutes=22)
        return {const.ATTR_NEXT_RT: [departure_time]}

    def _patches(self):
        stand_ins = {
            (coordinator_mod, "get_gtfs"): self._stand_in("get_gtfs"),
            (coordinator_mod, "check_datasource_index"): self._stand_in("check_datasource_index"),
            (coordinator_mod, "get_next_departure"): self._stand_in(
                "get_next_departure", self._next_departure),
            (coordinator_mod, "departure_records"): self._stand_in(
                "departure_records", lambda schedule, data: {"trip": data["next_departure"].get("trip_id")}),
            (coordinator_mod, "export_route_shape"): self._async_stand_in("export_route_shape"),
            (coordinator_mod, "export_timetable"): self._async_stand_in("export_timetable"),
            (coordinator_mod, "export_leg"): self._async_stand_in("export_leg"),
            (coordinator_mod, "next_service_date_for"): self._async_stand_in("next_service_date_for"),
            (coordinator_mod, "rt_window_gate"): self._stand_in("rt_window_gate"),
            (coordinator_mod, "clear_vehicle_file"): self._stand_in("clear_vehicle_file"),
            (coordinator_mod, "get_rt_alerts"): self._stand_in("get_rt_alerts"),
            (coordinator_mod, "get_next_services"): self._stand_in(
                "get_next_services", self._next_services),
            (refresh_steps_mod, "get_rt_alerts"): self._stand_in("get_rt_alerts"),
            (refresh_steps_mod, "get_next_services"): self._stand_in(
                "get_next_services", self._next_services),
            (refresh_steps_mod, "drop_departure_trips"): self._stand_in("drop_departure_trips"),
            (coordinator_mod, "er"): types.SimpleNamespace(
                async_get=self._stand_in("entity_registry", lambda hass: self.registry)),
        }
        return {key: value for key, value in stand_ins.items() if key[1] not in self.real}

    def run(self, at=NOW):
        """One refresh at a moment, its answer kept as Home Assistant would."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(freeze_time(at))
            for (module, name), stand_in in self._patches().items():
                stack.enter_context(patch.object(module, name, stand_in))
            result = asyncio.run(self.coordinator._async_update_data())
        self.coordinator.data = result
        return result

    def count(self, name) -> int:
        return len(self.calls[name])


def later(minutes):
    return NOW + datetime.timedelta(minutes=minutes)


# --- the timetable: read again or kept ---------------------------------------

def test_a_first_refresh_reads_the_timetable_and_writes_the_files(tmp_path):
    refresh = Refresh(tmp_path)
    result = refresh.run()
    assert result is refresh.coordinator._data
    assert result["schedule"] is SCHEDULE
    assert result["next_departure"]["trip_id"] == "T1"
    assert result["gtfs_updated_at"] == NOW.replace(tzinfo=UTC).isoformat()
    assert result["extracting"] is False
    assert result["records"] == {"trip": "T1"}
    assert refresh.count("check_datasource_index") == 1
    assert refresh.count("export_route_shape") == refresh.count("export_timetable") == 1
    # no realtime on this entry: the leg file follows the static departures
    assert refresh.calls["export_leg"] == [(refresh.coordinator, refresh.entry.data, None)]
    # a departure is left: no need to look ahead for another day
    assert "next_service_date" not in result
    assert result["next_departure_realtime_attr"] == {} and result["alert"] == {}
    assert refresh.count("rt_window_gate") == 0


def test_the_entry_fields_reach_the_data(tmp_path):
    refresh = Refresh(tmp_path, options={"offset": 5})
    refresh.entry.data.update(origin_stations=["Gare"], loop_direction="1", line="L1")
    result = refresh.run()
    assert result["offset"] == 5
    assert result["origin_stations"] == ["Gare"]
    # only the stations an entry ticked: no empty key for the other end
    assert "destination_stations" not in result
    assert result["loop_direction"] == "1" and result["line"] == "L1"
    assert (result["origin"], result["destination"], result["route"]) == (
        ENTRY["origin"], ENTRY["destination"], ENTRY["route"])


def test_within_the_interval_the_last_reading_is_kept(tmp_path):
    refresh = Refresh(tmp_path)
    first = dict(refresh.run())
    second = refresh.run(later(5))
    assert refresh.count("get_next_departure") == 1
    assert refresh.count("check_datasource_index") == 1
    assert refresh.count("export_route_shape") == refresh.count("export_timetable") == 1
    # nothing moved: no realtime, no new departures, the leg file stays
    assert refresh.count("export_leg") == 1
    assert second["gtfs_updated_at"] == first["gtfs_updated_at"]
    assert second["next_departure"] == first["next_departure"]
    assert second["extracting"] is False
    # the same departure: its records are not read again
    assert refresh.count("departure_records") == 1
    # but the schedule was asked for on every refresh
    assert refresh.count("get_gtfs") == 2


@pytest.mark.parametrize("minutes, interval, again", [
    (16, None, True),   # the default interval, 15 minutes, is up
    (14, None, False),
    (16, 30, False),    # the entry's own interval holds it
    (31, 30, True),
])
def test_the_refresh_interval_decides(tmp_path, minutes, interval, again):
    refresh = Refresh(tmp_path, options={} if interval is None else {"refresh_interval": interval})
    refresh.answers["get_next_departure"] = departure(minutes=60)
    refresh.run()
    result = refresh.run(later(minutes))
    assert refresh.count("get_next_departure") == (2 if again else 1)
    assert result["gtfs_updated_at"] == (later(minutes) if again else NOW).replace(tzinfo=UTC).isoformat()


def test_a_departure_gone_is_read_again_before_the_interval(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.answers["get_next_departure"] = departure(minutes=3)
    refresh.run()
    refresh.answers["get_next_departure"] = departure("T2", minutes=12)
    result = refresh.run(later(5))
    assert refresh.count("get_next_departure") == 2
    assert result["next_departure"]["trip_id"] == "T2"
    assert refresh.count("departure_records") == 2


def test_a_departure_gone_but_late_on_the_feed_is_kept(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.answers["get_next_departure"] = departure(minutes=3)
    refresh.run()
    # the feed has it leaving at 22 minutes past: still coming at 5
    result = refresh.run(later(5))
    assert refresh.count("get_next_departure") == 1
    assert result["next_departure"]["trip_id"] == "T1"


def test_a_failed_reading_fails_the_update(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.failing.add("get_next_departure")
    with pytest.raises(coordinator_mod.UpdateFailed, match="get_next_departure failed"):
        refresh.run()


def test_nothing_left_today_looks_ahead(tmp_path):
    refresh = Refresh(tmp_path, options={"offset": 5})
    refresh.answers["get_next_departure"] = {}
    result = refresh.run()
    assert result["next_departure"] == {}
    assert result["next_service_date"] == "2026-10-01"
    assert refresh.calls["next_service_date_for"] == [
        (refresh.hass, SCHEDULE, refresh.entry.data, 5)]
    # the files do not wait for a departure: the line is the same tomorrow
    assert refresh.count("export_route_shape") == refresh.count("export_timetable") == 1


# --- while the database is written, and without one --------------------------

def test_while_the_database_is_written_the_last_answer_stays(tmp_path):
    refresh = Refresh(tmp_path, real={"check_extracting"})
    first = dict(refresh.run())
    journal = tmp_path / "gtfs2" / "town.sqlite-journal"
    journal.write_bytes(b"")
    # what get_gtfs says while the journal is there
    refresh.answers["get_gtfs"] = "extracting"
    during = refresh.run(later(20))
    assert during["extracting"] is True
    assert during["next_departure"] == first["next_departure"]
    assert during["gtfs_updated_at"] == first["gtfs_updated_at"]
    # nothing read, nothing written, not even past the interval
    assert refresh.count("get_next_departure") == 1
    assert refresh.count("export_leg") == 1
    assert refresh.count("departure_records") == 1

    journal.unlink()
    refresh.answers["get_gtfs"] = SCHEDULE
    after = refresh.run(later(21))
    # the flag the last minute was left with is cleared, and the reading
    # overdue is made
    assert after["extracting"] is False
    assert refresh.count("get_next_departure") == 2


def test_a_first_refresh_while_the_database_is_written(tmp_path):
    refresh = Refresh(tmp_path, real={"check_extracting"})
    (tmp_path / "gtfs2" / "town.sqlite-journal").write_bytes(b"")
    refresh.answers["get_gtfs"] = "extracting"
    result = refresh.run()
    assert result["extracting"] is True
    assert result["next_departure"] == {}
    assert "gtfs_updated_at" not in result
    assert refresh.count("get_next_departure") == 0


def test_the_flag_is_cleared_within_the_interval(tmp_path):
    refresh = Refresh(tmp_path, real={"check_extracting"})
    refresh.run()
    journal = tmp_path / "gtfs2" / "town.sqlite-journal"
    journal.write_bytes(b"")
    assert refresh.run(later(1))["extracting"] is True
    journal.unlink()
    result = refresh.run(later(2))
    assert result["extracting"] is False
    assert refresh.count("get_next_departure") == 1


class _Schedule:
    """A schedule as far as closing it goes: its session and its engine."""

    def __init__(self, closed) -> None:
        self.session = types.SimpleNamespace(close=lambda: closed.append("session"))
        self.engine = types.SimpleNamespace(dispose=lambda: closed.append("engine"))


def test_the_schedule_is_reopened_only_when_its_database_changes(tmp_path):
    refresh = Refresh(tmp_path)
    closed = []
    first = refresh.answers["get_gtfs"] = _Schedule(closed)
    database = tmp_path / "gtfs2" / "town.sqlite"
    database.write_bytes(b"one")
    refresh.run()
    refresh.run(later(1))
    assert refresh.count("get_gtfs") == 1
    assert refresh.coordinator._pygtfs is first
    # a refresh of the source wrote to it: the old one is let go
    database.write_bytes(b"one and more")
    second = refresh.answers["get_gtfs"] = _Schedule(closed)
    refresh.run(later(2))
    assert refresh.count("get_gtfs") == 2
    assert closed == ["session", "engine"]
    assert refresh.coordinator._pygtfs is second


def test_a_schedule_that_will_not_close_is_let_go(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.answers["get_gtfs"] = types.SimpleNamespace(session=types.SimpleNamespace())
    refresh.run()
    refresh.answers["get_gtfs"] = SCHEDULE
    assert refresh.run(later(1))["next_departure"]["trip_id"] == "T1"
    assert refresh.coordinator._pygtfs is SCHEDULE


def test_records_that_cannot_be_read_are_read_next_minute(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.failing.add("departure_records")
    result = refresh.run()
    assert "records" not in result
    assert result["next_departure"]["trip_id"] == "T1"
    refresh.failing.clear()
    assert refresh.run(later(1))["records"] == {"trip": "T1"}
    assert refresh.count("departure_records") == 2


@pytest.mark.parametrize("files, answer", [
    (("town.zip",), "not_built"),
    ((), "no_zip_file"),
    # an answer the config flow also knows; get_gtfs itself no longer
    # gives it, and any word stands for no schedule the same way
    (None, "no_data_file"),
])
def test_a_source_without_a_database_shows_an_empty_board(tmp_path, files, answer):
    real = {"get_gtfs", "check_extracting", "check_datasource_index",
            "get_next_departure", "departure_records"}
    if files is None:
        real.discard("get_gtfs")
    refresh = Refresh(tmp_path, real=real)
    refresh.answers["get_gtfs"] = answer
    for name in files or ():
        (tmp_path / "gtfs2" / name).write_bytes(b"PK")
    result = refresh.run()
    assert result["schedule"] == answer
    assert result["next_departure"] == {}
    assert result["extracting"] is False
    assert result["next_service_date"] == "2026-10-01"
    assert result["records"] == {"origin": None, "destination": None, "trip": None,
                                 "route": None, "agency": None}
    # nothing is built from here: no database appears
    assert not (tmp_path / "gtfs2" / "town.sqlite").exists()
    # and a minute later the schedule is asked for again
    refresh.run(later(1))
    assert refresh.coordinator._pygtfs == answer
    if files is None:
        assert refresh.count("get_gtfs") == 2


# --- realtime ----------------------------------------------------------------

def test_realtime_reads_the_alerts_then_the_trip_updates(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    result = refresh.run()
    me = refresh.coordinator
    assert refresh.calls["rt_window_gate"] == [
        (refresh.hass, "town", SCHEDULE, "http://rt.test/trips")]
    assert refresh.order.index("get_rt_alerts") < refresh.order.index("get_next_services")
    assert result["alert"] == ALERTS
    realtime = result["next_departure_realtime_attr"]
    assert realtime[const.ATTR_NEXT_RT]
    assert realtime["gtfs_rt_updated_at"] == NOW.replace(tzinfo=UTC)
    # drop_struck_trips ran, with nothing struck
    assert realtime[const.ATTR_RT_CANCELLED] == [] and realtime[const.ATTR_RT_SKIPPED] == []
    # what the realtime readers are handed, from the departure shown
    assert (me._route_id, me._stop_id, me._stop_sequence, me._trip_id, me._direction) == (
        "R1", "S1", 1, "T1", "0")
    assert me._trip_list == ["T1", "T2", "T3"]
    assert me._trip_update_url == "http://rt.test/trips"
    assert me._alerts_url == "http://rt.test/alerts"
    assert me._vehicle_position_url is None
    assert me._vehicle_max_age == const.DEFAULT_VEHICLE_MAX_AGE
    # the leg file gets the trip updates just read
    assert refresh.calls["export_leg"] == [(me, refresh.entry.data, ["trip update"])]
    assert "vehicle_positions_file" not in result


def test_a_realtime_minute_between_two_readings(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.run()
    result = refresh.run(later(1))
    assert refresh.count("get_next_departure") == 1
    assert refresh.count("get_next_services") == 2
    assert result["next_departure_realtime_attr"]["gtfs_rt_updated_at"] == later(1).replace(tzinfo=UTC)
    # the realtime moved, so the leg file follows it
    assert refresh.count("export_leg") == 2


def test_realtime_with_no_departure_left_runs_on_the_entry(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.answers["get_next_departure"] = {}
    result = refresh.run()
    me = refresh.coordinator
    assert refresh.count("get_next_services") == 1
    assert (me._route_id, me._stop_id, me._stop_sequence, me._trip_id, me._direction) == (
        "R1", "S1", None, "no_trip_information", "0")
    assert me._trip_list == []
    assert result["next_departure_realtime_attr"][const.ATTR_NEXT_RT]


def test_the_query_key_joins_every_feed(tmp_path):
    refresh = Refresh(tmp_path, options={
        **REALTIME, "vehicle_position_url": "http://rt.test/vehicles",
        "api_key": "k", "api_key_location": "query_string", "api_key_name": "token"})
    refresh.run()
    me = refresh.coordinator
    assert me._trip_update_url == "http://rt.test/trips?token=k"
    assert me._alerts_url == "http://rt.test/alerts?token=k"
    assert me._vehicle_position_url == "http://rt.test/vehicles?token=k"
    assert refresh.calls["rt_window_gate"][0][3] == "http://rt.test/trips?token=k"


def test_failing_alerts_leave_the_trip_updates(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.failing.add("get_rt_alerts")
    result = refresh.run()
    assert result["alert"] == {}
    assert result["next_departure_realtime_attr"][const.ATTR_NEXT_RT]
    assert refresh.count("export_leg") == 1


def test_failing_trip_updates_leave_the_timetable(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.failing.add("get_next_services")
    result = refresh.run()
    assert result["next_departure"]["trip_id"] == "T1"
    assert result["gtfs_updated_at"]
    assert result["next_departure_realtime_attr"] == {}
    # the alerts, read first, stand
    assert result["alert"] == ALERTS
    assert result["records"] == {"trip": "T1"}


@pytest.mark.parametrize("vehicles", [None, "http://rt.test/vehicles"])
def test_outside_the_window_the_feeds_are_left_alone(tmp_path, vehicles):
    refresh = Refresh(tmp_path, options={**REALTIME, "vehicle_position_url": vehicles})
    refresh.run()
    assert refresh.run(later(1))["alert"] == ALERTS
    refresh.answers["rt_window_gate"] = "out_of_window"
    result = refresh.run(later(2))
    assert refresh.count("get_next_services") == 2
    assert refresh.count("get_rt_alerts") == 2
    # the delays and alerts of the last reading are not served as current
    assert result["next_departure_realtime_attr"] == {}
    assert result["alert"] == {}
    # the map is told the vehicles are no longer followed
    assert refresh.calls["clear_vehicle_file"] == (
        [(refresh.hass, "R1", "0")] if vehicles else [])
    # nothing moved: the leg file stays as it was
    assert refresh.count("export_leg") == 2


def test_outside_the_window_with_no_departure_the_map_is_told_from_the_entry(tmp_path):
    refresh = Refresh(tmp_path, options={**REALTIME, "vehicle_position_url": "http://rt.test/vehicles"})
    refresh.answers["get_next_departure"] = {}
    refresh.answers["rt_window_gate"] = "no_service_today"
    refresh.run()
    assert refresh.calls["clear_vehicle_file"] == [(refresh.hass, "R1", "0")]


def test_realtime_switched_off_clears_the_last_reading(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.run()
    refresh.entry.options = {"offset": 0, **REALTIME, "real_time": False}
    result = refresh.run(later(1))
    assert result["next_departure_realtime_attr"] == {}
    assert result["alert"] == {}
    assert refresh.count("rt_window_gate") == 1
    assert refresh.count("clear_vehicle_file") == 0


def test_vehicle_positions_name_their_file_and_clear_stale_markers_once(tmp_path):
    refresh = Refresh(tmp_path, options={**REALTIME, "vehicle_position_url": "http://rt.test/vehicles"})
    stale = marker("geo_location.r1_12_5", "R1(12)5")
    shown = marker("geo_location.r1_13_7", "R1(13)7")
    other_line = marker("geo_location.r2_14_1", "R2(14)1")
    other_platform = marker("geo_location.r1_15_2", "R1(15)2", platform="usgs")
    refresh.registry = _Registry([stale, shown, other_line, other_platform])
    refresh.hass.states.get = lambda entity_id: object() if entity_id == shown.entity_id else None
    result = refresh.run()
    assert result["vehicle_positions_file"] == coordinator_mod.vehicle_positions_name("R1", "0")
    assert refresh.registry.removed == [stale.entity_id]
    refresh.run(later(1))
    assert refresh.count("entity_registry") == 1


def test_a_struck_trip_moves_the_board_on(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.answers["cancelled"] = {"T1": None}
    result = refresh.run()
    assert refresh.count("drop_departure_trips") == 1
    assert result["next_departure"]["trip_id"] == "T2"
    assert refresh.coordinator._trip_id == "T2"
    realtime = result["next_departure_realtime_attr"]
    assert realtime[const.ATTR_RT_CANCELLED] == ["T1"]
    # the alerts are read again for the departure now shown
    assert refresh.count("get_rt_alerts") == 2


def test_a_failing_struck_reading_keeps_the_departure(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.answers["cancelled"] = {"T1": None}
    refresh.failing.add("drop_departure_trips")
    result = refresh.run()
    # the reading again failed: the trip updates of the cycle are kept on
    # the departure the timetable gave, and the refresh still answers
    assert result["next_departure"]["trip_id"] == "T1"
    assert result["next_departure_realtime_attr"][const.ATTR_NEXT_RT]
    assert result["records"] == {"trip": "T1"}
