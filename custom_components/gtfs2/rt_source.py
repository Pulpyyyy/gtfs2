"""The datasource config entries: one per GTFS source, owning its realtime feeds.

A source used to exist only as files on disk plus N journey entries, each
carrying its own copy of the realtime urls and api key. The datasource entry
makes the source a real Home Assistant object: entry.data is the identity
(kind, file, url, extract_from), entry.options are the realtime feeds, and
the file name is the unique_id so a source can never have two.

The model is all-source: a sensor follows its source, and realtime is on for
every sensor of a source as soon as the source has a trip updates url. The
journey entries keep their old realtime options as a fallback, so an install
that predates the datasource entries, or downgrades to upstream, keeps its
realtime exactly as it was.
"""
from __future__ import annotations

import logging

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    DEFAULT_PATH,
    DEFAULT_API_KEY_NAME,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    CONF_FILE,
    CONF_URL,
    CONF_EXTRACT_FROM,
    CONF_REAL_TIME,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_ALERTS_URL,
    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_API_KEY_LOCATION,
    CONF_ACCEPT_HEADER_PB,
)

_LOGGER = logging.getLogger(__name__)

# what moves from the journey entries to the datasource entry: the feeds and
# their key. Refresh, offset and the local-stop knobs stay per sensor.
RT_OPTION_KEYS = (
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_ALERTS_URL,
    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_API_KEY_LOCATION,
    CONF_ACCEPT_HEADER_PB,
)


def datasource_entry(hass: HomeAssistant, file) -> ConfigEntry | None:
    """The datasource entry of a source, or None while it does not exist."""
    if not file:
        return None
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE
                and entry.data.get(CONF_FILE) == file):
            return entry
    return None


def journey_entries(hass: HomeAssistant, file) -> list[ConfigEntry]:
    """Every non-datasource entry reading this source, local stops included."""
    return [
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE
        and entry.data.get(CONF_FILE) == file
    ]


def rt_feed_config(hass: HomeAssistant, entry: ConfigEntry):
    """The realtime config an entry runs with, and whether realtime is on.

    The source's datasource entry is authoritative when it exists: its
    options hold the feeds, shared by every sensor of the source, and
    realtime is on as soon as a trip updates url is set - the per-sensor
    boolean does not apply any more. Without a datasource entry the entry's
    own options apply unchanged, which is what keeps an install working
    before the bootstrap ran, and after a downgrade.

    Read on every coordinator cycle, so an edit on the datasource entry
    reaches every sensor of the source within a minute, with no reload.
    """
    source = datasource_entry(hass, entry.data.get(CONF_FILE))
    if source is not None:
        return source.options, bool(source.options.get(CONF_TRIP_UPDATE_URL))
    return entry.options, bool(entry.options.get(CONF_REAL_TIME, False))


def with_query_key(url, cfg):
    """The url with the api key appended, when the key travels in the query.

    None-safe on purpose: the code this replaces concatenated onto every feed
    url whether it was set or not, which is a TypeError as soon as a source
    keeps its key in the query string and has no vehicle or alerts feed.
    """
    if not url:
        return None
    if cfg.get(CONF_API_KEY_LOCATION) == "query_string" and cfg.get(CONF_API_KEY):
        return (url + "?" + cfg.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME)
                + "=" + cfg[CONF_API_KEY])
    return url


def rt_headers(cfg):
    """The request headers of a feed whose key travels in a header, else None."""
    if cfg.get(CONF_API_KEY_LOCATION) != "header":
        return None
    headers = {cfg.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME): cfg.get(CONF_API_KEY)}
    if cfg.get(CONF_ACCEPT_HEADER_PB, False):
        headers["Accept"] = "application/x-protobuf"
    return headers


def _rt_seed(entries: list[ConfigEntry]) -> dict:
    """The realtime options to seed a new datasource entry with.

    Several journey entries can carry a copy and they can disagree: the most
    recently modified entry wins, and each loser whose config differed gets a
    log line naming what was left behind, so nothing disappears silently.
    """
    donors = [e for e in entries if e.options.get(CONF_TRIP_UPDATE_URL)]
    if not donors:
        return {}
    floor = dt_util.utc_from_timestamp(0)
    winner = max(donors, key=lambda e: getattr(e, "modified_at", None) or floor)
    seed = {k: winner.options[k] for k in RT_OPTION_KEYS if k in winner.options}
    for entry in donors:
        if entry is winner:
            continue
        differs = {
            k: entry.options.get(k) for k in RT_OPTION_KEYS
            if entry.options.get(k) != seed.get(k)
            and (entry.options.get(k) or seed.get(k))
        }
        if differs:
            # the key value itself never goes to the log
            if CONF_API_KEY in differs:
                differs[CONF_API_KEY] = "(differs, not shown)"
            _LOGGER.warning(
                "Datasource %s: realtime config of entry '%s' differs from the"
                " most recently modified one and was not kept: %s",
                winner.data.get(CONF_FILE), entry.title, differs)
    return seed


async def async_ensure_datasource_entry(
        hass: HomeAssistant, file, url=None, extract_from=None) -> None:
    """Create the datasource entry of a source, unless it already exists.

    Called by the bootstrap and by the flow steps that bring a new source in.
    Idempotent by construction: the file name is the unique_id, so a second
    creation aborts inside the import flow instead of duplicating.
    """
    if not file or datasource_entry(hass, file) is not None:
        return
    entries = journey_entries(hass, file)
    if url is None:
        # the entries carry the url the source was downloaded from; "na" is
        # what the zip-based paths store, and stays the last resort
        url = next((e.data.get(CONF_URL) for e in entries
                    if e.data.get(CONF_URL) not in (None, "", "na")), "na")
    if extract_from is None:
        extract_from = next((e.data.get(CONF_EXTRACT_FROM) for e in entries
                             if e.data.get(CONF_EXTRACT_FROM)), "zip")
    _LOGGER.info("Creating datasource entry for source: %s", file)
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_KIND: ENTRY_KIND_DATASOURCE,
            CONF_FILE: file,
            CONF_URL: url,
            CONF_EXTRACT_FROM: extract_from,
            # carried through the import step into entry.options, where
            # rt_feed_config will look for it
            "options": _rt_seed(entries),
        },
    )


async def async_bootstrap_datasource_entries(hass: HomeAssistant) -> None:
    """Give every known source its datasource entry, idempotently.

    Runs in the background at every start: the sources are collected from the
    disk and from the entries, and only the missing datasource entries are
    created. Deliberately never through async_migrate_entry - a per-entry
    migration of a per-source object is how a bootstrap ends half-done and
    unrepeatable, and the journey entries are not touched at all.
    """
    from .gtfs_helper import get_datasources

    files = set(await get_datasources(hass, DEFAULT_PATH))
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE
                and entry.data.get(CONF_FILE)):
            files.add(entry.data[CONF_FILE])
    for file in sorted(files):
        try:
            await async_ensure_datasource_entry(hass, file)
        except Exception as ex:  # pylint: disable=broad-except
            # one source failing must not keep the others from their entry
            _LOGGER.error("Could not create datasource entry for %s: %s", file, ex)


async def async_mirror_rt_to_entries(hass: HomeAssistant, source_entry: ConfigEntry) -> None:
    """Write the datasource's realtime options through to its journey entries.

    While the datasource entry exists the resolution never reads the journey
    copies - but a downgrade to upstream does. Mirroring on every edit keeps
    that exit working with current values instead of the ones frozen at
    bootstrap. The per-entry boolean follows the all-source rule: realtime is
    on wherever the source has a trip updates url.
    """
    cfg = source_entry.options
    active = bool(cfg.get(CONF_TRIP_UPDATE_URL))
    for entry in journey_entries(hass, source_entry.data.get(CONF_FILE)):
        new_options = {**entry.options}
        for key in RT_OPTION_KEYS:
            if key in cfg:
                new_options[key] = cfg[key]
            else:
                new_options.pop(key, None)
        new_options[CONF_REAL_TIME] = active
        if new_options != dict(entry.options):
            hass.config_entries.async_update_entry(entry, options=new_options)
