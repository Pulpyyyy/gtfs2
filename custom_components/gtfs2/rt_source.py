"""The datasource config entries: one per GTFS source, owning its realtime feeds.

A source used to exist only as files on disk plus N journey entries, each
carrying its own copy of the realtime urls and api key. The datasource entry
makes the source a real Home Assistant object: entry.data is the identity
(kind, file, url) plus the key the static feed is fetched
with, entry.options are the realtime feeds, and the file name makes the
unique_id (datasource_unique_id) so a source can never have two.

The model is all-source: a sensor follows its source, and realtime is on for
every sensor of a source as soon as the source has any realtime feed url. The
journey entries keep their old realtime options as a fallback, so an install
that predates the datasource entries, or downgrades to upstream, keeps its
realtime exactly as it was.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import Any
from urllib.parse import quote

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import (
    id_of,
    DOMAIN,
    DEFAULT_API_KEY_LOCATION,
    DEFAULT_API_KEY_NAME,
    CONF_KIND,
    ENTRY_KIND_DATASOURCE,
    CONF_FILE,
    CONF_URL,
    CONF_EXTRACT_FROM,
    CONF_INNER_ZIP,
    CONF_REAL_TIME,
    CONF_TRIP_UPDATE_URL,
    CONF_VEHICLE_POSITION_URL,
    CONF_ALERTS_URL,
    CONF_API_KEY,
    CONF_API_KEY_NAME,
    CONF_API_KEY_LOCATION,
    CONF_ACCEPT_HEADER_PB,
    CONF_RT_ENABLED,
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

RT_FEED_URL_KEYS = (CONF_TRIP_UPDATE_URL, CONF_VEHICLE_POSITION_URL, CONF_ALERTS_URL)

# the key the static feed is downloaded with, held in the datasource entry's
# data next to the url and mirrored onto the journey entries. Distinct from
# the realtime key in the options: one key per feed, the provider may differ
STATIC_KEY_KEYS = (CONF_API_KEY, CONF_API_KEY_NAME, CONF_API_KEY_LOCATION)

def has_rt_feed(cfg: Mapping[str, Any]) -> bool:
    """Whether a config carries at least one realtime feed url.

    Trip updates are the usual case, but a feed can also publish alerts or
    vehicle positions alone (the TTC subway is alerts-only): any of the three
    urls makes the source a realtime source. What each reader does without a
    trip updates url is its own affair; most consume nothing else and simply
    keep to the static timetable.
    """
    return any(cfg.get(k) for k in RT_FEED_URL_KEYS)


def datasource_unique_id(file: str) -> str:
    """The unique_id of a source's datasource entry.

    Its own prefix: the bare file name was the unique_id, and a source
    named gtfs-home took the gtfs-home a journey named home is given, so
    Home Assistant refused whichever came second.
    """
    return f"gtfs2-source-{file}"


def source_device(file: str) -> DeviceInfo:
    """The device a source's own entities share (realtime, timetable,
    switch, button, update), so the source reads as one."""
    return DeviceInfo(
        name=f"GTFS - {file}",
        entry_type=DeviceEntryType.SERVICE,
        identifiers={(DOMAIN, f"GTFS datasource - {file}")},
        manufacturer="GTFS",
        model=file,
    )


def datasource_entry(hass: HomeAssistant, file: str | None) -> ConfigEntry | None:
    """The datasource entry of a source, or None while it does not exist."""
    if not file:
        return None
    # a bare hass with no registry (upstream's synthetic suite hands the
    # coordinator one) has no datasource entry either
    registry = getattr(hass, "config_entries", None)
    if registry is None:
        return None
    for entry in registry.async_entries(DOMAIN):
        if (entry.data.get(CONF_KIND) == ENTRY_KIND_DATASOURCE
                and entry.data.get(CONF_FILE) == file):
            return entry
    return None


def journey_entries(hass: HomeAssistant, file: str | None) -> list[ConfigEntry]:
    """Every non-datasource entry reading this source, local stops included."""
    return [
        entry for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE
        and entry.data.get(CONF_FILE) == file
    ]


def source_readers(hass: HomeAssistant, file: str,
                   exclude: str | None = None) -> tuple[set[str], bool]:
    """(the route_ids the source's sensors name, whether one reads it whole).

    A train sensor ("train" is its marker, not a route_id), a local stops
    sensor and one naming no line match across the whole feed: their source
    has to keep every line. exclude leaves one entry_id out: whether or not
    an entry still lists while its removal hook runs, it is no reader.
    """
    routes: set[str] = set()
    whole = False
    for entry in journey_entries(hass, file):
        if exclude is not None and entry.entry_id == exclude:
            continue
        route = id_of(entry.data.get("route"))
        if entry.data.get("device_tracker_id") or route in ("", "train"):
            whole = True
        else:
            routes.add(route)
    return routes, whole


def rt_feed_config(hass: HomeAssistant, entry: ConfigEntry) -> tuple[Mapping[str, Any], bool]:
    """The realtime config an entry runs with, and whether realtime is on.

    The source's datasource entry is authoritative when it exists: its
    options hold the feeds, shared by every sensor of the source, and
    realtime is on as soon as any feed url is set - the per-sensor
    boolean does not apply any more. Without a datasource entry the entry's
    own options apply unchanged, which is what keeps an install working
    before the bootstrap ran, and after a downgrade.

    Read on every coordinator cycle, so an edit on the datasource entry
    reaches every sensor of the source within a minute, with no reload.
    The rt_enabled switch silences the source without touching the urls:
    off means no sensor reads the feeds, config kept for when it returns.
    """
    source = datasource_entry(hass, entry.data.get(CONF_FILE))
    if source is not None:
        cfg = source.options
        return cfg, bool(has_rt_feed(cfg) and cfg.get(CONF_RT_ENABLED, True))
    return entry.options, bool(entry.options.get(CONF_REAL_TIME, False))


def with_query_key(url: str | None, cfg: Mapping[str, Any]) -> str | None:
    """The url with the api key appended, when the key travels in the query.

    None-safe on purpose: the code this replaces concatenated onto every feed
    url whether it was set or not, which is a TypeError as soon as a source
    keeps its key in the query string and has no vehicle or alerts feed.

    The one place a key joins a url, static feeds included. Joined with "&"
    onto a url that already asks something, where a second "?" made the
    key part of the previous value, and percent-encoded, where a key with
    a "+" or a "&" reached the host as something else.
    """
    if not url:
        return None
    if cfg.get(CONF_API_KEY_LOCATION) == "query_string" and cfg.get(CONF_API_KEY):
        name = cfg.get(CONF_API_KEY_NAME) or DEFAULT_API_KEY_NAME
        return (url + ("&" if "?" in url else "?")
                + quote(name, safe="") + "=" + quote(cfg[CONF_API_KEY], safe=""))
    return url


def rt_headers(cfg: Mapping[str, Any]) -> dict[str, str | None] | None:
    """The request headers of a feed whose key travels in a header, else None."""
    if cfg.get(CONF_API_KEY_LOCATION) != "header":
        return None
    headers = {cfg.get(CONF_API_KEY_NAME, DEFAULT_API_KEY_NAME): cfg.get(CONF_API_KEY)}
    if cfg.get(CONF_ACCEPT_HEADER_PB, False):
        headers["Accept"] = "application/x-protobuf"
    return headers


def _rt_seed(entries: list[ConfigEntry]) -> dict[str, Any]:
    """The realtime options to seed a new datasource entry with.

    Several journey entries can carry a copy and they can disagree: the most
    recently modified entry wins, and each loser whose config differed gets a
    log line naming what was left behind, so nothing disappears silently.
    """
    donors = [e for e in entries if has_rt_feed(e.options)]
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


def static_key_fields(fields: Mapping[str, Any]) -> dict[str, str]:
    """The static key trio as the datasource entry stores it.

    The three fields travel together behind a real key; without one the
    location alone is kept, saying "none" the way every flow always did. A
    blank key means none, like an emptied feed url.
    """
    key = (fields.get(CONF_API_KEY) or "").strip()
    if not key:
        return {CONF_API_KEY_LOCATION: DEFAULT_API_KEY_LOCATION}
    return {
        CONF_API_KEY: key,
        CONF_API_KEY_NAME: (fields.get(CONF_API_KEY_NAME) or "").strip()
        or DEFAULT_API_KEY_NAME,
        CONF_API_KEY_LOCATION: fields.get(CONF_API_KEY_LOCATION)
        or DEFAULT_API_KEY_LOCATION,
    }


def _static_seed(entries: list[ConfigEntry]) -> dict[str, str]:
    """The static key a datasource entry takes over from its journey entries.

    From the first entry that actually carries a key: every flow stores
    api_key_location (usually "not_applicable"), so taking the fields one by
    one would let a keyless entry strip a later keyed entry's authentication.
    """
    for entry in entries:
        if entry.data.get(CONF_API_KEY):
            return static_key_fields(entry.data)
    return static_key_fields({})


def static_feed_config(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """How the source's static feed is fetched: address, origin and key.

    The datasource entry's data is authoritative once it has spoken for the
    key, which the bootstrap and the source's own screen both do by writing
    api_key_location. An entry from before that falls back on its journey
    entries, exactly as the refresh always did, so nothing changes for an
    install until the bootstrap has run.
    """
    data = entry.data
    cfg = {
        CONF_FILE: data.get(CONF_FILE),
        CONF_URL: data.get(CONF_URL),
        # fetched from its url; a refresh that reads the kept zip instead
        # says so itself (use_zip)
        CONF_EXTRACT_FROM: "url",
    }
    if data.get(CONF_INNER_ZIP):
        # the network picked inside an envelope: every refresh asks for it
        cfg[CONF_INNER_ZIP] = data[CONF_INNER_ZIP]
    api = (static_key_fields(data) if CONF_API_KEY_LOCATION in data
           else _static_seed(journey_entries(hass, cfg[CONF_FILE])))
    if api.get(CONF_API_KEY):
        cfg.update(api)
    return cfg


async def async_ensure_datasource_entry(
        hass: HomeAssistant, file: str, url: str | None = None,
        api: Mapping[str, Any] | None = None, inner_zip: str | None = None) -> None:
    """Create the datasource entry of a source, unless it already exists.

    Called by the bootstrap and by the flow steps that bring a new source in.
    Idempotent by construction: the file name makes the unique_id, so a second
    creation aborts inside the import flow instead of duplicating. The flow
    passes the static key it collected as api; the bootstrap takes it over
    from the journey entries. inner_zip is the network picked inside an
    envelope, kept so every refresh fetches that member again.
    """
    if not file or datasource_entry(hass, file) is not None:
        return
    entries = journey_entries(hass, file)
    if url is None:
        # the entries carry the url the source was downloaded from; a
        # source made from a zip in the folder has none, and the import
        # step gives it the file:// url of its kept zip
        url = next((e.data.get(CONF_URL) for e in entries
                    if e.data.get(CONF_URL)), None)
    _LOGGER.info("Creating datasource entry for source: %s", file)
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_KIND: ENTRY_KIND_DATASOURCE,
            CONF_FILE: file,
            CONF_URL: url,
            **({CONF_INNER_ZIP: inner_zip} if inner_zip else {}),
            **(static_key_fields(api) if api is not None
               else _static_seed(entries)),
            # carried through the import step into entry.options, where
            # rt_feed_config will look for it
            "options": _rt_seed(entries),
        },
    )


async def async_bootstrap_datasource_entries(hass: HomeAssistant,
                                             datasources: Iterable[str]) -> None:
    """Give every known source its datasource entry, idempotently.

    Runs in the background at every start: the sources are collected from the
    disk and from the entries, and only the missing datasource entries are
    created. Deliberately never through async_migrate_entry - a per-entry
    migration of a per-source object is how a bootstrap ends half-done and
    unrepeatable, and the journey entries are not touched at all.
    The caller lists the sources on disk (get_datasources).
    """
    files = set(datasources)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE
                and entry.data.get(CONF_FILE)):
            files.add(entry.data[CONF_FILE])
            continue
        if entry.data.get(CONF_KIND) != ENTRY_KIND_DATASOURCE:
            continue
        new_data = {**entry.data}
        # the static key used to live on the journey entries only: an entry
        # from before takes it over once, and says so by carrying the
        # location even when there is no key, so the fallback never runs
        # again for it
        if CONF_API_KEY_LOCATION not in new_data:
            _LOGGER.info("Datasource %s takes over its static feed key",
                         new_data.get(CONF_FILE))
            new_data.update(_static_seed(
                journey_entries(hass, new_data.get(CONF_FILE))))
        if new_data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=new_data)
    for file in sorted(files):
        try:
            await async_ensure_datasource_entry(hass, file)
        except Exception as ex:  # pylint: disable=broad-except
            # one source failing must not keep the others from their entry
            _LOGGER.exception("Could not create datasource entry for %s: %s", file, ex)


async def async_mirror_rt_to_entries(hass: HomeAssistant, source_entry: ConfigEntry) -> None:
    """Write the datasource's feeds through to its journey entries.

    While the datasource entry exists the resolution never reads the journey
    copies - but a downgrade to upstream does. Mirroring on every edit keeps
    that exit working with current values instead of the ones frozen at
    bootstrap. The per-entry boolean follows the all-source rule: realtime is
    on wherever the source has a trip updates url.

    The static feed is mirrored the same way, into the entries' data: the
    address the zip comes from, and the key that download needs once the
    source has taken it over. A key the source dropped is taken off the
    entries too, so a stale copy can never bring it back.
    """
    cfg = source_entry.options
    src = source_entry.data
    # a source silenced by its switch mirrors as realtime off: the urls stay
    # in place, here and on the journey entries alike. Deliberately stricter
    # than has_rt_feed: upstream can only run realtime on trip updates, so an
    # alerts-only source mirrors as off rather than erroring there every cycle
    active = bool(cfg.get(CONF_TRIP_UPDATE_URL)) and cfg.get(CONF_RT_ENABLED, True)
    for entry in journey_entries(hass, src.get(CONF_FILE)):
        new_options = _mirrored_options(entry.options, cfg, active)
        if new_options != dict(entry.options):
            hass.config_entries.async_update_entry(entry, options=new_options)
        new_data = _mirrored_data(entry.data, src)
        if new_data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=new_data)


def _mirrored_options(options: Mapping[str, Any], cfg: Mapping[str, Any], active: bool) -> dict[str, Any]:
    """A journey entry's options with the source's realtime feeds written over them."""
    new_options = {**options}
    for key in RT_OPTION_KEYS:
        if key in cfg:
            new_options[key] = cfg[key]
        else:
            new_options.pop(key, None)
    new_options[CONF_REAL_TIME] = active
    return new_options


def _mirrored_data(data: Mapping[str, Any], src: Mapping[str, Any]) -> dict[str, Any]:
    """A journey entry's data with the source's static address and key."""
    new_data = {**data}
    if src.get(CONF_URL):
        new_data[CONF_URL] = src[CONF_URL]
    if CONF_API_KEY_LOCATION in src:
        if src.get(CONF_API_KEY):
            for key in STATIC_KEY_KEYS:
                new_data[key] = src[key]
        elif data.get(CONF_API_KEY):
            for key in STATIC_KEY_KEYS:
                new_data.pop(key, None)
            new_data[CONF_API_KEY_LOCATION] = DEFAULT_API_KEY_LOCATION
    return new_data
