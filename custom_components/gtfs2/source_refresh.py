"""Auto refresh of the sources' static feeds: look on a schedule, act per mode.

The datasource entry carries the choice, per source:

- off: nothing runs by itself. The update entity stays a manual button and
  the update service keeps doing what it always did.
- notify: one conditional request at the check hour. When the feed changed,
  the update entity turns "update available", a notification says so and an
  event fires, so an automation can install in whatever window suits the
  install. Installing stays a human's or an automation's decision.
- auto: same check, and the install runs by itself at the first check that
  finds a change.

The user chooses a frequency, never a moment: the moment is derived here,
at night in the instance's own timezone for daily and slower rhythms, and
staggered per source so rebuilds spread out. The schedule decides when to
look, the validators decide whether anything happens, so a week without a
new version costs one request. Only sources fetched from a url can be
checked: a zip source has no host to ask.

Two files carry the state, both disposable: the zip's sidecar records what
was downloaded (written by the download path itself), and a matching
sidecar beside the sqlite records what the database was last built from,
written here after a successful refresh. The gap between the two is what
"update available" means when the feed had to be fetched to know.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import timedelta

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change

from .const import (
    DOMAIN,
    DEFAULT_PATH,
    CONF_API_KEY,
    CONF_API_KEY_LOCATION,
    CONF_API_KEY_NAME,
    CONF_EXTRACT_FROM,
    CONF_FILE,
    CONF_URL,
    CONF_STATIC_CHECK_INTERVAL,
    CONF_STATIC_REFRESH_MODE,
    DEFAULT_STATIC_CHECK_INTERVAL,
    MAX_STATIC_CHECK_INTERVAL,
    MIN_STATIC_CHECK_INTERVAL,
    STATIC_REFRESH_AUTO,
    STATIC_REFRESH_NOTIFY,
    STATIC_REFRESH_OFF,
)
from .freshness import (
    PROBE_CHANGED,
    PROBE_UNCHANGED,
    PROBE_UNKNOWN,
    fetch_if_new,
    probe_source,
)
from .gtfs_helper import _async_notify, refresh_datasource, source_meta
from .rt_source import journey_entries

_LOGGER = logging.getLogger(__name__)

# fired towards automations when a source in notify mode has a new version
EVENT_SOURCE_UPDATE_AVAILABLE = "gtfs2_source_update_available"
# per-source dispatcher signal: something about the static feed moved,
# re-read the sidecars
SIGNAL_SOURCE_REFRESH = "gtfs2_source_refresh_{}"

# the flags a refresh honours that live on the journey entries, kept there
# for upstream compatibility; the identity keys come from the datasource
# entry itself, and the api trio is collected apart so it stays coherent
_JOURNEY_REFRESH_KEYS = (
    "check_source_dates",
    "clean_feed_info",
)


def _store(hass: HomeAssistant) -> dict:
    return hass.data.setdefault(DOMAIN, {})


def source_lock(hass: HomeAssistant, file) -> asyncio.Lock:
    """One lock per source: never two rebuilds of the same feed at once."""
    locks = _store(hass).setdefault("source_locks", {})
    if file not in locks:
        locks[file] = asyncio.Lock()
    return locks[file]


def probe_state(hass: HomeAssistant, file) -> dict:
    """What the last check learned, per source. Ephemeral on purpose: it is
    re-derivable at the next tick, so it lives in memory and a restart just
    means unknown-latest until the first check."""
    states = _store(hass).setdefault("source_probe_state", {})
    return states.setdefault(file, {})


def default_check_time(file) -> tuple[int, int, int]:
    """A night slot of the source's own, between 03:00 and 05:59 local.

    Derived from the file name, so it is stable across restarts and spreads
    the sources out without anyone choosing anything: rebuilds follow the
    publisher's rhythm without ever piling onto the same minute.
    """
    n = int(hashlib.sha256(str(file).encode()).hexdigest(), 16)
    return 3 + n % 3, (n // 3) % 60, (n // 180) % 60


def check_interval(entry: ConfigEntry) -> int:
    """The configured check frequency in hours, held to its bounds."""
    try:
        value = int(entry.options.get(CONF_STATIC_CHECK_INTERVAL,
                                      DEFAULT_STATIC_CHECK_INTERVAL))
    except (TypeError, ValueError):
        value = DEFAULT_STATIC_CHECK_INTERVAL
    return max(MIN_STATIC_CHECK_INTERVAL,
               min(MAX_STATIC_CHECK_INTERVAL, value))


def check_hours(interval: int, file) -> tuple[list[int], int, int]:
    """The local hours a source is looked at, for the frequency it asked.

    The user picks a frequency, the moment is picked here. The anchor is
    the source's own staggered night slot; a sub-daily frequency adds
    passes through the day, spaced by the interval from that anchor, so
    one look always lands at night. Daily and slower frequencies keep the
    single night slot, and how many nights go by between two looks is the
    elapsed-time gate's business, not the clock pattern's.
    """
    hour, minute, second = default_check_time(file)
    if interval < 24:
        hours = sorted({(hour + k * interval) % 24
                        for k in range((23 // interval) + 1)})
    else:
        hours = [hour]
    return hours, minute, second


def next_check_at(hass: HomeAssistant, entry: ConfigEntry,
                  fallback_last=None):
    """When the next scheduled look at this source is due, or None.

    The tick pattern is check_hours'; for the cadences slower than daily the
    elapsed-time gate of async_check_source decides which of those ticks
    counts, so the same gate is replayed here rather than promising a look
    that would be skipped. The caller passes the zip's downloaded_at as
    fallback_last, exactly as the gate falls back on it, because this runs
    on the loop and must not read a file to find out.

    Indicative to the hour across a DST boundary: the real schedule is
    async_track_time_change's, which handles the fold itself.
    """
    mode = entry.options.get(CONF_STATIC_REFRESH_MODE, STATIC_REFRESH_OFF)
    if mode == STATIC_REFRESH_OFF:
        return None
    if entry.data.get(CONF_EXTRACT_FROM, "url") != "url":
        # a zip source has no host to ask, so nothing is ever scheduled
        return None
    file = entry.data.get(CONF_FILE)
    interval = check_interval(entry)
    hours, minute, second = check_hours(interval, file)
    now = dt_util.now()
    earliest = now
    if interval > 24:
        last = probe_state(hass, file).get("checked_at") or fallback_last
        last_dt = dt_util.parse_datetime(last) if last else None
        if last_dt:
            earliest = max(
                now, dt_util.as_local(last_dt + timedelta(hours=interval - 12)))
    # the pattern repeats daily, so the first eligible tick is days away at
    # most as many days as the slowest interval allows
    for day in range((MAX_STATIC_CHECK_INTERVAL // 24) + 2):
        base = (now + timedelta(days=day)).replace(
            minute=minute, second=second, microsecond=0)
        for hour in hours:
            tick = base.replace(hour=hour)
            if tick > now and tick >= earliest:
                return tick
    return None


def _zip_path(hass: HomeAssistant, file) -> str:
    return os.path.join(hass.config.path(DEFAULT_PATH), file + ".zip")


def _installed_meta_path(hass: HomeAssistant, file) -> str:
    return os.path.join(hass.config.path(DEFAULT_PATH), file + ".sqlite.meta.json")


def installed_meta(hass: HomeAssistant, file) -> dict:
    """What the database was last built from.

    Falls back on the zip's sidecar when the build was never recorded: the
    flow's initial import and upstream's legacy paths build straight from
    the zip they just fetched, so zip and database start out as the same
    version.
    """
    try:
        with open(_installed_meta_path(hass, file), encoding="utf-8") as meta_file:
            meta = json.load(meta_file)
        if isinstance(meta, dict):
            return meta
    except (OSError, ValueError):
        pass
    return source_meta(_zip_path(hass, file))


def _record_installed(hass: HomeAssistant, file) -> None:
    """After a successful rebuild, the database is what the zip is."""
    meta = dict(source_meta(_zip_path(hass, file)))
    meta["built_at"] = dt_util.utcnow().isoformat()
    try:
        with open(_installed_meta_path(hass, file), "w", encoding="utf-8") as out:
            json.dump(meta, out, indent=1)
    except OSError as ex:
        _LOGGER.warning("Could not record the rebuild of %s: %s", file, ex)


def version_label(meta: dict):
    """A human answer to "which version is this", best evidence first."""
    if not meta:
        return None
    etag = (meta.get("etag") or "").removeprefix("W/").strip('"')
    return (meta.get("last_modified") or etag[:12] or
            (meta.get("sha256") or "")[:12] or meta.get("downloaded_at"))


def refresh_source(hass: HomeAssistant, path, data) -> bool:
    """The refresh itself plus its record, synchronous for executor jobs.

    Success means the swap rebuild delivered: only then is the database
    known to be what the zip is. The legacy fallback spawns an unpack and
    reports "extracting", which is not yet a built database, so it is not
    recorded either; its import writes the same files the fallback path
    always did.
    """
    result = refresh_datasource(hass, path, data)
    if isinstance(result, dict):
        _record_installed(hass, data.get(CONF_FILE) or data.get("file"))
        return True
    return result not in (False, "no_data_file", "no_zip_file")


def refresh_data_for(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """What refresh_datasource needs, assembled from the source's entries.

    The datasource entry owns the identity; the per-config flags and the
    static api key still live on the journey entries for upstream
    compatibility, so the first journey entry that carries one speaks for
    the source.
    """
    data = {
        CONF_FILE: entry.data.get(CONF_FILE),
        CONF_URL: entry.data.get(CONF_URL),
        CONF_EXTRACT_FROM: entry.data.get(CONF_EXTRACT_FROM, "url"),
    }
    for journey in journey_entries(hass, data[CONF_FILE]):
        # the api trio travels together, from the first entry that actually
        # carries a key: every flow stores api_key_location (usually
        # "not_applicable"), so taking the fields one by one would let a
        # keyless entry strip a later keyed entry's authentication
        if CONF_API_KEY not in data and journey.data.get(CONF_API_KEY):
            data[CONF_API_KEY] = journey.data[CONF_API_KEY]
            data[CONF_API_KEY_LOCATION] = journey.data.get(CONF_API_KEY_LOCATION)
            data[CONF_API_KEY_NAME] = journey.data.get(CONF_API_KEY_NAME)
        for key in _JOURNEY_REFRESH_KEYS:
            if key not in data and key in journey.data:
                data[key] = journey.data[key]
    return data


async def async_refresh_source(hass: HomeAssistant, entry: ConfigEntry,
                               *, use_zip: bool = False) -> bool:
    """Refresh one source, serialised per source, and tell its entities.

    use_zip says the fresh feed already sits in the kept zip (a check that
    had to download to know), so the rebuild reads it instead of
    downloading the same bytes again.
    """
    file = entry.data.get(CONF_FILE)
    lock = source_lock(hass, file)
    if lock.locked():
        _LOGGER.info("A refresh of %s is already running", file)
        return False
    data = refresh_data_for(hass, entry)
    if use_zip:
        data[CONF_EXTRACT_FROM] = "zip"
    async with lock:
        ok = await hass.async_add_executor_job(
            refresh_source, hass, DEFAULT_PATH, data)
    async_dispatcher_send(hass, SIGNAL_SOURCE_REFRESH.format(file))
    return ok


async def async_check_source(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """One scheduled look at a source's host, then whatever the mode says."""
    mode = entry.options.get(CONF_STATIC_REFRESH_MODE, STATIC_REFRESH_OFF)
    if mode == STATIC_REFRESH_OFF:
        return
    if entry.data.get(CONF_EXTRACT_FROM, "url") != "url":
        return
    file = entry.data.get(CONF_FILE)
    if source_lock(hass, file).locked():
        # a rebuild is running right now; next tick will know more
        return
    data = refresh_data_for(hass, entry)
    zip_path = _zip_path(hass, file)
    interval = check_interval(entry)
    if interval > 24:
        # slower than daily: the tick still fires every night, this gate
        # says which nights count. The 12 hour slack keeps a cadence from
        # drifting past its own slot, and a lost record just means one
        # extra conditional request, not a download
        last = probe_state(hass, file).get("checked_at") or (
            await hass.async_add_executor_job(source_meta, zip_path)
        ).get("downloaded_at")
        last_dt = dt_util.parse_datetime(last) if last else None
        if last_dt and dt_util.utcnow() - last_dt < timedelta(hours=interval - 12):
            return
    probe = await hass.async_add_executor_job(probe_source, data, zip_path)
    state = probe_state(hass, file)
    state["checked_at"] = dt_util.utcnow().isoformat()
    state["result"] = probe["result"]
    # through version_label, like the installed side: a raw W/"..." etag
    # would never equal the installed label and read as a phantom update
    # on the hosts that publish no Last-Modified
    state["latest"] = version_label({
        "last_modified": probe.get("last_modified"),
        "etag": probe.get("etag"),
    })

    changed = probe["result"] == PROBE_CHANGED
    use_zip = False
    if probe["result"] == PROBE_UNKNOWN:
        # the host publishes no validators, only the download can answer;
        # when it does turn out new, the fetched feed is kept in the zip so
        # nothing is downloaded twice
        fetched = await hass.async_add_executor_job(fetch_if_new, data, zip_path)
        if fetched is True:
            changed, use_zip = True, True
            state["result"] = PROBE_CHANGED
            state["latest"] = version_label(
                await hass.async_add_executor_job(source_meta, zip_path))
        elif fetched is False:
            state["result"] = PROBE_UNCHANGED
    async_dispatcher_send(hass, SIGNAL_SOURCE_REFRESH.format(file))
    if not changed:
        return

    if mode == STATIC_REFRESH_AUTO:
        _LOGGER.info("Source %s has a new version, refreshing it", file)
        await async_refresh_source(hass, entry, use_zip=use_zip)
        return

    latest = state.get("latest") or "new version"
    if state.get("notified_for") == latest:
        # the same version stays one notification, not one per check
        return
    state["notified_for"] = latest
    _LOGGER.info("Source %s has a new version: %s", file, latest)
    installed = version_label(
        await hass.async_add_executor_job(installed_meta, hass, file))
    hass.bus.async_fire(EVENT_SOURCE_UPDATE_AVAILABLE, {
        "file": file,
        "installed": installed,
        "latest": latest,
    })
    await _async_notify(hass, "source_update_available",
                        f"gtfs2_source_update_{file}",
                        file=file, version=latest)


@callback
def async_arm_source_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give a source its scheduled check, or take it away, per its options."""
    unsubs = _store(hass).setdefault("source_check_unsub", {})
    previous = unsubs.pop(entry.entry_id, None)
    if previous:
        previous()
    mode = entry.options.get(CONF_STATIC_REFRESH_MODE, STATIC_REFRESH_OFF)
    if mode not in (STATIC_REFRESH_NOTIFY, STATIC_REFRESH_AUTO):
        return
    if entry.data.get(CONF_EXTRACT_FROM, "url") != "url":
        _LOGGER.info("Source %s is fed from a zip, there is no host to ask "
                     "about new versions", entry.data.get(CONF_FILE))
        return
    interval = check_interval(entry)
    hours, minute, second = check_hours(interval, entry.data.get(CONF_FILE))

    async def _tick(now):
        await async_check_source(hass, entry)

    unsubs[entry.entry_id] = async_track_time_change(
        hass, _tick, hour=hours, minute=minute, second=second)
    _LOGGER.debug(
        "Source %s checks for new versions every %d h, at minute %02d:%02d "
        "of hours %s (%s)",
        entry.data.get(CONF_FILE), interval, minute, second, hours, mode)


async def async_rearm_source_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """The update listener shape of arming: options changed, follow them."""
    async_arm_source_check(hass, entry)


@callback
def async_disarm_source_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    unsub = _store(hass).setdefault("source_check_unsub", {}).pop(entry.entry_id, None)
    if unsub:
        unsub()
