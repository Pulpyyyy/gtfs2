"""What a local stops sensor's refresh decides, one branch at a time.

GTFSLocalStopUpdateCoordinator._async_update_data runs every minute and
reads the stops at the entry's own pace, 15 minutes unless its options say
otherwise; in between it only takes out the departures gone, and its
sensors hear of a minute only when one has. It opens the source's
schedule, lists nothing and says so once when there is none, carries the
last answer over while the database is being written, and hands
get_local_stops_next_departures the realtime settings of the source: the
trip updates only, the key where it travels, and no feed at all when the
service window is shut. test_stop_combined checks what the listing
returns on captured cases; here each of those decisions is driven on its
own.

get_local_stops_next_departures, check_extracting, check_datasource_index,
get_gtfs and rt_window_gate are replaced by stand-ins that note their call
and answer what the test says. The coordinator, schedule_for and the
realtime settings readers (rt_feed_config, with_query_key, rt_headers) run
as they are.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
import contextlib
import datetime
import logging
import sys
import types
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import ha_stub

ha_stub.install()

coordinator_mod = ha_stub.load("coordinator")
const = sys.modules["gtfs2_under_test.const"]

ENTRY = {
    "name": "Around me",
    "file": "town",
    "device_tracker_id": "person.me",
}

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
SCHEDULE = types.SimpleNamespace(session=None)
DEPARTURES = [{"stop_id": "S1", "departure": []}]


def later(minutes):
    return NOW + datetime.timedelta(minutes=minutes)


def leaving(minutes, realtime=None, delay="-"):
    """One departure of the list, as the reading writes it."""
    return {"trip_id": f"T{minutes}", "departure_datetime": later(minutes),
            "departure_realtime_datetime": later(realtime) if realtime is not None else "-",
            "delay_realtime": delay}


class _Hass:
    """What the refresh asks of Home Assistant: the config folder (a
    scratch one), the executor (run in place) and the config entries,
    none of them a datasource: the sensor's own options hold its realtime."""

    def __init__(self, root) -> None:
        self.config = types.SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts)))
        self.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [])

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class _Entry:
    def __init__(self, options=None) -> None:
        self.entry_id = "local"
        self.data = dict(ENTRY)
        self.options = dict(options or {})


class Refresh:
    """One local stops coordinator, and the stand-ins its refresh calls.

    Each stand-in notes its call in `calls`, by name; `answers` holds what
    they answer. The listing notes what the coordinator held when it ran:
    `seen` keeps its realtime settings.
    """

    def __init__(self, tmp_path, options=None) -> None:
        (tmp_path / "gtfs2").mkdir(exist_ok=True)
        self.hass = _Hass(tmp_path)
        self.entry = _Entry(options)
        self.coordinator = coordinator_mod.GTFSLocalStopUpdateCoordinator(self.hass, self.entry)
        self.calls = defaultdict(list)
        self.seen = []
        self.answers = {
            "get_gtfs": SCHEDULE,
            "check_extracting": False,
            "rt_window_gate": None,
            "departures": DEPARTURES,
        }
        self.failing = False
        self.runs = 0

    def _stand_in(self, name):
        def stand_in(*args):
            self.calls[name].append(args)
            return self.answers.get(name)
        return stand_in

    def _listing(self, coordinator):
        self.calls["get_local_stops_next_departures"].append(coordinator)
        self.seen.append({
            "realtime": coordinator._realtime,
            "trip_update_url": getattr(coordinator, "_trip_update_url", None),
            "vehicle_position_url": getattr(coordinator, "_vehicle_position_url", None),
            "alerts_url": getattr(coordinator, "_alerts_url", None),
            "headers": getattr(coordinator, "_headers", None),
            "group": getattr(coordinator, "_rt_group", None),
        })
        if self.failing:
            raise RuntimeError("the stops could not be read")
        return [dict(stop, departure=list(stop["departure"])) for stop in self.answers["departures"]]

    def run(self, at=None):
        """One refresh at a moment, its answer kept as Home Assistant
        would: by default a pace apart from the last one, so each reads."""
        if at is None:
            at = later(16 * self.runs)
        self.runs += 1
        stand_ins = {
            "get_gtfs": self._stand_in("get_gtfs"),
            "check_extracting": self._stand_in("check_extracting"),
            "check_datasource_index": self._stand_in("check_datasource_index"),
            "rt_window_gate": self._stand_in("rt_window_gate"),
            "get_local_stops_next_departures": self._listing,
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(freeze_time(at))
            for name, stand_in in stand_ins.items():
                stack.enter_context(patch.object(coordinator_mod, name, stand_in))
            result = asyncio.run(self.coordinator._async_update_data())
        self.coordinator.data = result
        return result


# --- the pace and the settings -----------------------------------------------

def test_it_runs_every_minute_and_its_sensors_hear_of_changes_only(tmp_path):
    coordinator = Refresh(tmp_path).coordinator
    assert coordinator.update_interval == datetime.timedelta(minutes=1)
    assert coordinator.always_update is False


def test_the_stops_are_read_at_the_entry_s_pace(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.run(NOW)
    refresh.run(later(const.DEFAULT_LOCAL_STOP_REFRESH_INTERVAL - 1))
    assert len(refresh.calls["get_local_stops_next_departures"]) == 1
    refresh.run(later(const.DEFAULT_LOCAL_STOP_REFRESH_INTERVAL))
    assert len(refresh.calls["get_local_stops_next_departures"]) == 2
    paced = Refresh(tmp_path, options={"local_stop_refresh_interval": 5})
    paced.run(NOW)
    paced.run(later(5))
    assert len(paced.calls["get_local_stops_next_departures"]) == 2


def test_a_refresh_lists_the_stops_with_the_entry_s_settings(tmp_path):
    refresh = Refresh(tmp_path, options={"offset": 3, "radius": 500})
    result = refresh.run()
    assert result["local_stops_next_departures"] == DEPARTURES
    assert result["schedule"] is SCHEDULE
    assert (result["offset"], result["radius"]) == (3, 500)
    assert result["timerange"] == const.DEFAULT_LOCAL_STOP_TIMERANGE
    assert result["device_tracker_id"] == "person.me"
    assert result["extracting"] is False
    assert len(refresh.calls["check_datasource_index"]) == 1
    # no realtime on this entry: the listing reads no feed, and the
    # window is not even looked at
    assert refresh.seen == [{**refresh.seen[0], "realtime": False}]
    assert refresh.calls["rt_window_gate"] == []


def test_the_schedule_is_kept_while_the_database_is_the_same(tmp_path):
    # a database on disk: schedule_for compares its edition with the one
    # the schedule was opened on. The local stops coordinator did not
    # declare it, and its first refresh failed on every install
    refresh = Refresh(tmp_path)
    (tmp_path / "gtfs2" / "town.sqlite").write_bytes(b"db")
    refresh.run()
    refresh.run()
    assert len(refresh.calls["get_gtfs"]) == 1
    assert refresh.coordinator.data["local_stops_next_departures"] == DEPARTURES


# --- no database, or one being written ----------------------------------------

def test_no_database_lists_nothing_and_says_so_once(tmp_path, caplog):
    refresh = Refresh(tmp_path)
    refresh.answers["get_gtfs"] = "not_built"
    with caplog.at_level(logging.WARNING):
        first = refresh.run()
        refresh.run()
    assert first["local_stops_next_departures"] == []
    assert refresh.calls["get_local_stops_next_departures"] == []
    assert refresh.calls["check_datasource_index"] == []
    said = [r for r in caplog.records if "no usable schedule" in r.getMessage()]
    assert len(said) == 1 and "not_built" in said[0].getMessage()


def test_a_database_back_then_gone_again_is_said_again(tmp_path, caplog):
    refresh = Refresh(tmp_path)
    refresh.answers["get_gtfs"] = "not_built"
    with caplog.at_level(logging.WARNING):
        refresh.run()
        refresh.answers["get_gtfs"] = SCHEDULE
        assert refresh.run()["local_stops_next_departures"] == DEPARTURES
        refresh.answers["get_gtfs"] = "not_built"
        refresh.run()
    assert len([r for r in caplog.records if "no usable schedule" in r.getMessage()]) == 2


def test_a_database_being_written_keeps_the_last_answer(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.run()
    refresh.answers["check_extracting"] = True
    result = refresh.run()
    assert result["extracting"] is True
    assert result["local_stops_next_departures"] == DEPARTURES
    # the listing is not run against a database in the middle of a write
    assert len(refresh.calls["get_local_stops_next_departures"]) == 1


def test_a_listing_that_fails_fails_the_refresh(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.failing = True
    with pytest.raises(coordinator_mod.UpdateFailed):
        refresh.run()


# --- the realtime feeds the listing reads --------------------------------------

REALTIME = {
    "real_time": True,
    "trip_update_url": "http://rt.test/trips",
    "vehicle_position_url": "http://rt.test/vehicles",
    "alerts_url": "http://rt.test/alerts",
}


def test_realtime_reads_the_trip_updates_by_trip_and_no_vehicle(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.run()
    seen = refresh.seen[0]
    assert seen["realtime"] is True
    assert seen["trip_update_url"] == "http://rt.test/trips"
    assert seen["group"] == "trip"
    # a local stops sensor owns no route to draw: the vehicle feed is not
    # read, nor any route's map file written
    assert seen["vehicle_position_url"] is None
    assert seen["headers"] == {}


def test_a_key_in_the_query_joins_the_trip_updates_url(tmp_path):
    refresh = Refresh(tmp_path, options={
        **REALTIME, "api_key": "k+1", "api_key_name": "token", "api_key_location": "query_string"})
    refresh.run()
    assert refresh.seen[0]["trip_update_url"] == "http://rt.test/trips?token=k%2B1"
    assert refresh.seen[0]["headers"] == {}


def test_a_key_in_a_header_goes_in_the_headers(tmp_path):
    refresh = Refresh(tmp_path, options={
        **REALTIME, "api_key": "k1", "api_key_name": "x-key", "api_key_location": "header"})
    refresh.run()
    assert refresh.seen[0]["trip_update_url"] == "http://rt.test/trips"
    assert refresh.seen[0]["headers"] == {"x-key": "k1"}


def test_a_source_without_trip_updates_reads_no_feed(tmp_path):
    # alerts or vehicles alone say nothing of the departures around a stop:
    # reading them would download a feed that is not there
    refresh = Refresh(tmp_path, options={**REALTIME, "trip_update_url": None})
    refresh.run()
    assert refresh.seen[0]["realtime"] is False
    assert refresh.calls["rt_window_gate"] == []


def test_outside_the_service_window_no_feed_is_read(tmp_path):
    refresh = Refresh(tmp_path, options=REALTIME)
    refresh.answers["rt_window_gate"] = "no service before 05:00"
    result = refresh.run()
    assert refresh.seen[0]["realtime"] is False
    assert result["local_stops_next_departures"] == DEPARTURES
    # asked about the url the listing would have fetched
    assert refresh.calls["rt_window_gate"][0][3] == "http://rt.test/trips"


# --- between two readings: the departures gone go -------------------------------

def test_a_departure_gone_goes_within_the_minute_without_reading(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.answers["departures"] = [{"stop_id": "S1", "departure": [leaving(1), leaving(10)]}]
    refresh.run(NOW)
    result = refresh.run(later(2))
    assert [d["trip_id"] for d in result["local_stops_next_departures"][0]["departure"]] == ["T10"]
    assert len(refresh.calls["get_local_stops_next_departures"]) == 1
    # everything else the sensors show is the reading's
    assert result["gtfs_updated_at"] == NOW.isoformat()


def test_a_minute_with_nothing_gone_is_the_same_answer(tmp_path):
    # the same data: the coordinator tells its sensors nothing, and they
    # write no line of history
    refresh = Refresh(tmp_path)
    refresh.answers["departures"] = [{"stop_id": "S1", "departure": [leaving(10)]}]
    first = refresh.run(NOW)
    assert refresh.run(later(2)) is first


def test_a_late_departure_stays_until_the_feed_s_time(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.answers["departures"] = [{"stop_id": "S1", "departure": [
        leaving(1, realtime=5), leaving(2, delay=240), leaving(20)]}]
    refresh.run(NOW)
    kept = refresh.run(later(4))["local_stops_next_departures"][0]["departure"]
    assert [d["trip_id"] for d in kept] == ["T1", "T2", "T20"]
    # T1 leaves at 5 by the feed, T2 at 6 by its delay of 4 minutes
    kept = refresh.run(later(5))["local_stops_next_departures"][0]["departure"]
    assert [d["trip_id"] for d in kept] == ["T2", "T20"]
    kept = refresh.run(later(6))["local_stops_next_departures"][0]["departure"]
    assert [d["trip_id"] for d in kept] == ["T20"]


def test_the_walk_to_the_stop_counts(tmp_path):
    # an offset of 3 minutes: a departure 4 minutes away is gone for a
    # rider 2 minutes later, as the reading would have said
    refresh = Refresh(tmp_path, options={"offset": 3})
    refresh.answers["departures"] = [{"stop_id": "S1", "departure": [leaving(6), leaving(20)]}]
    refresh.run(NOW)
    kept = refresh.run(later(4))["local_stops_next_departures"][0]["departure"]
    assert [d["trip_id"] for d in kept] == ["T20"]


def test_a_database_swapped_in_is_read_at_once(tmp_path):
    refresh = Refresh(tmp_path)
    refresh.run(NOW)
    refresh.answers["get_gtfs"] = types.SimpleNamespace(session=None)
    refresh.run(later(1))
    assert len(refresh.calls["get_local_stops_next_departures"]) == 2
