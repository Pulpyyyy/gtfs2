"""What one scheduled look at a source does, per mode and per answer.

async_check_source is the only caller that turns the host's answers into
a rebuild or a notification by itself, at night, with nobody watching.
Each path is pinned here: the modes and sources that never look, the
slow cadences that skip a night, a probe that could not ask, a feed the
download proved unchanged or new, and what notify mode tells the user,
once per version.
"""
from __future__ import annotations

import asyncio
import datetime
import types

import ha_stub

source_refresh = ha_stub.load("source_refresh")
dt_util = source_refresh.dt_util

AUTO = source_refresh.STATIC_REFRESH_AUTO
NOTIFY = source_refresh.STATIC_REFRESH_NOTIFY
OFF = source_refresh.STATIC_REFRESH_OFF
CHANGED = source_refresh.PROBE_CHANGED
UNCHANGED = source_refresh.PROBE_UNCHANGED
UNKNOWN = source_refresh.PROBE_UNKNOWN
ERROR = source_refresh.PROBE_ERROR


def _probe(result, last_modified=None, etag=None):
    return {"result": result, "last_modified": last_modified, "etag": etag}


def _entry(mode, extract_from="url", interval=None):
    options = {"static_refresh_mode": mode}
    if interval is not None:
        options["static_check_interval"] = interval
    return types.SimpleNamespace(
        entry_id="e1", options=options,
        data={"file": "src", "extract_from": extract_from, "url": "https://h/src.zip"})


def _hass(calls, taken_by_probe=False):
    """A hass whose executor runs inline; the lock can be taken meanwhile,
    as a refresh started while the host was being asked would take it."""
    hass = types.SimpleNamespace(data={})

    async def job(fn, *args):
        answer = fn(*args)
        if taken_by_probe and fn is source_refresh.probe_source:
            await source_refresh.source_lock(hass, "src").acquire()
        return answer

    hass.async_add_executor_job = job
    hass.bus = types.SimpleNamespace(
        async_fire=lambda event, payload: calls.append(("event", event, payload)))
    return hass


def _stub(monkeypatch, calls, *, probe, fetched=None, pending=False, rebuilt=True,
          last=None, meta=None):
    async def rebuild(hass, entry, *, use_zip=False, flags=None):
        calls.append(("refresh", use_zip))
        return rebuilt

    def fetch_if_new(data, zip_path, adopt=True):
        calls.append(("fetch", adopt))
        return fetched

    async def notify(hass, key, notification_id, **values):
        calls.append(("notify", key, notification_id, values))

    monkeypatch.setattr(source_refresh, "rebuild_pending", lambda hass, file: pending)
    monkeypatch.setattr(source_refresh, "async_refresh_source", rebuild)
    monkeypatch.setattr(source_refresh, "refresh_data_for", lambda hass, entry: {"file": "src"})
    monkeypatch.setattr(source_refresh, "probe_source",
                        lambda data, zip_path: calls.append(("probe",)) or probe)
    monkeypatch.setattr(source_refresh, "note_checked",
                        lambda zip_path: calls.append(("noted",)))
    monkeypatch.setattr(source_refresh, "_zip_path", lambda hass, file: "src.zip")
    monkeypatch.setattr(source_refresh, "fetch_if_new", fetch_if_new)
    monkeypatch.setattr(source_refresh, "source_meta", lambda zip_path: meta or {})
    monkeypatch.setattr(source_refresh, "_carry_validators",
                        lambda hass, file: calls.append(("carried",)))
    monkeypatch.setattr(source_refresh, "last_look", lambda hass, file: last)
    monkeypatch.setattr(source_refresh, "installed_meta",
                        lambda hass, file: {"last_modified": "OLD"})
    monkeypatch.setattr(source_refresh, "async_dispatcher_send",
                        lambda hass, signal: calls.append(("told",)))
    monkeypatch.setattr(source_refresh, "_async_notify", notify)


def _check(hass, entry, lock_first=False):
    async def run():
        if lock_first:
            await source_refresh.source_lock(hass, "src").acquire()
        await source_refresh.async_check_source(hass, entry)
    asyncio.run(run())
    return source_refresh.probe_state(hass, "src")


# --- the looks that never happen ---------------------------------------------

def test_off_never_looks(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED))
    _check(_hass(calls), _entry(OFF))
    assert calls == []


def test_a_zip_source_has_no_host_to_ask(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED))
    _check(_hass(calls), _entry(AUTO, extract_from="zip"))
    assert calls == []


def test_a_rebuild_running_puts_the_look_off(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED))
    _check(_hass(calls), _entry(AUTO), lock_first=True)
    assert calls == []


# --- the cadences slower than daily ------------------------------------------

def _ago(hours):
    return dt_util.utcnow() - datetime.timedelta(hours=hours)


def test_a_slow_cadence_skips_a_night_looked_at_recently(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(UNCHANGED), last=_ago(30))
    _check(_hass(calls), _entry(AUTO, interval=72))
    assert calls == []


def test_a_slow_cadence_looks_once_its_interval_went_by(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(UNCHANGED), last=_ago(70))
    _check(_hass(calls), _entry(AUTO, interval=72))
    assert ("probe",) in calls


def test_a_slow_cadence_with_no_record_looks(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(UNCHANGED), last=None)
    _check(_hass(calls), _entry(AUTO, interval=72))
    assert ("probe",) in calls


# --- what the probe answered -------------------------------------------------

def test_a_host_that_could_not_be_asked_leaves_no_record_of_a_look(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(ERROR))
    state = _check(_hass(calls), _entry(AUTO))
    # no look kept for the catch-up, nothing downloaded, nothing rebuilt
    assert calls == [("probe",), ("told",)]
    assert state["result"] == ERROR and state["latest"] is None


def test_an_unchanged_feed_is_left_alone(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(UNCHANGED, etag='W/"abcdef0123456789"'))
    state = _check(_hass(calls), _entry(AUTO))
    assert calls == [("probe",), ("noted",), ("told",)]
    # named like the installed side, the weak marker and quotes gone
    assert state["result"] == UNCHANGED and state["latest"] == "abcdef012345"
    assert state["checked_at"]


# --- what the download proved, auto mode -------------------------------------

def test_a_new_feed_is_kept_and_built_from_the_zip(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED, last_modified="PROBED"),
          fetched=True, meta={"last_modified": "DOWNLOADED"})
    state = _check(_hass(calls), _entry(AUTO))
    assert calls == [("probe",), ("noted",), ("fetch", True), ("told",), ("refresh", True)]
    assert state["result"] == CHANGED and state["latest"] == "DOWNLOADED"


def test_the_same_bytes_are_no_change(monkeypatch):
    # a host stamping a fresh Last-Modified on every answer: the download
    # says the feed is the same, and the record takes the new validators
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED, last_modified="PROBED"),
          fetched=False, meta={"last_modified": "KEPT"})
    state = _check(_hass(calls), _entry(AUTO))
    assert calls == [("probe",), ("noted",), ("fetch", True), ("carried",), ("told",)]
    assert state["result"] == UNCHANGED and state["latest"] == "KEPT"


def test_a_failed_download_leaves_the_hosts_word_standing(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED, last_modified="PROBED"), fetched=None)
    state = _check(_hass(calls), _entry(AUTO))
    # the rebuild downloads again itself, since nothing new sits in the zip
    assert calls[-1] == ("refresh", False)
    assert state["result"] == CHANGED and state["latest"] == "PROBED"


def test_a_failed_download_with_no_validators_changes_nothing(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(UNKNOWN), fetched=None)
    state = _check(_hass(calls), _entry(AUTO))
    assert calls == [("probe",), ("noted",), ("fetch", True), ("told",)]
    assert state["result"] == UNKNOWN


def test_a_refresh_started_during_the_look_keeps_the_download_off(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED), fetched=True)
    hass = _hass(calls, taken_by_probe=True)
    _check(hass, _entry(AUTO))
    assert calls == [("probe",), ("noted",)]


# --- notify mode ---------------------------------------------------------------

def test_notify_asks_without_adopting_and_says_so_once(monkeypatch):
    calls = []
    sha = "0123456789abcdef" * 4
    _stub(monkeypatch, calls, probe=_probe(UNKNOWN), fetched=sha)
    hass = _hass(calls)
    state = _check(hass, _entry(NOTIFY))
    assert ("fetch", False) in calls
    assert not any(call[0] == "refresh" for call in calls)
    # no validator to name the new edition by: its hash does
    assert state["result"] == CHANGED and state["latest"] == sha[:12]
    assert ("event", source_refresh.EVENT_SOURCE_UPDATE_AVAILABLE,
            {"file": "src", "installed": "OLD", "latest": sha[:12]}) in calls
    assert ("notify", "source_update_available", "gtfs2_source_update_src",
            {"file": "src", "version": sha[:12]}) in calls
    # the same version at the next check: one notification, not two
    calls.clear()
    _check(hass, _entry(NOTIFY))
    assert not any(call[0] in ("event", "notify") for call in calls)


def test_notify_names_a_new_edition_by_what_the_host_said(monkeypatch):
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED, last_modified="PROBED"),
          fetched="f" * 64)
    state = _check(_hass(calls), _entry(NOTIFY))
    assert state["latest"] == "PROBED"
    assert ("notify", "source_update_available", "gtfs2_source_update_src",
            {"file": "src", "version": "PROBED"}) in calls


def test_notify_with_nothing_to_name_it_by(monkeypatch):
    # no sidecar to compare with and a download that failed: the host's
    # word stands, with no version to show
    calls = []
    _stub(monkeypatch, calls, probe=_probe(CHANGED), fetched=None)
    _check(_hass(calls), _entry(NOTIFY))
    assert ("notify", "source_update_available", "gtfs2_source_update_src",
            {"file": "src", "version": "new version"}) in calls
