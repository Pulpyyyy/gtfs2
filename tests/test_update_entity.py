"""The update entity of a source: its rebuild in progress, whatever started
it, and its versions read as labels.

Home Assistant reads in_progress only from an entity that declares
UpdateEntityFeature.PROGRESS, and otherwise shows a rebuild only while it
runs its own install: the night check and the update service rebuilt the
source with the entity saying nothing. The entity declares it, follows the
source's lock, and is told when a rebuild starts as well as when it ends.

Its versions are a Last-Modified date, an etag or a hash prefix. Home
Assistant orders them with AwesomeVersion once they differ, which reads an
all-digit hash as a number: a new feed whose hash was smaller read as up
to date. Any other version is the newer one.

Installed is what the database was last built from, read off its sidecar,
or off the kept zip's when no build was ever recorded, which the entity
says. Latest is what the last check learned; with the checks off, or
before any check spoke, the entity claims nothing beyond the installed
version, unless the kept zip is already ahead. What the last check learned
lives in memory, so the entity's saved state seeds it back after a
restart, without overriding a check that spoke since. Install rebuilds
from the kept zip when it is ahead, and says when the rebuild failed.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import types
import zipfile

import pytest

import ha_stub

update = ha_stub.load("update")
source_refresh = ha_stub.load("source_refresh")
key_mask = ha_stub.load("key_mask")


class _Hass:
    def __init__(self, root=None):
        self.data = {}
        if root is not None:
            self.config = types.SimpleNamespace(path=lambda *p: str(root.joinpath(*p)))

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _entity(hass, **options):
    entry = types.SimpleNamespace(data={"file": "src", "kind": "datasource"},
                                  options=options, entry_id="e1")
    return update.GTFSSourceUpdateEntity(hass, entry)


def _source(root, installed=None, kept=None, feed=True):
    """A source on disk: the database's record, the zip's, the zip itself."""
    folder = root / "gtfs2"
    folder.mkdir(exist_ok=True)
    if installed is not None:
        (folder / "src.sqlite.meta.json").write_text(json.dumps(installed))
    if kept is not None:
        (folder / "src.zip.meta.json").write_text(json.dumps(kept))
    if feed:
        with zipfile.ZipFile(folder / "src.zip", "w") as archive:
            archive.writestr("feed_info.txt", "feed_publisher_name,feed_publisher_url,"
                             "feed_lang,feed_version\nTAO,https://tao,fr,v42\n")
            archive.writestr("calendar_dates.txt",
                             "service_id,date,exception_type\nS,20260930,1\n")


def _loaded(root, **options):
    entity = _entity(_Hass(root), **options)
    asyncio.run(entity.async_load_versions())
    return entity


BUILT = {"last_modified": "Fri, 19 Sep 2026 19:30:00 GMT",
         "built_at": "2026-09-19T20:00:00+00:00",
         "downloaded_at": "2026-09-19T19:45:00+00:00"}
NEWER = {"last_modified": "Sat, 20 Sep 2026 19:30:00 GMT",
         "downloaded_at": "2026-09-20T19:45:00+00:00"}


def test_the_entity_declares_its_progress():
    features = update.GTFSSourceUpdateEntity._attr_supported_features
    assert features & update.UpdateEntityFeature.PROGRESS
    assert features & update.UpdateEntityFeature.INSTALL


def test_in_progress_follows_the_source_lock():
    async def run():
        hass = _Hass()
        entity = _entity(hass)
        assert entity.in_progress is False
        lock = source_refresh.source_lock(hass, "src")
        async with lock:
            # held by a rebuild the entity did not start
            assert entity.in_progress is True
        assert entity.in_progress is False
    asyncio.run(run())


def test_a_rebuild_is_told_when_it_starts(monkeypatch):
    """The signal goes out with the lock held, so the entity writes a state
    that reads in progress, and once more after it is released."""
    hass = _Hass()
    seen = []

    def send(hass_, signal):
        seen.append((signal, source_refresh.source_lock(hass, "src").locked()))

    async def notify(*args):
        return None

    monkeypatch.setattr(source_refresh, "async_dispatcher_send", send)
    monkeypatch.setattr(source_refresh, "async_notify_refresh", notify)
    monkeypatch.setattr(source_refresh, "_lines_read", lambda hass, file: [])
    monkeypatch.setattr(source_refresh, "_reads_whole_feed", lambda hass, file: False)
    monkeypatch.setattr(source_refresh, "refresh_source", lambda hass, path, data: True)
    assert asyncio.run(source_refresh.async_refresh_source_data(hass, "src", {"file": "src"}))
    signal = source_refresh.SIGNAL_SOURCE_REFRESH.format("src")
    assert seen == [(signal, True), (signal, False)]


def test_any_other_version_is_newer():
    entity = _entity(_Hass())
    for latest, installed in [
            ("098765432109", "123456789012"),     # all-digit hash prefixes
            ("1726800000", "1726900000"),         # a numeric etag, smaller
            ("Sat, 20 Sep 2026 19:30:00 GMT", "Fri, 19 Sep 2026 19:30:00 GMT"),
            ("5a0f00c1d2e3", "66e1f2a31b2c")]:
        assert entity.version_is_newer(latest, installed), (latest, installed)


def test_only_a_datasource_entry_carries_the_entity(tmp_path):
    _source(tmp_path, installed=BUILT)
    added = []
    journey = types.SimpleNamespace(data={"file": "src"}, options={}, entry_id="j1")
    datasource = types.SimpleNamespace(data={"file": "src", "kind": "datasource"},
                                       options={}, entry_id="d1")
    for entry in (journey, datasource):
        asyncio.run(update.async_setup_entry(_Hass(tmp_path), entry, added.extend))
    assert [e._attr_unique_id for e in added] == ["gtfs2_source_update_src"]
    # added with its versions already read
    assert added[0].installed_version == BUILT["last_modified"]


def test_the_versions_are_read_off_the_sidecars(tmp_path):
    key_mask.note_key("test-update-entity-key")
    installed = {**BUILT, "url": "https://tao/gtfs.zip?key=test-update-entity-key", "size": 1234}
    _source(tmp_path, installed=installed, kept=NEWER)
    entity = _loaded(tmp_path)
    assert entity.installed_version == BUILT["last_modified"]
    attributes = entity.extra_state_attributes
    assert attributes["zip_version"] == NEWER["last_modified"]
    assert attributes["version_source"] == "recorded"
    assert attributes["built_at"] == BUILT["built_at"]
    # the key never shows, even from a record written before the mask
    assert attributes["source_url"] == "https://tao/gtfs.zip?key=" + key_mask.KEY_MASK
    assert attributes["source_size"] == 1234
    # what the kept feed says of itself
    assert attributes["feed_version"] == "v42"
    assert attributes["last_service_day"] == "2026-09-30"
    # the checks are off: nothing is scheduled
    assert attributes["refresh_mode"] == "off" and attributes["next_check"] is None


def test_a_database_never_recorded_is_read_off_the_zip(tmp_path):
    _source(tmp_path, kept=NEWER, feed=False)
    entity = _loaded(tmp_path)
    assert entity.installed_version == NEWER["last_modified"]
    attributes = entity.extra_state_attributes
    assert attributes["version_source"] == "assumed"
    assert attributes["source_url"] is None
    assert attributes["feed_version"] is None


def test_a_source_on_a_schedule_says_when_it_looks_next(tmp_path):
    _source(tmp_path, installed=BUILT)
    entity = _loaded(tmp_path, static_refresh_mode="notify", static_check_interval=6)
    attributes = entity.extra_state_attributes
    assert attributes["check_interval"] == 6
    assert attributes["next_check"] is not None


def test_latest_claims_nothing_beyond_what_is_known(tmp_path):
    _source(tmp_path, installed=BUILT, kept=BUILT)
    for options in ({}, {"static_refresh_mode": "notify"}):
        # checks off, or no check spoke yet: the installed version
        assert _loaded(tmp_path, **options).latest_version == BUILT["last_modified"]
    entity = _loaded(tmp_path, static_refresh_mode="auto")
    source_refresh.probe_state(entity.hass, "src")["latest"] = "abc123"
    assert entity.latest_version == "abc123"
    # with the checks off, even a known latest is not claimed
    entity._entry.options = {}
    assert entity.latest_version == BUILT["last_modified"]


def test_a_zip_ahead_of_the_database_is_the_latest(tmp_path):
    _source(tmp_path, installed=BUILT, kept=NEWER)
    entity = _loaded(tmp_path, static_refresh_mode="notify")
    assert entity.latest_version == NEWER["last_modified"]


@pytest.fixture
def utc():
    """The stamps read as local minutes: in UTC here, whatever zone a test
    before this one left as the default."""
    before = update.dt_util.DEFAULT_TIME_ZONE
    update.dt_util.set_default_time_zone(datetime.timezone.utc)
    yield
    update.dt_util.set_default_time_zone(before)


def test_the_release_summary_says_what_the_database_is(tmp_path, utc):
    _source(tmp_path, installed=BUILT)
    assert _loaded(tmp_path).release_summary == (
        "Database built 2026-09-19 20:00, from a feed downloaded 2026-09-19 19:45")
    _source(tmp_path, installed={"etag": '"abc"'})
    assert _loaded(tmp_path).release_summary == (
        "Database version assumed from the kept zip: no build was ever recorded")
    # no sidecar at all, nothing to say
    (tmp_path / "gtfs2" / "src.sqlite.meta.json").unlink()
    assert _loaded(tmp_path).release_summary is None


def test_an_unreadable_stamp_reads_as_none(utc):
    assert update._when(None) is None
    assert update._when("not a date") is None
    assert update._when("2026-09-19T20:00:00+00:00") == "2026-09-19 20:00"


def _added(entity, last, monkeypatch):
    connected = []

    async def last_state():
        return last

    def connect(hass, signal, target):
        connected.append((signal, target))
        return "disconnect"

    monkeypatch.setattr(update, "async_dispatcher_connect", connect)
    entity.async_get_last_state = last_state
    asyncio.run(entity.async_added_to_hass())
    return connected


def test_a_restart_keeps_the_last_verdict(tmp_path, monkeypatch):
    _source(tmp_path, installed=BUILT)
    entity = _loaded(tmp_path, static_refresh_mode="notify")
    # a check spoke already this run: its word stands
    source_refresh.probe_state(entity.hass, "src")["checked_at"] = "now"
    last = types.SimpleNamespace(attributes={
        "latest_version": "abc123", "last_check": "before",
        "last_check_result": "new_version"})
    connected = _added(entity, last, monkeypatch)
    assert source_refresh.probe_state(entity.hass, "src") == {
        "checked_at": "now", "latest": "abc123", "result": "new_version"}
    assert entity.latest_version == "abc123"
    # told of every rebuild of its source, until it goes
    assert connected == [(update.SIGNAL_SOURCE_REFRESH.format("src"),
                          entity._async_source_moved)]
    assert entity._on_remove == ["disconnect"]


def test_a_first_start_has_no_verdict_to_keep(tmp_path, monkeypatch):
    _source(tmp_path, installed=BUILT)
    entity = _loaded(tmp_path)
    _added(entity, None, monkeypatch)
    assert source_refresh.probe_state(entity.hass, "src") == {}


def test_a_rebuild_is_read_back_and_written(tmp_path):
    _source(tmp_path, installed=BUILT, kept=BUILT)
    entity = _loaded(tmp_path)
    writes = []
    entity.async_write_ha_state = lambda: writes.append(entity.installed_version)
    _source(tmp_path, installed={**NEWER, "built_at": "2026-09-20T20:00:00+00:00"})
    asyncio.run(entity._async_source_moved())
    assert writes == [NEWER["last_modified"]]


def _install(tmp_path, monkeypatch, kept, ok):
    _source(tmp_path, installed=BUILT, kept=kept)
    entity = _loaded(tmp_path, static_refresh_mode="notify")
    calls, writes = [], []

    async def refresh(hass, entry, *, use_zip=False):
        calls.append(use_zip)
        return ok

    monkeypatch.setattr(update, "async_refresh_source", refresh)
    entity.async_write_ha_state = lambda: writes.append(True)
    return entity, calls, writes


def test_install_rebuilds_from_the_zip_only_when_it_is_ahead(tmp_path, monkeypatch):
    for kept, from_zip in ((NEWER, True), (BUILT, False)):
        entity, calls, writes = _install(tmp_path, monkeypatch, kept, True)
        asyncio.run(entity.async_install(None, False))
        assert calls == [from_zip]
        assert writes == [True]


def test_a_failed_install_says_so(tmp_path, monkeypatch):
    entity, calls, writes = _install(tmp_path, monkeypatch, NEWER, False)
    with pytest.raises(update.HomeAssistantError, match="failed"):
        asyncio.run(entity.async_install("v", False))
    assert calls == [True] and writes == []
